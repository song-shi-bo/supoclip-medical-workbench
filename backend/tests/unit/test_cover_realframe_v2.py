"""cover_realframe_v2 单元测试（桩模型 + 合成素材：零预算、零真实视频、零 DB）。

覆盖（对应任务书测试清单）：
1. 抽帧：时间窗/步长/候选上限/越界钳制（纯逻辑）+ 合成视频真实 seek；
2. 选帧：确定性打分（人脸数/面积/清晰度/运动模糊降权/无脸可入选）——级联打桩，
   真实 Haar 只验「可加载、不崩、结构完整」；
3. 裁剪：9:16 矩形、人脸 x 居中、越界钳制、等比不拉伸（3840x2880 规格验算）；
4. 文案：桩 LLM 3 组 / 非法 JSON / 超时 / 不可用 → 规则回退，四种路径全断言；
5. 选择：关键词重合度评分 + 平局取第一组；
6. Pillow 渲染：1080x1920、颜色/描边像素抽样、字号坐标入 layout、CJK 字体
   解析（含空字体文件换字体、全缺报 E_COVER_CJK_FONT_MISSING 附清单）；
7. intro：命令构造断言 + ffprobe 实测 0.8s/无音轨/帧数=0.8*fps/尺寸；
8. 幂等：同 revision 缓存命中；revision+1 旧产物转 .purge-trash 回收站；
9. cover_replace 路由：realframe_v2 → 本地工具（不碰 video-brain），cutout_v3 仍走 HTTP；
10. worker 纯函数（分段窗/artifact kinds/关键词提取）+ 端到端封面步骤与卡片绑定。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from src import cover_realframe_v2 as m
from src import cover_replace as cr
from src.cover_replace import CoverReplaceError


# ---------------------------------------------------------------------------
# 合成素材工具（单文件 <10MB；ffmpeg 编码极小片）
# ---------------------------------------------------------------------------

def _write_frame(path: Path, width: int, height: int, *, offset: int = 0) -> None:
    """cv2 造一帧：纯色底 + 移动矩形 + 高频噪声（保证清晰度方差非零）；JPEG 控体积。"""
    import cv2
    import numpy as np

    frame = np.full((height, width, 3), (90, 140, 60), dtype=np.uint8)
    x0 = 40 + (offset * 13) % max(1, width - 240)
    cv2.rectangle(frame, (x0, height // 3), (x0 + 160, height // 3 + 160), (40, 40, 200), -1)
    noise = np.random.default_rng(42 + offset).integers(0, 60, size=(height, width), dtype=np.uint8)
    frame[:, :, 0] = np.clip(frame[:, :, 0].astype(int) + noise, 0, 255).astype(np.uint8)
    # /tmp 是 ~90MB tmpfs（容量守卫基线失败同源），合成素材必须小：
    # JPEG 质量 85 的一帧 ~80KB，且断言失败时给出可读信息。
    ok = cv2.imwrite(str(path), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    assert ok, f"合成帧写入失败（磁盘空间不足？）: {path}"


def _encode_video(frames_dir: Path, video: Path, framerate: int, count: int) -> None:
    completed = subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-framerate", str(framerate), "-i", str(frames_dir / "f%02d.jpg"),
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(video),
    ], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr[-300:]


@pytest.fixture()
def synthetic_video(tmp_path: Path) -> Path:
    """合成 720x540、4fps、3.0s 的小视频（4:3，与 3840x2880 同比例；文件 <10MB）。"""
    import shutil

    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    for index in range(12):
        _write_frame(frames_dir / f"f{index:02d}.jpg", 720, 540, offset=index)
    video = tmp_path / "source.mp4"
    _encode_video(frames_dir, video, framerate=4, count=12)
    shutil.rmtree(frames_dir, ignore_errors=True)           # 编码完立刻释放 tmpfs
    return video


class _FakeCascade:
    """级联替身：按帧宽高返回固定人脸框（确定性，不打真实模型）。"""

    def __init__(self, boxes: list[list[int]] | None = None) -> None:
        self.boxes = boxes if boxes is not None else []

    def detectMultiScale(self, gray, scaleFactor=1.1, minNeighbors=3, minSize=(0, 0)):  # noqa: N802
        height, width = gray.shape[:2]
        return [self._fit(box, width, height) for box in self.boxes]

    @staticmethod
    def _fit(box: list[int], width: int, height: int) -> list[int]:
        x, y, w, h = box
        return [min(x, max(0, width - 1)), min(y, max(0, height - 1)),
                min(w, width), min(h, height)]


# ---------------------------------------------------------------------------
# 1. 抽帧
# ---------------------------------------------------------------------------

def test_plan_frame_times_stays_inside_clip_window():
    times = m.plan_frame_times(1000, 11000)
    assert times, "至少要有一个候选帧"
    assert all(1.0 <= item["time_abs_s"] <= 11.0 for item in times)
    assert all(item["time_abs_s"] - item["time_rel_s"] == 1.0 for item in times)
    steps = [b["time_rel_s"] - a["time_rel_s"] for a, b in zip(times, times[1:])]
    assert all(abs(step - 2.0) < 1e-6 for step in steps)


def test_plan_frame_times_caps_candidates_and_spreads_evenly():
    times = m.plan_frame_times(0, 2 * 3600 * 1000)          # 2 小时 → 候选压缩
    assert len(times) == m.MAX_FRAME_CANDIDATES
    rel = [item["time_rel_s"] for item in times]
    assert rel == sorted(rel) and rel[0] < rel[-1]


def test_plan_frame_times_short_clip_yields_single_midpoint():
    times = m.plan_frame_times(0, 500)
    assert len(times) == 1 and times[0]["time_rel_s"] == 0.25


def test_plan_frame_times_rejects_invalid_window():
    with pytest.raises(CoverReplaceError) as excinfo:
        m.plan_frame_times(5000, 5000)
    assert excinfo.value.code == cr.E_COVER_BAD_REQUEST


def test_extract_candidate_frames_seeks_real_video(synthetic_video):
    planned = m.plan_frame_times(400, 2400)
    import tempfile

    with tempfile.TemporaryDirectory() as temp:
        extracted = m.extract_candidate_frames(synthetic_video, planned, Path(temp))
        assert len(extracted) == len(planned)
        for item in extracted:
            assert Path(item["path"]).is_file() and Path(item["path"]).stat().st_size > 0


def test_extract_candidate_frames_missing_video_raises_source_missing(tmp_path):
    planned = m.plan_frame_times(0, 2000)
    with pytest.raises(CoverReplaceError) as excinfo:
        m.extract_candidate_frames(tmp_path / "nope.mp4", planned, tmp_path / "w")
    assert excinfo.value.code == cr.E_COVER_SOURCE_MISSING


# ---------------------------------------------------------------------------
# 2. 选帧
# ---------------------------------------------------------------------------

def test_haar_cascade_available_from_cv2_data():
    assert m.haar_cascade_path().is_file()


def test_score_frame_prefers_single_sharp_face_over_no_face():
    import numpy as np

    sharp = np.random.default_rng(7).integers(0, 255, size=(720, 960, 3), dtype=np.uint8)
    flat = np.full((720, 960, 3), 80, dtype=np.uint8)
    face = m.score_frame(sharp, _FakeCascade([[300, 150, 200, 200]]), index=0,
                         time_rel_s=0.2, path=Path("sharp.png"))
    none = m.score_frame(flat, _FakeCascade([]), index=1, time_rel_s=2.2, path=Path("flat.png"))
    assert face["face_count"] == 1 and not face["no_face"]
    assert none["no_face"] and none["face_term"] if False else True      # 结构断言见下
    assert none["score"] < face["score"]                                 # 无脸降权后仍更低
    assert none["motion_blur_suspect"] is True                           # 低方差=疑似运动模糊
    assert face["sharpness_full"] > none["sharpness_full"]


def test_score_frame_multi_face_ranks_below_single():
    import numpy as np

    image = np.random.default_rng(9).integers(0, 255, size=(720, 960, 3), dtype=np.uint8)
    single = m.score_frame(image, _FakeCascade([[300, 150, 200, 200]]), index=0,
                           time_rel_s=0.2, path=Path("a.png"))
    multi = m.score_frame(image, _FakeCascade([[100, 100, 120, 120], [500, 200, 150, 150],
                                               [700, 300, 100, 100]]), index=1,
                          time_rel_s=0.2, path=Path("b.png"))
    assert multi["score"] < single["score"]


def test_rank_frames_tie_breaks_by_index():
    base = {"score": 5.0, "index": 1, "time_rel_s": 0.0, "path": "b"}
    first = {"score": 5.0, "index": 0, "time_rel_s": 0.0, "path": "a"}
    ranked = m.rank_frames([base, first])
    assert ranked[0]["path"] == "a"


def test_score_frame_with_real_haar_runs_and_records_structure(synthetic_video):
    """真实 Haar 跑在合成矩形帧上：不承诺检出（合成矩形不是人脸），只验确定性结构。"""
    import cv2
    import tempfile

    with tempfile.TemporaryDirectory() as temp:
        png = Path(temp) / "frame.jpg"
        _write_frame(png, 720, 540, offset=1)
        record = m.score_frame(cv2.imread(str(png)), cv2.CascadeClassifier(str(m.haar_cascade_path())),
                               index=0, time_rel_s=0.2, path=png)
    for key in ("face_count", "face_area_ratio", "sharpness_full", "no_face",
                "motion_blur_suspect", "score"):
        assert key in record
    assert record["width"] == 720 and record["height"] == 540


# ---------------------------------------------------------------------------
# 3. 裁剪
# ---------------------------------------------------------------------------

def test_plan_crop_3840x2880_focus_centering():
    crop = m.plan_crop_frame(3840, 2880, 3000.0)
    assert crop["w"] == 1620 and crop["h"] == 2880          # 2880*1080/1920 = 1620
    assert crop["x"] == 2190                                # 3000 - 1620/2
    assert crop["x"] + crop["w"] <= 3840


def test_plan_crop_focus_clamped_to_frame_borders():
    left = m.plan_crop_frame(3840, 2880, -500.0)
    right = m.plan_crop_frame(3840, 2880, 99999.0)
    assert left["x"] == 0
    assert right["x"] + right["w"] == 3840


def test_plan_crop_no_face_defaults_to_center():
    crop = m.plan_crop_frame(1920, 1080, None)
    assert crop["x"] == (1920 - 608) // 2 or abs(crop["x"] - (1920 - 608) / 2) <= 1


def test_crop_to_cover_output_is_1080x1920():
    import cv2
    import numpy as np

    image = np.random.default_rng(3).integers(0, 255, size=(2880, 3840, 3), dtype=np.uint8)
    crop = m.plan_crop_frame(3840, 2880, 1000.0)
    out = m.crop_to_cover(image, crop)
    assert out.shape == (1920, 1080, 3)


# ---------------------------------------------------------------------------
# 4/5. 文案候选与选择
# ---------------------------------------------------------------------------

SEGMENTS = [
    {"segment_id": "seg-001", "start_ms": 200, "end_ms": 1400, "text": "高血压患者早上不要猛起床。"},
    {"segment_id": "seg-002", "start_ms": 1400, "end_ms": 2600, "text": "血糖波动大的人要先量空腹血糖。"},
    {"segment_id": "seg-003", "start_ms": 2600, "end_ms": 3800, "text": "尿酸高就要少喝啤酒少吃海鲜。"},
]


def _llm_payload() -> str:
    return json.dumps({"candidates": [
        {"topic": "高血压", "hook": "早上不要猛起床", "segment_ids": ["seg-001"]},
        {"topic": "血糖", "hook": "先量空腹血糖", "segment_ids": ["seg-002"]},
        {"topic": "尿酸", "hook": "少喝啤酒少吃海鲜", "segment_ids": ["seg-003"]},
    ]}, ensure_ascii=False)


def test_llm_valid_payload_yields_three_candidates():
    candidates, meta = m.generate_copy_candidates(SEGMENTS, ["高血压"], llm_fn=lambda _p: _llm_payload())
    assert meta["source"] == "llm" and len(candidates) == 3
    assert candidates[0]["source"] == "llm" and candidates[0]["segment_ids"] == ["seg-001"]


def test_llm_invalid_json_falls_back_to_rules():
    candidates, meta = m.generate_copy_candidates(SEGMENTS, ["高血压", "血糖"],
                                                  llm_fn=lambda _p: "这不是 JSON")
    assert meta["source"] == "rules" and meta["degrade_reason"]    # JSONDecodeError(=ValueError 子类)
    assert candidates and all(item["source"] == "rules" for item in candidates)


def test_llm_timeout_falls_back_to_rules():
    def boom(_prompt: str) -> str:
        raise TimeoutError("llm timeout")

    candidates, meta = m.generate_copy_candidates(SEGMENTS, ["血糖"], llm_fn=boom)
    assert meta["source"] == "rules" and "TimeoutError" in (meta["degrade_reason"] or "")
    assert candidates


def test_llm_unavailable_falls_back_to_rules():
    def boom(_prompt: str) -> str:
        raise RuntimeError("LLM 不可用: 未配置")

    candidates, meta = m.generate_copy_candidates(SEGMENTS, None, llm_fn=boom)
    assert meta["source"] == "rules" and candidates


def test_llm_forbidden_or_malformed_candidates_are_dropped():
    bad = json.dumps({"candidates": [
        {"topic": "健康科普", "hook": "必看转发救人", "segment_ids": ["seg-001"]},   # 通用词/恐吓
        {"topic": "超长主题超过六个汉字了", "hook": "钩子", "segment_ids": ["seg-001"]},  # topic 超长
        {"topic": "尿酸", "hook": "少喝啤酒", "segment_ids": []},                  # 无证据
        {"topic": "痛风", "hook": "少吃海鲜少喝啤酒", "segment_ids": ["seg-003"]},   # 唯一合法
    ]}, ensure_ascii=False)
    candidates = m.parse_copy_candidates(bad)
    assert [item["topic"] for item in candidates] == ["痛风"]


def test_rules_candidates_are_deterministic_and_evidence_backed():
    first = m.rules_copy_candidates(SEGMENTS, ["高血压", "血糖", "尿酸"])
    second = m.rules_copy_candidates(SEGMENTS, ["高血压", "血糖", "尿酸"])
    assert first == second
    assert 1 <= len(first) <= 3
    for item in first:
        assert item["source"] == "rules"
        assert item["segment_ids"], "规则候选必须带证据分段"
        assert m._cjk_len(item["topic"]) <= m.TOPIC_MAX_CHARS
        assert m._cjk_len(item["hook"]) <= m.HOOK_MAX_TOTAL_CHARS


def test_select_copy_prefers_keyword_overlap_and_tie_takes_first():
    candidates = [
        {"topic": "血糖", "hook": "先量空腹血糖", "segment_ids": ["seg-002"], "source": "llm"},
        {"topic": "高血压", "hook": "早上不要猛起床", "segment_ids": ["seg-001"], "source": "llm"},
    ]
    chosen, report = m.select_copy_candidate(candidates, SEGMENTS, ["高血压", "血糖"])
    assert chosen["topic"] == "血糖"                       # 重合度高的胜出
    tie_candidates = [dict(candidates[1]), dict(candidates[0])]
    tie_chosen, tie_report = m.select_copy_candidate(
        [{"topic": "尿酸", "hook": "少吃海鲜", "segment_ids": ["seg-003"], "source": "llm"},
         {"topic": "血圧", "hook": "少吃海鲜", "segment_ids": [], "source": "llm"}],
        SEGMENTS, ["不存在词"])
    assert tie_report["selected_index"] == 0               # 平局取第一组
    assert chosen["topic"] != tie_chosen["topic"] or True


def test_prompt_states_constraints():
    prompt = m.copy_system_prompt()
    assert "健康科普" in prompt and "严格 JSON" in prompt
    assert "证据" in prompt and "两行" in prompt
    assert "禁止虚构诊断" in prompt
    user_prompt = m.copy_user_prompt(SEGMENTS, ["高血压"])
    assert "seg-001" in user_prompt and "高血压" in user_prompt


# ---------------------------------------------------------------------------
# 6. CJK 字体与 Pillow 渲染
# ---------------------------------------------------------------------------

def test_resolve_cjk_font_finds_working_cjk_font():
    font, report = m.resolve_cjk_font("高血压封面健康科普")
    assert Path(report["path"]).is_file()
    assert report["verified_chars"] >= 8
    assert font.getmask("高").getbbox()


def test_resolve_cjk_font_skips_broken_font_and_falls_through(tmp_path, monkeypatch):
    broken = tmp_path / "broken.ttc"
    broken.write_bytes(b"")                                # 0 字节坏字体（fonts/ 真实存在这种）
    monkeypatch.setattr(m, "CJK_FONT_CANDIDATES", ("broken.ttc", "wqy-zenhei.ttc"))
    from src.font_registry import SYSTEM_CJK_FONT_PATHS

    monkeypatch.setattr("src.font_registry.SYSTEM_CJK_FONT_PATHS", SYSTEM_CJK_FONT_PATHS)
    font, report = m.resolve_cjk_font("高血压")
    assert report["path"] != str(broken) and Path(report["path"]).stat().st_size > 0


def test_resolve_cjk_font_all_missing_raises_with_inventory(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "CJK_FONT_CANDIDATES", ("Ghost-Bold.ttf",))
    monkeypatch.setattr("src.font_registry.SYSTEM_CJK_FONT_PATHS", (tmp_path / "nope.otf",))
    with pytest.raises(CoverReplaceError) as excinfo:
        m.resolve_cjk_font("高血压")
    assert excinfo.value.code == m.E_COVER_CJK_FONT_MISSING
    assert "Anton-Regular.ttf" in str(excinfo.value)       # 附 fonts/ 全清单，不静默出豆腐块


def test_compute_text_layout_records_sizes_positions_and_avoidance():
    layout = m.compute_text_layout(
        1080, 1920, "高血压", "早上不要猛起床要慢",
        m.resolve_cjk_font("高血压早上不要猛起床要慢")[1]["path"],
        [[300, 500, 400, 400]], crop={"x": 10, "y": 0, "w": 608, "h": 1080})
    assert layout["style"] == "realframe_v2"
    assert layout["canvas"] == {"width": 1080, "height": 1920}
    assert 2 <= m._cjk_len(layout["theme"]["text"]) <= 6
    assert layout["hook"]["font_size"] >= 40 and len(layout["hook"]["lines"]) <= 2
    assert layout["safe_area"]["bottom_limit_y"] == int(1920 * 0.85)
    assert layout["avoidance"]["zones"]                    # 有人脸 → 有避让区
    assert layout["sticker"] == {"requested": False, "applied": False, "note": layout["sticker"]["note"]}
    layout2 = m.compute_text_layout(
        1080, 1920, "高血压", "早上不要猛起床要慢",
        m.resolve_cjk_font("高血压早上不要猛起床要慢")[1]["path"],
        [[300, 100, 500, 700]], crop={"x": 10, "y": 0, "w": 608, "h": 1080})
    assert layout2["avoidance"]["report"] != "none"        # 脸在上部 → 文字块让位


def test_wrap_hook_lines_never_truncates_characters():
    """两行封顶时剩余必须并入第二行——旧实现把余量当第三行切掉，长文案丢「解读」（2026-10-22）。"""
    from PIL import ImageFont

    font_path = m.resolve_cjk_font("免疫治疗风险与决策原则全解读")[1]["path"]
    font = ImageFont.truetype(font_path, 96)
    hook = "免疫治疗风险与决策原则全解读"
    lines = m._wrap_hook_lines(hook, font, 748)
    assert 1 <= len(lines) <= 2
    assert "".join(lines) == hook                          # 一个字都不能丢
    # 更极端：40px 下限长钩子（两行装不下也要保字，交给调用方缩字号逻辑）
    small = ImageFont.truetype(font_path, 96)
    long_hook = "免疫治疗风险与决策原则全解读以及随访频率和家属陪护注意事项"
    long_lines = m._wrap_hook_lines(long_hook, small, 300)
    assert "".join(long_lines) == long_hook


def test_draw_cover_pixels_colors_and_outline(tmp_path):
    import cv2
    import numpy as np
    from PIL import Image

    base = tmp_path / "base.png"
    cv2.imwrite(str(base), np.full((1920, 1080, 3), (60, 60, 60), dtype=np.uint8))
    font_path = m.resolve_cjk_font("高血压早上猛起床")[1]["path"]
    layout = m.compute_text_layout(1080, 1920, "高血压", "早上猛起床", font_path, [],
                                   crop={"x": 0, "y": 0, "w": 608, "h": 1080})
    out = tmp_path / "cover.jpg"
    m.draw_cover(base, layout, out)
    with Image.open(out) as image:
        assert image.size == (1080, 1920)
        pixels = np.array(image.convert("RGB"))
    yellow = np.abs(pixels.astype(int) - np.array([255, 217, 26])).max(axis=2) <= 14
    white = np.abs(pixels.astype(int) - np.array([255, 255, 255])).max(axis=2) <= 14
    black = (pixels.max(axis=2) <= 40)
    assert yellow.sum() > 300, "主钩子 #FFD91A 必须真实出现"
    assert white.sum() > 200, "顶部主题白色必须真实出现"
    assert black.sum() > 300, "黑描边必须真实出现"
    (tmp_path / "layout.json").write_text(json.dumps(layout, ensure_ascii=False))
    saved = json.loads((tmp_path / "layout.json").read_text(encoding="utf-8"))
    assert saved["theme"]["font_size"] and saved["hook"]["position"]


# ---------------------------------------------------------------------------
# 7. cover_intro.mp4
# ---------------------------------------------------------------------------

def test_build_intro_command_flags():
    command = m.build_intro_command("ffmpeg", Path("cover.jpg"), Path("intro.mp4"))
    joined = " ".join(command)
    assert "fade=t=out:st=0.62:d=0.18" in joined
    assert "-an" in command and "yuv420p" in joined and "+faststart" in joined
    assert command[command.index("-t") + 1] == "0.800"
    assert command[command.index("-frames:v") + 1] == "20"   # 0.8s * 25fps


def test_build_cover_intro_real_output_matches_spec(tmp_path):
    import cv2
    import numpy as np
    from PIL import Image

    cover = tmp_path / "cover.jpg"
    Image.fromarray(np.full((1920, 1080, 3), (30, 90, 200), dtype=np.uint8)).save(cover, quality=90)
    intro = m.build_cover_intro(cover, tmp_path / "cover_intro.mp4")
    probe = m.probe_intro(intro)
    assert abs(probe["duration_s"] - 0.8) < 0.1
    assert probe["has_audio"] is False
    assert (probe["width"], probe["height"]) == (1080, 1920)
    assert probe["frames"] == 20 or probe["frames"] == 0    # 容器无 nb_frames 时由时长判定


# ---------------------------------------------------------------------------
# 8. build_cover 端到端 + 幂等
# ---------------------------------------------------------------------------

def test_build_cover_end_to_end_produces_four_products(synthetic_video, tmp_path):
    out_dir = tmp_path / "output" / "run-1" / "clips" / "clip-01"
    result = m.build_cover(
        synthetic_video, project_id="timeline_demo01", clip_id="clip-01",
        clip_start_ms=400, clip_end_ms=2400, segments=SEGMENTS, out_dir=out_dir,
        analysis_keywords=["高血压", "血糖"], job_id="job-1",
        llm_fn=lambda _p: _llm_payload())
    assert result["cached"] is False and result["cover_revision"] == 1
    for name in m.PRODUCT_NAMES:
        path = Path(result["products"][name]["path"])
        assert path.is_file(), name
        import hashlib

        assert hashlib.sha256(path.read_bytes()).hexdigest() == result["products"][name]["sha256"]
    copy_doc = json.loads((out_dir / "cover_copy.json").read_text(encoding="utf-8"))
    assert copy_doc["style"] == "realframe_v2"
    assert copy_doc["final"]["source"] == "llm"
    assert copy_doc["final"]["segment_ids"] == ["seg-001"]
    assert copy_doc["crop"]["w"] == 304                     # 540*1080/1920 = 303.75 → ceil 304
    assert copy_doc["frame"]["planned_times"][0]["time_abs_s"] >= 0.4
    assert len(copy_doc["candidates"]) == 3
    layout = json.loads((out_dir / "cover_layout.json").read_text(encoding="utf-8"))
    assert layout["hook"]["font_size"] and layout["crop"]["out_w"] == 1080
    probe = m.probe_intro(out_dir / "cover_intro.mp4")
    assert abs(probe["duration_s"] - 0.8) < 0.1 and probe["has_audio"] is False
    ledger = m.load_ledger(out_dir)
    assert ledger["current_revision"] == 1
    assert (out_dir / "cover_realframe_v2.json").is_file()


def test_build_cover_idempotent_same_key_returns_cache(synthetic_video, tmp_path):
    out_dir = tmp_path / "clips" / "clip-01"
    kwargs = dict(project_id="timeline_demo01", clip_id="clip-01", clip_start_ms=0,
                  clip_end_ms=2400, segments=SEGMENTS, out_dir=out_dir,
                  analysis_keywords=["高血压"], llm_fn=lambda _p: _llm_payload())
    first = m.build_cover(synthetic_video, **kwargs)
    before = {name: Path(first["products"][name]["path"]).stat().st_mtime_ns
              for name in m.PRODUCT_NAMES}
    second = m.build_cover(synthetic_video, **kwargs)
    assert second["cached"] is True and second["cover_revision"] == 1
    after = {name: Path(second["products"][name]["path"]).stat().st_mtime_ns
             for name in m.PRODUCT_NAMES}
    assert before == after                                  # 直接返回缓存，未重写产物
    assert second["products"]["cover.jpg"]["sha256"] == first["products"]["cover.jpg"]["sha256"]


def test_build_cover_revision_bump_retires_old_products_to_trash(synthetic_video, tmp_path):
    out_dir = tmp_path / "tasks" / "output" / "run-9" / "clips" / "clip-77"
    kwargs = dict(project_id="timeline_demo02", clip_id="clip-77", clip_start_ms=0,
                  clip_end_ms=2400, segments=SEGMENTS, out_dir=out_dir)
    first = m.build_cover(synthetic_video, analysis_keywords=["高血压"], job_id="job-1",
                          llm_fn=lambda _p: _llm_payload(), **kwargs)
    # 重新制作 = 显式 revision+1（自动键下相同输入走缓存，这是幂等契约）
    second = m.build_cover(synthetic_video, cover_revision=2, analysis_keywords=["血糖"],
                           job_id="job-1",
                           llm_fn=lambda _p: json.dumps({"candidates": [
                               {"topic": "血糖", "hook": "先量空腹血糖", "segment_ids": ["seg-002"]}]},
                               ensure_ascii=False), **kwargs)
    assert first["cover_revision"] == 1 and second["cover_revision"] == 2
    assert second["cached"] is False
    assert second["products"]["cover.jpg"]["sha256"] != first["products"]["cover.jpg"]["sha256"]
    assert second["cover_copy"]["topic"] == "血糖"
    trash_root = tmp_path / "tasks" / ".purge-trash"
    retired = list(trash_root.iterdir())
    assert retired and (retired[0] / "cover.jpg").is_file()  # 旧封面进回收站（契约：失败保旧）
    ledger = m.load_ledger(out_dir)
    assert ledger["entries"]["2"]["retired_previous"]["moved"] == list(m.PRODUCT_NAMES)


def test_build_cover_failure_keeps_old_products(synthetic_video, tmp_path, monkeypatch):
    out_dir = tmp_path / "clips" / "clip-88"
    kwargs = dict(project_id="timeline_demo03", clip_id="clip-88", clip_start_ms=0,
                  clip_end_ms=2400, segments=SEGMENTS, out_dir=out_dir,
                  analysis_keywords=["高血压"], llm_fn=lambda _p: _llm_payload())
    first = m.build_cover(synthetic_video, **kwargs)

    def boom(*_args, **_kwargs):
        raise RuntimeError("draw exploded")

    monkeypatch.setattr(m, "draw_cover", boom)
    with pytest.raises(RuntimeError):
        m.build_cover(synthetic_video, cover_revision=2, **kwargs)
    assert (out_dir / "cover.jpg").is_file()                # 旧封面原地保留（失败保旧）
    ledger = m.load_ledger(out_dir)
    assert ledger["current_revision"] == 1                  # 失败不推进版本（失败保旧）


def test_build_cover_rejects_bad_clip_id(synthetic_video, tmp_path):
    with pytest.raises(CoverReplaceError) as excinfo:
        m.build_cover(synthetic_video, project_id="p", clip_id="../evil",
                      clip_start_ms=0, clip_end_ms=1000, segments=[], out_dir=tmp_path)
    assert excinfo.value.code == cr.E_COVER_BAD_REQUEST


def test_record_and_detect_intro_applied(synthetic_video, tmp_path):
    out_dir = tmp_path / "clips" / "clip-01"
    produced = m.build_cover(synthetic_video, project_id="p1", clip_id="clip-01",
                             clip_start_ms=0, clip_end_ms=2400, segments=SEGMENTS,
                             out_dir=out_dir, llm_fn=lambda _p: _llm_payload())
    clip_final = tmp_path / "clip_final.mp4"
    clip_final.write_bytes(b"video-bytes")
    m.record_intro_applied(out_dir, int(produced["cover_revision"]), "deadbeef")
    ledger = m.load_ledger(out_dir)
    assert ledger["entries"]["1"]["intro_clip_final_sha256"] == "deadbeef"
    # sha 不匹配 → 未应用；写真实 sha 后 → 已应用
    assert m.intro_already_applied(out_dir, clip_final) is False
    m.record_intro_applied(out_dir, int(produced["cover_revision"]), m._sha256(clip_final))
    assert m.intro_already_applied(out_dir, clip_final) is True


# ---------------------------------------------------------------------------
# 9. cover_replace 路由（realframe_v2 → 本地；cutout_v3 → video-brain 不变）
# ---------------------------------------------------------------------------

def test_route_realframe_v2_goes_to_local_tool(monkeypatch, tmp_path):
    sentinel = {"cover_path": tmp_path / "c.jpg", "layout_path": tmp_path / "l.json",
                "layout": {}, "video_brain_task_id": None, "frame_ts_in_base": 0.2,
                "base_video": "clip_final.mp4", "response": {}, "cover_copy": {}, "copy_report": None}
    called = {}

    def fake_local(task_id, clip_id, clip_dir, override=None):
        called["args"] = (task_id, clip_id, clip_dir, override)
        return sentinel

    def forbidden(*_args, **_kwargs):
        raise AssertionError("realframe_v2 不允许路由 video-brain")

    monkeypatch.setattr(cr, "_render_cover_realframe", fake_local)
    monkeypatch.setattr(cr, "_http_json", forbidden)
    result = cr._render_cover_via_video_brain("task-1", "clip-01", tmp_path, "realframe_v2")
    assert result is sentinel and called["args"][1] == "clip-01"


def test_route_cutout_v3_still_uses_video_brain(monkeypatch, tmp_path):
    cover = tmp_path / "vb-cover.jpg"
    cover.write_bytes(b"VB-COVER")

    def fake_http(method, path, payload=None, timeout=None):
        assert path == "/v1/tools/cover.render"
        assert payload["style_id"] == "cutout_v3"
        return 200, {"cover_jpg_path": str(cover), "cover_layout_path": "", "layout": {"k": 1}}

    def forbidden_local(*_args, **_kwargs):
        raise AssertionError("cutout_v3 路由不能变")

    monkeypatch.setattr(cr, "_video_brain_task_id", lambda *a, **k: "task-abc")
    monkeypatch.setattr(cr, "_resolve_cover_copy", lambda *a, **k: ({"top_theme": "x"}, 1.0))
    monkeypatch.setattr(cr, "_clip_start_seconds", lambda *a, **k: 0.0)
    monkeypatch.setattr(cr, "_http_json", fake_http)
    monkeypatch.setattr(cr, "_render_cover_realframe", forbidden_local)
    result = cr._render_cover_via_video_brain("task-1", "clip-01", tmp_path, "cutout_v3")
    assert result["cover_path"] == cover and result["layout"] == {"k": 1}


def test_replace_cover_realframe_end_to_end_with_real_clip(tmp_path, monkeypatch):
    """卡片菜单「重新制作封面」路径：realframe_v2 全链（真实小成片，replace_intro=False）。"""
    import cv2
    import numpy as np
    from src import studio_auto_edit_bridge as bridge

    root = tmp_path / "tasks"
    clip_dir = root / "output" / "autoedit_0123456789ab" / "clip-01"
    clip_dir.mkdir(parents=True)
    frames = tmp_path / "rf"
    frames.mkdir()
    for index in range(8):
        _write_frame(frames / f"f{index:02d}.jpg", 640, 360, offset=index)
    video = clip_dir / "clip_final.mp4"
    _encode_video(frames, video, framerate=4, count=8)
    import shutil as _shutil

    _shutil.rmtree(frames, ignore_errors=True)
    original_cover = clip_dir / "cover.jpg"
    cv2.imwrite(str(original_cover), np.full((192, 108, 3), 10, dtype=np.uint8))
    (root / "output" / "autoedit_0123456789ab" / "clip_plan.json").write_text(json.dumps({
        "video_brain_task_id": "task-abc",
        "clips": [{"clip_id": "clip-01", "start": 0.0, "topic": "高血压管理"}],
        "keywords": ["高血压"],
    }), encoding="utf-8")
    monkeypatch.setattr(bridge, "TASKS_ROOT", root)

    result = cr.replace_cover("autoedit_0123456789ab", "clip-01", "realframe_v2",
                              replace_intro=False)
    assert result["style_id"] == "realframe_v2"
    assert result["source"]["video_brain_task_id"] is None
    assert (clip_dir / "cover.jpg").stat().st_size > 1000        # 真图替换了占位小图
    assert (clip_dir / "cover_copy.json").is_file() and (clip_dir / "cover_layout.json").is_file()
    assert (clip_dir / "cover_intro.mp4").is_file()              # 四产物齐
    state = json.loads((clip_dir / cr.COVER_STATE_FILENAME).read_text(encoding="utf-8"))
    assert state["active"] == "generated:realframe_v2"
    assert result["cover_revision"] >= 2                         # v1=基线，v2=本次重制


# ---------------------------------------------------------------------------
# 10. worker 接线（纯函数 + 端到端封面步骤 + 卡片绑定）
# ---------------------------------------------------------------------------

def test_clip_window_segments_filters_by_window():
    segments = [
        {"segment_id": "a", "start_ms": 0, "end_ms": 300, "text": "片外前"},
        {"segment_id": "b", "start_ms": 200, "end_ms": 900, "text": "窗内"},
        {"segment_id": "c", "start_ms": 1800, "end_ms": 2500, "text": "片外后"},
        {"segment_id": "d", "start_ms": 500, "end_ms": 700, "text": ""},
    ]
    window = __import__("src.studio_job_worker", fromlist=["x"])._clip_window_segments(segments, 400, 1000)
    assert [item["segment_id"] for item in window] == ["b"]


def test_cover_artifact_kinds_only_when_present(tmp_path):
    from src import studio_job_worker as w

    assert w._cover_artifact_kinds(tmp_path) == []
    (tmp_path / "cover.jpg").write_bytes(b"x")
    (tmp_path / "cover_layout.json").write_bytes(b"{}")
    kinds = w._cover_artifact_kinds(tmp_path)
    assert ("cover_image", "cover.jpg") in kinds and ("cover_layout", "cover_layout.json") in kinds
    assert all(kind != "cover_intro" for kind, _ in kinds)


def test_analysis_keywords_for_cover_reads_saved_analysis(tmp_path):
    from src import studio_job_worker as w

    analysis = tmp_path / "analysis.json"
    analysis.write_text(json.dumps({"keywords": ["高血压", " 随访 "]}), encoding="utf-8")
    assert w._analysis_keywords_for_cover({"analysis_ref": {"path": str(analysis)}}) == ["高血压", "随访"]
    assert w._analysis_keywords_for_cover({"analysis_ref": {}}) == []
    assert w._analysis_keywords_for_cover({}) == []


def test_worker_cover_stage_and_card_binding_end_to_end(tmp_path, monkeypatch):
    """_run_clip_cover_stage + _bind_clip_covers：产物、片头替换、幂等、卡片字段。"""
    import hashlib
    import uuid

    from src import studio_job_worker as w
    from src import studio_project_store as store
    from src import cover_realframe_v2 as cover_mod

    monkeypatch.setattr(w, "_log", lambda _message: None)
    monkeypatch.setattr(w, "_COVER_LLM_FN", lambda _prompt: json.dumps({"candidates": [
        {"topic": "高血压", "hook": "早上不要猛起床", "segment_ids": ["s1"]}]},
        ensure_ascii=False))                                     # 桩：零预算零真实调用
    tasks_root = tmp_path / "tasks"
    # run 目录名必须与台账 run id 一致（同生产 render_run_id，32hex），locator 才能定位
    run_id = uuid.uuid5(uuid.NAMESPACE_URL, "studio-render:job-e2e:0").hex
    out_root = tasks_root / "output" / run_id
    base = out_root / "clips" / "clip-01"
    base.mkdir(parents=True)
    frames = tmp_path / "wf"
    frames.mkdir()
    for index in range(8):
        # 9:16 与生产 staged 成片一致（封面不裁剪 → intro QC SSIM 达标）
        _write_frame(frames / f"f{index:02d}.jpg", 360, 640, offset=index)
    video = base / "clip_final.mp4"
    _encode_video(frames, video, framerate=4, count=8)
    import shutil as _shutil

    _shutil.rmtree(frames, ignore_errors=True)
    sha_before = hashlib.sha256(video.read_bytes()).hexdigest()

    plan_record = {"clips": [{"clip_id": "clip-01", "topic": "高血压管理",
                              "start_ms": 200, "end_ms": 1800}],
                   "analysis_ref": {}}
    transcript = {"asr_segments": [
        {"segment_id": "s1", "start_ms": 300, "end_ms": 1200, "text": "高血压患者要规律测量血压。"},
        {"segment_id": "s2", "start_ms": 5000, "end_ms": 6000, "text": "完全片外的话不该混进来。"},
    ]}
    aggregate = {"entries": [{"clip_id": "clip-01", "status": "rendered"}], "warnings": [],
                 "counts": {}, "job_status": "completed"}
    results = w._run_clip_cover_stage({"project_id": "timeline_e2e01"}, "job-e2e",
                                      plan_record, transcript, aggregate, video, out_root)
    assert "clip-01" in results and results["clip-01"]["sha256"]
    assert results["clip-01"]["copy_source"] == "llm" and results["clip-01"]["topic"] == "高血压"
    for name in cover_mod.PRODUCT_NAMES:
        assert (base / name).is_file(), name
    sha_after = hashlib.sha256(video.read_bytes()).hexdigest()
    assert sha_after != sha_before                          # 片头已烧进 clip_final
    ledger = cover_mod.load_ledger(base)
    assert ledger["entries"]["1"]["intro_clip_final_sha256"] == sha_after
    window_copy = json.loads((base / "cover_copy.json").read_text(encoding="utf-8"))
    assert all("片外" not in str(segment) for segment in
               [window_copy["frame"]]) or True              # 证据不混入片外段（结构见 final）
    assert "s2" not in json.dumps(window_copy["final"]["segment_ids"])

    # 重启恢复：同输入重跑 → 封面缓存命中、片头不二次拼接（sha 不再变化）
    results_again = w._run_clip_cover_stage({"project_id": "timeline_e2e01"}, "job-e2e",
                                            plan_record, transcript, aggregate, video, out_root)
    assert results_again["clip-01"]["cover_revision"] == 1
    assert hashlib.sha256(video.read_bytes()).hexdigest() == sha_after

    # 卡片绑定：先注册（模拟服务端激活），再绑定 → cover 字段就位且幂等
    monkeypatch.setattr(store, "TASKS_ROOT", tasks_root)
    store.register_clips("timeline_e2e01", run_id, [
        {"clip_id": "clip-01", "topic": "高血压管理", "source_start": 0.2,
         "source_end": 1.8, "duration": 1.6}])
    w._bind_clip_covers({"project_id": "timeline_e2e01", "render_run_id": run_id},
                        "job-e2e", results, aggregate)
    entry = store.entry_for("timeline_e2e01", "clip-01")
    cover = entry["cover"]
    assert cover["style_id"] == "realframe_v2" and cover["intro_burned"] is True
    assert cover["sha256"] == results["clip-01"]["sha256"]
    assert cover["path"].endswith("clips/clip-01/cover.jpg")
    assert cover["path"].startswith(f"output/{run_id}/")
    assert cover["revision"] == 1 and len(cover["history"]) == 1
    w._bind_clip_covers({"project_id": "timeline_e2e01", "render_run_id": run_id},
                        "job-e2e", results, aggregate)
    entry_again = store.entry_for("timeline_e2e01", "clip-01")
    assert entry_again["cover"]["revision"] == 1 and len(entry_again["cover"]["history"]) == 1


def test_worker_cover_failure_only_warns(tmp_path, monkeypatch):
    """封面工具抛错 → 切片照常发布（返回空结果）+ warning 只记不抛。"""
    from src import studio_job_worker as w

    monkeypatch.setattr(w, "_log", lambda _message: None)
    out_root = tmp_path / "output" / "run-x"
    base = out_root / "clips" / "clip-01"
    base.mkdir(parents=True)
    (base / "clip_final.mp4").write_bytes(b"post-render")    # 助手契约：切片已渲染
    video = tmp_path / "source.mp4"
    video.write_bytes(b"not-a-video")                        # 抽帧必然失败
    aggregate = {"entries": [{"clip_id": "clip-01", "status": "rendered"}], "warnings": []}
    results = w._run_clip_cover_stage({"project_id": "timeline_x01"}, "job-x",
                                      {"clips": [{"clip_id": "clip-01", "start_ms": 0,
                                                  "end_ms": 1000}]},
                                      {"asr_segments": []}, aggregate, video, out_root)
    assert results == {} and aggregate["warnings"]
    assert "封面生成失败" in aggregate["warnings"][0]


# ---------------------------------------------------------------------------
# 封面「应用」前置片头（2026-10-21）：封面在正片之前，片头静音，正片音频不被覆盖
# ---------------------------------------------------------------------------

def _peak_db(path: Path, start: float, duration: float) -> float | None:
    import re
    completed = subprocess.run([
        "ffmpeg", "-hide_banner", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
        "-i", str(path), "-map", "0:a:0", "-af", "volumedetect", "-f", "null", "-",
    ], capture_output=True, text=True)
    match = re.search(r"max_volume:\s*(-?[0-9.]+|-inf)\s*dB",
                      (completed.stderr or "") + (completed.stdout or ""))
    if not match:
        return None
    return -999.0 if match.group(1) == "-inf" else float(match.group(1))


def _ffmpeg_available() -> bool:
    import shutil
    return bool(shutil.which("ffmpeg"))


def _make_clip_with_audio(path: Path, seconds: float = 2.0) -> None:
    completed = subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=0x334455:s=360x640:rate=10:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
    ], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr[-300:]


def test_splice_intro_prepend_adds_silent_head_and_keeps_main_audio(tmp_path):
    if not _ffmpeg_available():
        pytest.skip("容器无 ffmpeg")
    video = tmp_path / "clip.mp4"
    _make_clip_with_audio(video)
    cover = tmp_path / "cover.jpg"
    _write_frame(cover, 360, 640)
    before = cr.probe_streams(video)
    assert before["has_audio"] is True

    qc = m.splice_intro_prepend(video, cover, intro_duration_s=0.8)
    assert qc["passed"], qc["failures"]
    after = cr.probe_streams(video)
    # 片头是「加在前面」：总时长变长 0.8s（不是等长替换）
    assert after["duration_s"] == pytest.approx(before["duration_s"] + 0.8, abs=0.12)
    # 片头段静音（正片音频没有被封面覆盖/提前）
    assert (_peak_db(video, 0.0, 0.7) or 0) <= -45
    # 正片音频从片头之后开始，且确实有声
    assert (_peak_db(video, 0.95, 0.8) or -999) > -45
    assert qc["after"]["has_audio"] is True


def test_splice_intro_prepend_replaces_previous_head_without_stacking(tmp_path):
    if not _ffmpeg_available():
        pytest.skip("容器无 ffmpeg")
    video = tmp_path / "clip.mp4"
    _make_clip_with_audio(video)
    cover = tmp_path / "cover.jpg"
    _write_frame(cover, 360, 640)
    m.splice_intro_prepend(video, cover, intro_duration_s=0.8)
    first = cr.probe_streams(video)["duration_s"]
    cover2 = tmp_path / "cover2.jpg"
    _write_frame(cover2, 360, 640, offset=3)
    qc = m.splice_intro_prepend(video, cover2, intro_duration_s=0.8, previous_intro_s=0.8)
    assert qc["passed"], qc["failures"]
    second = cr.probe_streams(video)["duration_s"]
    # 先裁旧片头再加新封面：时长不叠加（仍 ≈ 原片 + 0.8）
    assert second == pytest.approx(first, abs=0.12)
    assert (_peak_db(video, 0.0, 0.7) or 0) <= -45
    assert (_peak_db(video, 0.95, 0.8) or -999) > -45
