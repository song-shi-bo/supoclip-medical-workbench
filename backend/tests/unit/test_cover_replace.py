"""cover_replace 单元测试（纯逻辑优先：不跑真实 ffmpeg、不联网、不碰真实成片）。

覆盖：
1. 风格白名单校验（缺省 / 合法 / 非法 → E_ 码）；
2. 片头切点规划（关键帧对齐 / 桥接 / 病态无关键帧 / 非法入参）；
3. ffmpeg 命令构造（片头帧数·时长·淡入、有音轨取原音、无音轨补静音、尾部流拷贝、
   桥接段、concat）；
4. 备份 / 幂等 / 回滚（打桩 ffprobe，只在 tmp 目录里动文件）；
5. QC 断言（帧数·时长·音轨·首帧·尾部一致）与失败清单，全部打桩。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import cover_replace as cr
from src import studio_auto_edit_bridge as bridge


# ---------------------------------------------------------------------------
# 1. 风格白名单
# ---------------------------------------------------------------------------

def test_validate_style_none_uses_regenerate_default_from_rules():
    """未指定风格 → 规则清单 cover_rules.style_for_regenerate（当前 cutout_v3）。

    2026-10-13 成员规则：用户主动「重新制作封面」缺省 = 第三版·人物抠像；
    首次自动剪辑缺省才是第二版·真实帧（两链路各读各的字段）。
    """
    from src import studio_workflow
    expected = studio_workflow.cover_style_choice(purpose="regenerate")[0]
    assert expected == "cutout_v3"
    assert cr.validate_style_id(None) == expected
    assert cr.validate_style_id("") == expected
    assert cr.regenerate_default_style()[0] == expected
    assert cr.DEFAULT_COVER_STYLE == "realframe_v2"      # 代码兜底仍是 v2


@pytest.mark.parametrize("style_id", ["realframe_v2", "cutout_v3"])
def test_validate_style_accepts_whitelist(style_id):
    assert cr.validate_style_id(style_id) == style_id
    assert cr.validate_style_id(f"  {style_id}  ") == style_id


@pytest.mark.parametrize("style_id", ["bogus", "medical_big_text_v3", "realframe_v1", " "])
def test_validate_style_rejects_unknown_with_code(style_id):
    if style_id == " ":
        assert cr.validate_style_id(style_id) == cr.regenerate_default_style()[0]
        return
    with pytest.raises(cr.CoverReplaceError) as excinfo:
        cr.validate_style_id(style_id)
    assert excinfo.value.code == cr.E_COVER_STYLE_INVALID
    assert "realframe_v2" in str(excinfo.value)


def test_validate_style_allow_legacy_is_opt_in():
    """legacy 只允许被沿用/只读引用，不允许作为生成风格。"""
    assert cr.validate_style_id("medical_big_text_v3", allow_legacy=True) == "medical_big_text_v3"
    with pytest.raises(cr.CoverReplaceError):
        cr.validate_style_id("medical_big_text_v3")


def test_cover_replace_error_is_value_error():
    """错误映射契约：ValueError 子类 → 路由层 400（非法风格在路由层是 422）。"""
    assert issubclass(cr.CoverReplaceError, ValueError)


# ---------------------------------------------------------------------------
# 2. 片头切点规划
# ---------------------------------------------------------------------------

KF = [{"frame": 0, "time_s": 0.022969}, {"frame": 20, "time_s": 0.822969},
      {"frame": 45, "time_s": 1.822969}]


def test_plan_split_exact_keyframe_is_lossless():
    plan = cr.plan_intro_split(25.0, 427, KF, 20)
    assert plan["mode"] == "exact_copy"
    assert plan["bridge_frames"] == 0
    assert plan["split_frame"] == 20
    assert plan["split_time_s"] == 0.822969          # 用真实 pts，不是 20/25
    assert plan["tail_frames"] == 407
    assert plan["head_duration_s"] == 0.8
    assert plan["tail_copy"] is True


def test_plan_split_uses_next_keyframe_after_target():
    """片头边界不在关键帧上 → 桥接最多一个 GOP，尾部仍可流拷贝。"""
    plan = cr.plan_intro_split(25.0, 427, KF, 17)
    assert plan["split_frame"] == 20
    assert plan["bridge_frames"] == 3
    assert plan["mode"] == "bridge_reencode"
    assert plan["tail_copy"] is True


def test_plan_split_without_later_keyframe_reencodes_tail():
    plan = cr.plan_intro_split(25.0, 100, [{"frame": 0, "time_s": 0.0}], 20)
    assert plan["tail_copy"] is False
    assert plan["mode"] == "tail_reencode"
    assert plan["split_frame"] == 20
    assert plan["bridge_frames"] == 0


def test_plan_split_missing_pts_falls_back_to_frame_over_fps():
    plan = cr.plan_intro_split(25.0, 427, [{"frame": 20}], 20)
    assert plan["split_time_s"] == pytest.approx(0.8)


@pytest.mark.parametrize("fps,total,target,code", [
    (0.0, 427, 20, cr.E_COVER_BAD_REQUEST),
    (25.0, 0, 20, cr.E_COVER_BAD_REQUEST),
    (25.0, 427, 0, cr.E_COVER_BAD_REQUEST),
    (25.0, 20, 20, cr.E_COVER_BAD_REQUEST),      # 片头 ≥ 全片
])
def test_plan_split_rejects_invalid_input(fps, total, target, code):
    with pytest.raises(cr.CoverReplaceError) as excinfo:
        cr.plan_intro_split(fps, total, KF, target)
    assert excinfo.value.code == code


# ---------------------------------------------------------------------------
# 3. ffmpeg 命令构造
# ---------------------------------------------------------------------------

PARAMS = {"fps": 25.0, "pix_fmt": "yuv420p", "has_audio": True, "acodec": "aac",
          "sample_rate": 44100, "channels": 1, "channel_layout": "mono",
          "audio_bit_rate": 128000, "profile": "High", "level": 40,
          "width": 1080, "height": 1920}


def _plan(**overrides):
    plan = cr.plan_intro_split(25.0, 427, KF, 20)
    plan.update(overrides)
    return plan


def test_head_command_keeps_original_intro_audio_and_frame_count(tmp_path):
    cmd = cr.build_head_command("ffmpeg", tmp_path / "cover.jpg", tmp_path / "clip.mp4",
                                tmp_path / "head.mp4", PARAMS, _plan())
    assert "-loop" in cmd and "1" in cmd
    assert "-frames:v" in cmd and cmd[cmd.index("-frames:v") + 1] == "20"
    assert cmd[cmd.index("-t") + 1] == "0.800000"
    # 有音轨 → 取原片头对应时段（-ss 0 -t 0.8 -i source），不是静音
    assert "-ss" in cmd and cmd[cmd.index("-ss") + 1] == "0"
    assert "anullsrc" not in " ".join(cmd)
    assert cmd[cmd.index("-c:a") + 1] == "aac"
    assert cmd[cmd.index("-ar") + 1] == "44100"
    assert cmd[cmd.index("-ac") + 1] == "1"
    # 编码参数必须与原片一致（profile/level/pix_fmt/fps），否则 concat copy 会错位
    assert cmd[cmd.index("-profile:v") + 1] == "high"
    assert cmd[cmd.index("-level:v") + 1] == "4.0"
    assert cmd[cmd.index("-pix_fmt") + 1] == "yuv420p"
    assert cmd[cmd.index("-r") + 1] == "25"
    assert cmd[-1].endswith("head.mp4")


def test_head_command_uses_silence_when_source_has_no_audio(tmp_path):
    params = dict(PARAMS, has_audio=False, acodec=None, channel_layout="mono")
    cmd = cr.build_head_command("ffmpeg", tmp_path / "cover.jpg", tmp_path / "clip.mp4",
                                tmp_path / "head.mp4", params, _plan())
    assert "anullsrc=r=44100:cl=mono" in cmd
    assert "-frames:v" in cmd and cmd[cmd.index("-frames:v") + 1] == "20"


def test_head_command_includes_fade_in(tmp_path):
    cmd = cr.build_head_command("ffmpeg", tmp_path / "cover.jpg", tmp_path / "clip.mp4",
                                tmp_path / "head.mp4", PARAMS, _plan(), fade_in_s=0.18)
    joined = " ".join(cmd)
    assert "fade=t=in:st=0:d=0.18" in joined


def test_bridge_command_length_matches_gap(tmp_path):
    plan = cr.plan_intro_split(25.0, 427, KF, 17)
    cmd = cr.build_bridge_command("ffmpeg", tmp_path / "clip.mp4", tmp_path / "bridge.mp4",
                                  PARAMS, plan)
    assert cmd[cmd.index("-ss") + 1] == "0.680000"       # 17/25
    assert cmd[cmd.index("-frames:v") + 1] == "3"


def test_tail_command_stream_copies_from_real_keyframe_time(tmp_path):
    plan = _plan()
    cmd = cr.build_tail_command("ffmpeg", tmp_path / "clip.mp4", tmp_path / "tail.mp4",
                                PARAMS, plan)
    assert cmd[cmd.index("-ss") + 1] == "0.822969"       # 真实 pts，保证无重复帧
    assert "-c" in cmd and cmd[cmd.index("-c") + 1] == "copy"


def test_tail_command_adds_silence_but_copies_video_when_no_audio(tmp_path):
    params = dict(PARAMS, has_audio=False)
    cmd = cr.build_tail_command("ffmpeg", tmp_path / "clip.mp4", tmp_path / "tail.mp4",
                                PARAMS | {"has_audio": False}, _plan())
    assert cmd[cmd.index("-c:v") + 1] == "copy"          # 视频帧仍然逐位一致
    assert "anullsrc" in " ".join(cmd)


def test_tail_command_reencodes_only_when_no_keyframe_exists(tmp_path):
    plan = cr.plan_intro_split(25.0, 100, [{"frame": 0, "time_s": 0.0}], 20)
    cmd = cr.build_tail_command("ffmpeg", tmp_path / "clip.mp4", tmp_path / "tail.mp4",
                                PARAMS, plan)
    assert "-c" not in cmd or cmd[cmd.index("-c") + 1] != "copy"
    assert cmd[cmd.index("-ss") + 1] == "0.800000"


def test_concat_command_is_stream_copy(tmp_path):
    cmd = cr.build_concat_command("ffmpeg", tmp_path / "concat.txt", tmp_path / "merged.mp4")
    assert cmd[cmd.index("-f") + 1] == "concat"
    assert cmd[cmd.index("-c") + 1] == "copy"
    assert cmd[-1].endswith("merged.mp4")


@pytest.mark.parametrize("profile,expected", [
    ("High", "high"), ("Main", "main"), ("Baseline", "baseline"), ("High 10", "high10"),
    (None, "high"),
])
def test_x264_profile_mapping(profile, expected):
    assert cr._x264_profile(profile) == expected


# ---------------------------------------------------------------------------
# 4. 二进制解析
# ---------------------------------------------------------------------------

def test_resolve_binary_prefers_env_override(tmp_path, monkeypatch):
    fake = tmp_path / "myffmpeg"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("STUDIO_FFMPEG", str(fake))
    assert cr.resolve_binary("STUDIO_FFMPEG", "ffmpeg", ()) == str(fake)


def test_resolve_binary_reports_missing_with_code(monkeypatch, tmp_path):
    monkeypatch.delenv("STUDIO_FFMPEG_UNIT", raising=False)
    monkeypatch.setattr(cr.shutil, "which", lambda name: None)
    with pytest.raises(cr.CoverReplaceError) as excinfo:
        cr.resolve_binary("STUDIO_FFMPEG_UNIT", "ffmpeg-nope",
                          (str(tmp_path / "absent"),))
    assert excinfo.value.code == cr.E_COVER_FFMPEG_MISSING
    assert "工具与帮助" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 5. 备份 / 幂等 / 回滚（打桩 ffprobe 与渲染）
# ---------------------------------------------------------------------------

def _fake_probe(path: Path, **_kwargs) -> dict:
    return {"path": str(path), "fps": 25.0, "frames": 427, "duration_s": 17.08,
            "width": 1080, "height": 1920, "pix_fmt": "yuv420p", "vcodec": "h264",
            "profile": "High", "level": 40, "has_audio": True, "acodec": "aac",
            "sample_rate": 44100, "channels": 1, "channel_layout": "mono",
            "audio_bit_rate": 128000}


@pytest.fixture()
def clip_env(tmp_path, monkeypatch):
    """造一个假任务目录并把 TASKS_ROOT 指过去（不碰真实成片）。"""
    root = tmp_path / "tasks"
    clip_dir = root / "output" / "autoedit_0123456789ab" / "clip-01"
    clip_dir.mkdir(parents=True)
    (clip_dir / "clip_final.mp4").write_bytes(b"ORIGINAL-VIDEO" * 100)
    (clip_dir / "cover.jpg").write_bytes(b"ORIGINAL-COVER")
    plan = root / "output" / "autoedit_0123456789ab" / "clip_plan.json"
    plan.write_text(json.dumps({
        "video_brain_task_id": "task-abc",
        "highlights": [{"clip_id": "clip-01", "start": 0.14, "end": 16.3}],
    }), encoding="utf-8")
    monkeypatch.setattr(bridge, "TASKS_ROOT", root)
    monkeypatch.setattr(cr, "probe_streams", _fake_probe)
    return clip_dir


def test_ensure_baseline_creates_timestamped_backup_and_is_reused(clip_env):
    state = cr._load_state(clip_env)
    baseline = cr._ensure_baseline(clip_env, state)
    backups = sorted(p.name for p in (clip_env / cr.COVER_BACKUP_DIRNAME).iterdir())
    assert len(backups) == 2
    assert any(name.startswith("cover.jpg.") and name.endswith(".bak") for name in backups)
    assert any(name.startswith("clip_final.mp4.") and name.endswith(".bak") for name in backups)
    assert baseline["frames"] == 427 and baseline["has_audio"] is True
    assert (clip_env / cr.COVER_STATE_FILENAME).is_file()
    # 第二次调用复用同一条基线，不叠加备份
    again = cr._ensure_baseline(clip_env, cr._load_state(clip_env))
    assert again["cover_jpg"] == baseline["cover_jpg"]
    assert len(list((clip_env / cr.COVER_BACKUP_DIRNAME).iterdir())) == 2


def test_restore_is_idempotent_and_verifies_hashes(clip_env):
    state = cr._load_state(clip_env)
    cr._ensure_baseline(clip_env, state)
    # 模拟一次替换：改掉封面与成片
    (clip_env / cr.COVER_FILENAME).write_bytes(b"NEW-COVER")
    (clip_env / cr.CLIP_FILENAME).write_bytes(b"NEW-VIDEO")
    first = cr.restore_cover("autoedit_0123456789ab", "clip-01")
    assert first["kind"] == "restore"
    assert (clip_env / cr.COVER_FILENAME).read_bytes() == b"ORIGINAL-COVER"
    assert (clip_env / cr.CLIP_FILENAME).read_bytes() == b"ORIGINAL-VIDEO" * 100
    # 再回滚一次同样成功（不叠加备份、结果一致）
    second = cr.restore_cover("autoedit_0123456789ab", "clip-01")
    assert second["qc"]["restore_verification"]["restored"] is True
    assert len(list((clip_env / cr.COVER_BACKUP_DIRNAME).iterdir())) == 2


def test_restore_without_baseline_reports_code(clip_env):
    with pytest.raises(cr.CoverReplaceError) as excinfo:
        cr.restore_cover("autoedit_0123456789ab", "clip-01")
    assert excinfo.value.code == cr.E_COVER_BACKUP_MISSING


def test_missing_clip_reports_code(clip_env):
    with pytest.raises(cr.CoverReplaceError) as excinfo:
        cr.replace_cover("autoedit_0123456789ab", "clip-99", "realframe_v2")
    assert excinfo.value.code == cr.E_COVER_CLIP_MISSING


def test_bad_clip_id_is_rejected(clip_env):
    with pytest.raises(cr.CoverReplaceError) as excinfo:
        cr.replace_cover("autoedit_0123456789ab", "../etc/passwd", "realframe_v2")
    assert excinfo.value.code == cr.E_COVER_BAD_REQUEST


def test_is_noop_detects_unchanged_result(clip_env, monkeypatch):
    state = cr._load_state(clip_env)
    cr._ensure_baseline(clip_env, state)
    record = {"key": "generated:realframe_v2", "replace_intro": True,
              "result_sha256": cr._current_hashes(clip_env)}
    state["records"] = [record]
    assert cr._is_noop(state, "generated:realframe_v2", True, clip_env) is record
    # 参数不同（replace_intro 变了）→ 不算幂等
    assert cr._is_noop(state, "generated:realframe_v2", False, clip_env) is None
    # key 不同 → 不算幂等
    assert cr._is_noop(state, "generated:cutout_v3", True, clip_env) is None
    # 产物被外部改动 → 不算幂等
    (clip_env / cr.COVER_FILENAME).write_bytes(b"CHANGED")
    assert cr._is_noop(state, "generated:realframe_v2", True, clip_env) is None


def test_replace_cover_idempotent_does_not_add_backup(clip_env, monkeypatch):
    """同一 (task, clip, style) 重复执行：第二次为 noop，不叠加备份。"""
    rendered = clip_env / "rendered-cover.jpg"
    rendered.write_bytes(b"RENDERED-COVER")
    monkeypatch.setattr(cr, "_render_cover_via_video_brain",
                        lambda *a, **k: {"cover_path": rendered, "layout_path": clip_env / "nope.json",
                                         "layout": {}, "video_brain_task_id": "task-abc",
                                         "frame_ts_in_base": 8.08, "base_video": "main_base.mp4"})
    first = cr.replace_cover("autoedit_0123456789ab", "clip-01", "realframe_v2",
                            replace_intro=False)
    assert first["idempotent"] is False
    assert (clip_env / cr.COVER_FILENAME).read_bytes() == b"RENDERED-COVER"
    assert len(list((clip_env / cr.COVER_BACKUP_DIRNAME).iterdir())) == 2
    second = cr.replace_cover("autoedit_0123456789ab", "clip-01", "realframe_v2",
                             replace_intro=False)
    assert second["idempotent"] is True
    assert len(list((clip_env / cr.COVER_BACKUP_DIRNAME).iterdir())) == 2
    assert second["qc"] == first["qc"]


def test_replace_cover_rolls_back_when_intro_splice_fails(clip_env, monkeypatch):
    rendered = clip_env / "rendered-cover.jpg"
    rendered.write_bytes(b"RENDERED-COVER")
    monkeypatch.setattr(cr, "_render_cover_via_video_brain",
                        lambda *a, **k: {"cover_path": rendered, "layout_path": clip_env / "nope.json",
                                         "layout": {}, "video_brain_task_id": "task-abc",
                                         "frame_ts_in_base": 8.08, "base_video": "main_base.mp4"})

    def boom(*_args, **_kwargs):
        raise cr.CoverReplaceError(cr.E_COVER_QC_FAILED, "帧数不一致")

    monkeypatch.setattr(cr, "splice_intro", boom)
    with pytest.raises(cr.CoverReplaceError) as excinfo:
        cr.replace_cover("autoedit_0123456789ab", "clip-01", "realframe_v2")
    assert excinfo.value.code == cr.E_COVER_QC_FAILED
    # 自动回滚：封面与成片都回到原状
    assert (clip_env / cr.COVER_FILENAME).read_bytes() == b"ORIGINAL-COVER"
    assert (clip_env / cr.CLIP_FILENAME).read_bytes() == b"ORIGINAL-VIDEO" * 100


def test_adopt_cover_reuses_existing_jpg_and_is_idempotent(clip_env):
    source_dir = clip_env.parent / "clip-02"
    source_dir.mkdir()
    (source_dir / cr.CLIP_FILENAME).write_bytes(b"SOURCE-VIDEO")
    (source_dir / cr.COVER_FILENAME).write_bytes(b"SOURCE-COVER")
    task_id = "autoedit_0123456789ab"
    first = cr.adopt_cover(task_id, "clip-01", task_id, "clip-02")
    assert first["kind"] == "adopted"
    assert first["source"]["type"] == "adopted"
    assert first["source"]["task_id"] == task_id
    assert first["source"]["clip_id"] == "clip-02"
    assert first["source"]["cover_sha256"]          # 记录来源封面指纹，便于审计
    assert first["replace_intro"] is False
    assert (clip_env / cr.COVER_FILENAME).read_bytes() == b"SOURCE-COVER"
    # 默认不改片头
    assert (clip_env / cr.CLIP_FILENAME).read_bytes() == b"ORIGINAL-VIDEO" * 100
    second = cr.adopt_cover(task_id, "clip-01", task_id, "clip-02")
    assert second["idempotent"] is True
    assert len(list((clip_env / cr.COVER_BACKUP_DIRNAME).iterdir())) == 2


def test_adopt_cover_missing_source_cover_reports_code(clip_env):
    source_dir = clip_env.parent / "clip-03"
    source_dir.mkdir()
    (source_dir / cr.CLIP_FILENAME).write_bytes(b"SOURCE-VIDEO")
    with pytest.raises(cr.CoverReplaceError) as excinfo:
        cr.adopt_cover("autoedit_0123456789ab", "clip-01",
                       "autoedit_0123456789ab", "clip-03")
    assert excinfo.value.code == cr.E_COVER_SOURCE_MISSING


def test_read_cover_state_reports_restore_availability(clip_env):
    task_id = "autoedit_0123456789ab"
    state = cr.read_cover_state(task_id, "clip-01")
    assert state["has_cover"] is True
    assert state["can_restore"] is False
    assert state["default_style_id"] == "realframe_v2"
    assert set(state["supported_styles"]) == {"realframe_v2", "cutout_v3"}
    cr._ensure_baseline(clip_env, cr._load_state(clip_env))
    assert cr.read_cover_state(task_id, "clip-01")["can_restore"] is True


def test_render_cover_via_video_brain_maps_cutout_error(clip_env, monkeypatch):
    """video-brain 报 E_CUTOUT_* → 工作台映射为 E_COVER_STYLE_UNAVAILABLE（不静默降级）。"""
    monkeypatch.setattr(cr, "_video_brain_task_id", lambda *a, **k: "task-abc")
    monkeypatch.setattr(cr, "_resolve_cover_copy", lambda *a, **k: ({"top_theme": "x"}, 1.0))
    monkeypatch.setattr(cr, "_clip_start_seconds", lambda *a, **k: 0.0)
    monkeypatch.setattr(cr, "_http_json", lambda *a, **k: (
        422, {"detail": "E_CUTOUT_WEIGHTS_MISSING: u2net 权重未就绪 ... 预置到 ~/.u2net/"}))
    with pytest.raises(cr.CoverReplaceError) as excinfo:
        cr._render_cover_via_video_brain("autoedit_0123456789ab", "clip-01", clip_env,
                                         "cutout_v3")
    assert excinfo.value.code == cr.E_COVER_STYLE_UNAVAILABLE
    assert "E_CUTOUT_WEIGHTS_MISSING" in str(excinfo.value)


def test_render_cover_via_video_brain_maps_unreachable_service(clip_env, monkeypatch):
    import urllib.error

    def boom(*_args, **_kwargs):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(cr, "_video_brain_task_id", lambda *a, **k: "task-abc")
    monkeypatch.setattr(cr.urllib.request, "urlopen", boom)
    with pytest.raises(cr.CoverReplaceError) as excinfo:
        cr._http_json("POST", "/v1/tools/cover.render", {})
    assert excinfo.value.code == cr.E_COVER_VB_UNAVAILABLE


# ---------------------------------------------------------------------------
# 6. QC 断言
# ---------------------------------------------------------------------------

def _run_intro_qc(monkeypatch, tmp_path, *, after_frames=427, after_duration=17.08,
                  after_audio=True, first_iframe=0, similarity=0.99, tail_mismatch=0):
    plan = _plan()
    before = _fake_probe(tmp_path / "before.mp4")
    after = dict(before, frames=after_frames, duration_s=after_duration,
                 has_audio=after_audio)
    monkeypatch.setattr(cr, "keyframe_list",
                        lambda path, limit=600: [{"frame": first_iframe, "time_s": 0.0}])
    monkeypatch.setattr(cr, "frame_similarity_to_image",
                        lambda *a, **k: similarity)
    monkeypatch.setattr(cr, "frame_md5_list",
                        lambda path, start_time_s=0.0, limit=0: ["a"] * 5)
    if tail_mismatch:
        monkeypatch.setattr(cr, "frame_md5_list",
                            lambda path, start_time_s=0.0, limit=0:
                            (["a"] * 5 if "merged" not in str(path) else ["b"] * 5))
    return cr.intro_qc(tmp_path / "before.mp4", tmp_path / "merged.mp4",
                       tmp_path / "cover.jpg", before, after, plan, tmp_path)


def test_intro_qc_passes_on_matching_media(monkeypatch, tmp_path):
    qc = _run_intro_qc(monkeypatch, tmp_path)
    assert qc["passed"] is True, qc["failures"]
    names = {check["check"] for check in qc["checks"]}
    assert {"total_frames_unchanged", "duration_unchanged", "audio_stream_preserved",
            "first_frame_is_iframe", "intro_frame_matches_new_cover",
            "tail_frames_bit_identical", "cover_text_window_within_intro"} <= names


@pytest.mark.parametrize("kwargs,expected_check", [
    ({"after_frames": 460}, "total_frames_unchanged"),
    ({"after_duration": 19.5}, "duration_unchanged"),
    ({"after_audio": False}, "audio_stream_preserved"),
    ({"first_iframe": 3}, "first_frame_is_iframe"),
    ({"similarity": 0.4}, "intro_frame_matches_new_cover"),
    ({"tail_mismatch": 1}, "tail_frames_bit_identical"),
])
def test_intro_qc_fails_and_names_the_check(monkeypatch, tmp_path, kwargs, expected_check):
    qc = _run_intro_qc(monkeypatch, tmp_path, **kwargs)
    assert qc["passed"] is False
    failures = " ".join(qc["failures"])
    assert expected_check in failures


def test_intro_qc_allows_one_frame_drift(monkeypatch, tmp_path):
    """±1 帧容差：427 → 428 必须通过（任务书要求 ±1 帧）。"""
    qc = _run_intro_qc(monkeypatch, tmp_path, after_frames=428, after_duration=17.12)
    assert qc["passed"] is True, qc["failures"]


@pytest.mark.parametrize("output,expected", [
    ("[Parsed_ssim_4 @ 0x1] SSIM Y:0.992164 (21.059068) All:0.992164 (21.059068)", 0.992164),
    ("SSIM All:0.907776 (10.3)", 0.907776),
    ("SSIM All: 0.5", 0.5),
    ("nothing here", None),
    ("", None),
])
def test_parse_ssim(output, expected):
    """相似度解析只认 ffmpeg ssim 日志里的 All 分数（不依赖 numpy/PIL）。"""
    assert cr._parse_ssim(output) == expected


def test_frame_similarity_uses_ssim_without_numpy(monkeypatch, tmp_path):
    """封面相似度走 ffmpeg ssim 过滤器：断言命令形态并解析出分数。"""
    recorded: list[list[str]] = []

    class _Done:
        returncode = 0
        stdout = ""
        stderr = "SSIM Y:0.99 (20.0) All:0.9922 (21.0)"

    def fake_run(command, timeout, ffmpeg_env=None):
        recorded.append(command)
        if command[-1].endswith(".png") and "-ss" in command:
            Path(command[-1]).write_bytes(b"png")
        return _Done()

    monkeypatch.setattr(cr, "resolve_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(cr, "_run", fake_run)
    score = cr.frame_similarity_to_image(tmp_path / "v.mp4", 0.2, tmp_path / "c.jpg", tmp_path)
    assert score == 0.9922
    joined = " ".join(recorded[-1])
    assert "ssim" in joined and "scale=1080:1920" in joined and "format=gray" in joined


def test_restore_removes_layout_json_created_by_channel(clip_env):
    """基线里没有 cover_layout.json（原成片目录本就没有）→ 回滚必须把它删掉。

    否则会留下「layout 说自己是大字风格、cover.jpg 却已回到旧封面」的自相矛盾状态。
    """
    state = cr._load_state(clip_env)
    cr._ensure_baseline(clip_env, state)
    assert state["baseline"]["cover_layout"] is None
    (clip_env / cr.COVER_LAYOUT_FILENAME).write_text('{"style_id": "realframe_v2"}', encoding="utf-8")
    cr.restore_cover("autoedit_0123456789ab", "clip-01")
    assert not (clip_env / cr.COVER_LAYOUT_FILENAME).exists()


def test_restore_puts_back_preexisting_layout_json(clip_env):
    """基线里本来就有 cover_layout.json → 回滚还原它的内容（不是删掉）。"""
    layout = clip_env / cr.COVER_LAYOUT_FILENAME
    layout.write_text('{"style_id": "medical_big_text_v3", "original": true}', encoding="utf-8")
    state = cr._load_state(clip_env)
    baseline = cr._ensure_baseline(clip_env, state)
    assert baseline["cover_layout"] and (clip_env / cr.COVER_BACKUP_DIRNAME / baseline["cover_layout"]).is_file()
    layout.write_text('{"style_id": "realframe_v2"}', encoding="utf-8")
    cr.restore_cover("autoedit_0123456789ab", "clip-01")
    assert "original" in layout.read_text(encoding="utf-8")
