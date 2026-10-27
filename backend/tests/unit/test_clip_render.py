"""第五步：clip_render 集成测试（真实 FFmpeg，不调用任何模型）。

合成 8 秒素材（testsrc2 + 正弦音），验证：
* 多切片渲染、产物可解码（ffprobe）、SRT 时间平移与裁剪；
* 过短/completeness=warning 的切片照常渲染（只有 warning，无拦截）；
* 单条技术故障隔离（partial_succeeded，成功文件保留）；
* worker 重启恢复：已存在且可解码的成片跳过（mtime 不变）；
* 模块零模型依赖（源码级断言）。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from src import clip_render as cr


@pytest.fixture(scope="module")
def source_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("media") / "source.mp4"
    subprocess.run(
        [cr._resolve(cr.FFMPEG_BIN), "-y",
         "-f", "lavfi", "-i", "testsrc2=duration=8:size=320x240:rate=15",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest",
         str(path)],
        check=True, capture_output=True)
    return path


def _plan() -> dict:
    return {
        "plan_source": "heuristic_fallback", "warnings": [], "rule_snapshot_id": "2026-10-13.10",
        "clips": [
            {"clip_id": "clip-a", "topic": "主题A", "start_ms": 0, "end_ms": 2000,
             "completeness": "pass", "warnings": []},
            {"clip_id": "clip-b", "topic": "过短但有效", "start_ms": 2500, "end_ms": 3500,
             "completeness": "warning", "warnings": ["short_duration: 时长 1s 低于 30s"]},
            {"clip_id": "clip-c", "topic": "主题C", "start_ms": 4000, "end_ms": 6000,
             "completeness": "pass", "warnings": ["时长 2s 偏离正常区间 90–210s"]},
        ],
    }


def _segments() -> list[dict]:
    return [
        {"segment_id": "seg_0000", "start_ms": 500, "end_ms": 1800, "text": "第一段。"},
        {"segment_id": "seg_0001", "start_ms": 2600, "end_ms": 3400, "text": "第二段。"},
        {"segment_id": "seg_0002", "start_ms": 1500, "end_ms": 4500, "text": "跨界段。"},
        {"segment_id": "seg_0003", "start_ms": 7000, "end_ms": 7800, "text": "切片外。"},
    ]


def test_srt_shift_clamp_and_provenance():
    srt, used, warnings = cr.build_srt(_segments(), 0, 2000)
    assert "00:00:00,500 --> 00:00:01,800" in srt          # seg_0000 平移
    assert "00:00:01,500 --> 00:00:02,000" in srt          # 跨界段被裁剪到切片内
    assert "seg_0000" in used and "seg_0002" in used
    assert "seg_0003" not in used                           # 其他区间文字禁止出现
    assert "第一段" in srt and "切片外" not in srt
    empty_srt, empty_used, empty_warnings = cr.build_srt(_segments(), 5_500, 6_500)
    assert empty_used == [] and empty_srt == ""
    assert empty_warnings and "没有命中" in empty_warnings[0]


def test_render_plan_multi_clip_outputs_decodable(source_video: Path, tmp_path: Path):
    out_root = tmp_path / "output" / "job-r1"
    result = cr.render_plan(_plan(), source=source_video, out_root=out_root,
                            corrected_segments=_segments(),
                            transcript_ref={"transcript_id": "tr-x", "transcript_version": 2},
                            rule_snapshot_id="2026-10-13.10")
    assert result["job_status"] == "completed_with_warnings"
    assert result["counts"]["succeeded"] == 3
    for entry in result["entries"]:
        assert entry["status"] == "rendered", entry
        clip_dir = out_root / "clips" / entry["clip_id"]
        final = clip_dir / "clip_final.mp4"
        assert final.is_file() and final.stat().st_size > 0
        playable, reason = cr._ffprobe_playable(final)
        assert playable, reason                              # ffprobe 可解码
        assert (clip_dir / "subtitles.srt").is_file()
        assert (clip_dir / "clip_plan.json").is_file()
        warnings_doc = json.loads((clip_dir / "warnings.json").read_text())
        assert warnings_doc["cover"] is None                 # 本轮无封面且不是错误
        assert warnings_doc["subtitle_provenance"]["transcript_version"] == 2
        assert warnings_doc["rule_snapshot_id"] == "2026-10-13.10"
    short_doc = json.loads((out_root / "clips" / "clip-b" / "warnings.json").read_text())
    assert any("short_duration" in w for w in short_doc["warnings"])  # 过短仅 warning
    assert not list(out_root.rglob("cover.jpg"))             # 不生成封面
    # 字幕只来自本切片区间
    b_srt = (out_root / "clips" / "clip-b" / "subtitles.srt").read_text()
    assert "第二段" in b_srt and "第一段" not in b_srt


def test_failure_isolation_partial_succeeded(source_video: Path, tmp_path: Path):
    out_root = tmp_path / "output" / "job-r2"
    real_ffmpeg = cr._run_ffmpeg

    def flaky(args: list[str]) -> None:
        if any("clip-b" in str(a) for a in args):            # 注入第 2 条技术故障
            raise RuntimeError("模拟 FFmpeg 失败")
        real_ffmpeg(args)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(cr, "_run_ffmpeg", flaky)
    try:
        result = cr.render_plan(_plan(), source=source_video, out_root=out_root,
                                corrected_segments=_segments(),
                                transcript_ref={"transcript_id": "tr-x", "transcript_version": 2})
    finally:
        monkey.undo()
    assert result["job_status"] == "partial_succeeded"
    assert result["counts"]["technical_error"] == 1
    by_id = {e["clip_id"]: e for e in result["entries"]}
    assert by_id["clip-a"]["status"] == "rendered"
    assert by_id["clip-c"]["status"] == "rendered"           # 其余切片继续完成
    assert by_id["clip-b"]["status"] == "technical_error"
    assert "模拟 FFmpeg 失败" in by_id["clip-b"]["technical_error"]
    assert (out_root / "clips" / "clip-a" / "clip_final.mp4").is_file()  # 成功文件保留


def test_resume_skips_playable_and_completes_missing(source_video: Path, tmp_path: Path):
    out_root = tmp_path / "output" / "job-r3"
    real_ffmpeg = cr._run_ffmpeg

    def fail_b(args: list[str]) -> None:
        if any("clip-b" in str(a) for a in args):
            raise RuntimeError("模拟 FFmpeg 失败")
        real_ffmpeg(args)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(cr, "_run_ffmpeg", fail_b)
    try:
        cr.render_plan(_plan(), source=source_video, out_root=out_root,
                       corrected_segments=_segments(),
                       transcript_ref={"transcript_id": "tr-x", "transcript_version": 2})
    finally:
        monkey.undo()
    a_final = out_root / "clips" / "clip-a" / "clip_final.mp4"
    mtime_before = a_final.stat().st_mtime_ns

    # worker 重启后重跑：a/c 可解码直接跳过（mtime 不变），b 补渲染
    result = cr.render_plan(_plan(), source=source_video, out_root=out_root,
                            corrected_segments=_segments(),
                            transcript_ref={"transcript_id": "tr-x", "transcript_version": 2})
    assert result["counts"]["skipped_playable"] == 2
    by_id = {e["clip_id"]: e for e in result["entries"]}
    assert by_id["clip-a"]["status"] == "skipped_playable"
    assert by_id["clip-b"]["status"] == "rendered"
    assert a_final.stat().st_mtime_ns == mtime_before        # 未被覆盖重渲
    assert result["job_status"] == "completed_with_warnings"


def test_all_technical_failure_maps_to_technical_failed(source_video: Path, tmp_path: Path):
    monkey = pytest.MonkeyPatch()
    monkey.setattr(cr, "_run_ffmpeg", lambda args: (_ for _ in ()).throw(RuntimeError("磁盘故障")))
    try:
        result = cr.render_plan(_plan(), source=source_video, out_root=tmp_path / "out",
                                corrected_segments=_segments(),
                                transcript_ref={"transcript_id": "tr-x", "transcript_version": 2})
    finally:
        monkey.undo()
    assert result["job_status"] == "technical_failed"
    assert result["counts"]["technical_error"] == 3


def test_clip_render_module_has_no_model_dependencies():
    source = Path(cr.__file__).read_text(encoding="utf-8")
    for banned in ("pydantic_ai", "content_analysis", "transcript_correction",
                   "clip_planning", "studio_copilot"):
        assert banned not in source, f"渲染模块不得依赖模型层：{banned}"


def test_worker_render_stage_registers_artifacts_and_partial_status(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from src import studio_job_worker as worker

    monkeypatch.setenv("STUDIO_TASKS_ROOT", str(tmp_path / "tasks"))
    from src import studio_project_store
    monkeypatch.setattr(studio_project_store, "TASKS_ROOT", tmp_path / "tasks")
    monkeypatch.setattr(worker, "_state",
                        lambda job_id, status, stage, progress, error=None: states.append(status))
    monkeypatch.setattr(worker, "_materialise_source_for_job", lambda key, jid: "video.mp4")
    monkeypatch.setattr("src.r2.upload_file", lambda path, key: {"object_key": key, "size": 1})
    states: list[str] = []
    posts: list[dict] = []
    monkeypatch.setattr(worker, "_http", lambda method, url, payload=None, timeout=60: {"artifacts":[]} if method=="GET" else {"accepted":True} if url.endswith("/activate") else posts.append(payload) or {})
    aggregate = {
        "job_status": "partial_succeeded",
        "entries": [
            {"clip_id": "clip-a", "status": "rendered", "technical_error": None, "warnings": [], "render_warnings": []},
            {"clip_id": "clip-b", "status": "technical_error", "technical_error": "boom",
             "warnings": [], "render_warnings": []},
        ],
        "counts": {"total": 2, "succeeded": 1, "technical_error": 1, "skipped_playable": 0},
        "warnings": [],
    }
    monkeypatch.setattr(cr, "render_plan", lambda plan, **kwargs: aggregate)

    plan_record = {"plan_source": "llm", "clips": [], "rule_snapshot_id": "v1"}
    Path(tmp_path / "tasks" / "output" / "autoedit_r1" / "clips" / "clip-a").mkdir(parents=True)
    (tmp_path / "tasks" / "output" / "autoedit_r1" / "clips" / "clip-a" / "clip_final.mp4").write_bytes(b"x")
    (tmp_path / "tasks" / "output" / "autoedit_r1" / "clips" / "clip-a" / "warnings.json").write_text("{}")
    worker._run_clip_render_stage(
        {"job_id": "autoedit_r1", "object_key": "local:/v.mp4", "project_id": "timeline_t"},
        "autoedit_r1", {"asr_segments": []}, plan_record)

    assert states[-1] == "partial_succeeded"
    kinds = [p.get("kind") for p in posts]
    assert kinds.count("clip_final") == 1 and kinds.count("clip_warnings") == 1
    assert posts[0].get("clip_id") == "clip-a"


@pytest.fixture
def subtitle_mock_runtime(tmp_path, monkeypatch):
    from src import capacity_guard as cg
    source = tmp_path / "source"; source.write_bytes(b"synthetic-source")
    font = tmp_path / "SyntheticCJK.ttc"; font.write_bytes(b"synthetic-font")
    monkeypatch.setattr(cg, "free_bytes", lambda _: 10 * 1024**3)
    guard = tmp_path / "guard"; guard.mkdir()
    monkeypatch.setattr(cg, "_reservation_paths", lambda device: (guard/f"{device}.lock",guard/f"{device}.json"))
    monkeypatch.setattr(cr, "_subtitle_font", lambda: (font, "Synthetic CJK"))
    monkeypatch.setattr(cr, "_ffprobe_playable", lambda p: (Path(p).is_file(), ""))
    calls = []
    def render(args):
        calls.append(args)
        Path(args[-1]).write_bytes(b"synthetic-rendered-video")
    monkeypatch.setattr(cr, "_run_ffmpeg", render)
    return source, calls


def test_fixed_revision_subtitle_burn_arguments_boundaries_cache_and_cards(tmp_path, monkeypatch, subtitle_mock_runtime):
    import json
    from src import studio_project_store as store
    source, calls = subtitle_mock_runtime
    root = tmp_path / "tasks"; monkeypatch.setattr(store,"TASKS_ROOT",root)
    run = "f"*32; cid = "clip-subtitles"
    out = root/"output"/run/"clips"/cid
    clip = {"clip_id":cid,"start_ms":1000,"end_ms":3000}
    segments = [
        {"segment_id":"head","start_ms":800,"end_ms":1200,"text":"合成片头重叠"},
        {"segment_id":"tail","start_ms":2800,"end_ms":3200,"text":"合成片尾重叠"},
        {"segment_id":"outside","start_ms":3500,"end_ms":4000,"text":"片外不得进入字幕"}]
    options = dict(source=source,out_dir=out,corrected_segments=segments,
                   transcript_ref={"transcript_id":"synthetic-tr","transcript_version":7},rule_snapshot_id="synthetic")
    rendered = cr.render_one(clip,**options)
    # 2026-10-19 起底片不烧字幕：mode=sidecar_only，烧录在 transform 导出（studio_transform_export）
    assert rendered["subtitle_mode"] == "sidecar_only"
    assert [(cue["start"],cue["end"]) for cue in rendered["segments"]] == [(0,.2),(1.8,2)]
    seconds=[{"segment_id":seg["segment_id"],"text":seg["text"],
              "start":seg["start_ms"]/1000,"end":seg["end_ms"]/1000} for seg in segments]
    assert cr._subtitle_cues(seconds,1000,3000) == rendered["segments"]
    vf = calls[0][calls[0].index("-vf")+1]
    assert "subtitles=" not in vf and "scale=" in vf
    assert calls[0][-1].endswith("clip_final.rendering.mp4")
    srt = (out/"subtitles.srt").read_text()
    assert "00:00:00,000 --> 00:00:00,200" in srt
    assert "00:00:01,800 --> 00:00:02,000" in srt and "片外不得" not in srt
    warning = json.loads((out/"warnings.json").read_text())
    assert warning["subtitle_provenance"]["transcript_version"] == 7
    assert warning["subtitle_provenance"]["segment_ids"] == ["head","tail"]
    store.register_clips("timeline_subtitles",run,[{"clip_id":cid}])
    card = store.list_cards("timeline_subtitles")[0]
    assert [c["segment_id"] for c in card["segments"]] == ["head","tail"]
    assert card["subtitle_provenance"]["transcript_version"] == 7 and card["subtitle_mode"] == "sidecar_only"
    assert cr.render_one(clip,**options)["status"] == "skipped_playable" and len(calls) == 1
    options["transcript_ref"] = {"transcript_id":"synthetic-tr","transcript_version":8}
    cr.render_one(clip,**options)
    assert len(calls) == 2
    (out/"subtitles.srt").write_text("changed-sidecar")
    cr.render_one(clip,**options)
    assert len(calls) == 3


def test_missing_cjk_font_does_not_block_sidecar_render(tmp_path, monkeypatch, subtitle_mock_runtime):
    # 2026-10-19 起底片不再烧字幕：无 CJK 字体也不影响底片渲染（烧录在导出侧，届时再要求字体）
    source, calls = subtitle_mock_runtime
    monkeypatch.setattr(cr,"_subtitle_font",lambda:(None,None))
    result = cr.render_one({"clip_id":"no-font","end_ms":1000},source=source,out_dir=tmp_path/"out",
        corrected_segments=[{"start_ms":0,"end_ms":1000,"text":"合成字幕"}],
        transcript_ref={"transcript_version":2},rule_snapshot_id=None)
    assert result["subtitle_mode"] == "sidecar_only" and result["render_warnings"] == []
    assert "subtitles=" not in calls[0][calls[0].index("-vf")+1]


def test_builtin_ttc_registry_family_and_user_upload_format_unchanged(tmp_path, monkeypatch):
    import struct
    from src import font_registry as registry
    family = "Synthetic CJK"; value = family.encode("utf-16-be")
    name = struct.pack(">HHH",0,1,18)+struct.pack(">HHHHHH",3,1,0x409,1,len(value),0)+value
    data = b"ttcf"+struct.pack(">III",0x10000,1,16)
    data += b"\x00\x01\x00\x00"+struct.pack(">HHHH",1,16,0,0)
    data += b"name"+struct.pack(">III",0,44,len(name))+name
    fonts = tmp_path/"fonts"; fonts.mkdir()
    ttc = fonts/"MicrosoftYaHeiBold.ttc"; ttc.write_bytes(data)
    monkeypatch.setattr(registry,"FONTS_DIR",fonts)
    monkeypatch.setattr(registry,"USER_FONTS_DIR",fonts/"users")
    assert registry.find_font_path(ttc.name) == ttc
    assert registry.get_font_family_name(ttc) == family
    assert registry.get_available_fonts()[0]["format"] == "ttc"
    user = registry.get_user_fonts_dir("synthetic"); user.mkdir(parents=True)
    (user/"upload.ttc").write_bytes(data)
    assert ".ttc" not in registry.SUPPORTED_FONT_EXTENSIONS
    assert registry.find_user_font_path("upload.ttc","synthetic") is None


def test_srt_independent_artifact_registered_with_clip_revision(tmp_path, monkeypatch, subtitle_mock_runtime):
    from src import studio_job_worker as worker, studio_project_store as store, r2
    import shutil
    source, _calls = subtitle_mock_runtime
    root = tmp_path/"tasks"; monkeypatch.setattr(store,"TASKS_ROOT",root)
    monkeypatch.setenv("STUDIO_TASKS_ROOT",str(root))
    run = "e"*32; plan = {"clips":[{"clip_id":"clip-srt","start_ms":0,"end_ms":1000}]}
    aggregate = cr.render_plan(plan,source=source,out_root=root/"output"/run,
        corrected_segments=[{"segment_id":"s1","start_ms":0,"end_ms":1000,"text":"合成SRT"}],
        transcript_ref={"transcript_id":"synthetic-tr","transcript_version":3})
    monkeypatch.setattr(cr,"render_plan",lambda *a,**k:aggregate)
    monkeypatch.setattr(worker,"_materialise_source_for_job",lambda *a:source)
    monkeypatch.setattr(worker,"_rule_snapshot_id",lambda:"synthetic")
    monkeypatch.setattr(worker,"_state",lambda *a,**k:None)
    posts=[]
    def http(method,url,payload=None,**kwargs):
        if method=="GET": return {"artifacts":[]}
        if url.endswith("/activate"):
            from src.studio_job_store import _register_rendered_clips
            _register_rendered_clips({"id":run,"project_id":"timeline_srt","config":{}},payload["plan_record"],payload["published"])
            return {"accepted":True}
        posts.append(payload);return {}
    monkeypatch.setattr(worker,"_http",http)
    monkeypatch.setattr(r2,"local_path_for",lambda key:tmp_path/"objects"/key)
    def upload(path,key):
        dest=r2.local_path_for(key);dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,dest)
        return {"object_key":key,"size":Path(path).stat().st_size}
    monkeypatch.setattr(r2,"upload_file",upload)
    worker._run_clip_render_stage({"project_id":"timeline_srt","object_key":"synthetic"},run,{},plan)
    srt=[p for p in posts if p["kind"] == "srt"]
    assert len(srt)==1 and srt[0]["clip_id"]=="clip-srt" and srt[0]["size_bytes"]>0
    card=store.list_cards("timeline_srt")[0]
    assert card["subtitle_provenance"]["transcript_version"]==3


def test_invalid_clip_path_rejected_before_any_output(tmp_path, subtitle_mock_runtime):
    source,_calls=subtitle_mock_runtime
    out=tmp_path/"out"
    with pytest.raises(cr.ClipRenderError,match="非法clip_id"):
        cr.render_one({"clip_id":"../outside","end_ms":1000},source=source,out_dir=out,
                      corrected_segments=[],transcript_ref={},rule_snapshot_id=None)
    assert not out.exists()
