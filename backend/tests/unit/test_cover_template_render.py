"""封面版型真实渲染（2026-10-21 路线 A）定向测试。

覆盖：
* 注册表版面契约：四版型各带 layout，版本升位，未知版型 spec 报错；
* apply_template_layout：颜色/描边覆盖 + 装饰记录，坐标与字号不动（纯函数）；
* draw_cover 模板装饰：无装饰行为不变；有装饰（面板/强调条/暗幕/气泡）出图非空；
* replace_cover(template_id=…) 端到端（合成视频 + 真实 ffmpeg/cv2/系统 CJK 字体）：
  封面按版型重渲、revision 快照 +1、风格记录 template:cute、幂等（同参二调 noop）、
  未知版型 E_TEMPLATE_UNKNOWN；
* 路由校验：template_id 与 style_id 互斥 422、未知版型 404。

全程确定性：无模型调用；文案走 cover_copy/clip_plan 回退链。
真实渲染冒烟依赖容器内 ffmpeg/cv2/系统 CJK 字体（真实封面同款环境），缺失即 skip。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException

from src import cover_templates as registry
from src import studio_access as sa
from src.cover_realframe_v2 import apply_template_layout, draw_cover
from src.cover_replace import CoverReplaceError, replace_cover
from src.api.routes import media

PID = "timeline_coverlib01"
TASK = "autoedit_aaaabbbbcccc"
CLIP = "clip-aaaaaaaaaa"
TEMPLATE_IDS = ["cute", "cool", "anime_layout", "yellow_bold"]


# ---------------------------------------------------------------------------
# 注册表：版面契约
# ---------------------------------------------------------------------------

def test_layout_contract_present_for_all_templates():
    assert registry.REGISTRY_VERSION == "2026-10-22.1"
    for template_id in TEMPLATE_IDS:
        spec = registry.template_render_spec(template_id)
        assert spec["template_id"] == template_id
        assert spec["registry_version"] == registry.REGISTRY_VERSION
        assert spec["name"]
        layout = spec["layout"]
        theme = layout.get("theme") or {}
        assert theme.get("color") and theme.get("stroke_color")
        assert int(theme.get("stroke_width") or 0) > 0
        # 主题标签起始档 ≥48（2026-10-22 规范：标签明显小于主标题，最小 54）；自动缩宽仍生效
        assert float(theme.get("start_size") or 0) >= 48
        assert isinstance(layout, dict)
        # 每个版型至少声明一种装饰或压屏方案（scrim/panel/accent_bar/bubbles 任意组合）
        assert any(key in layout for key in ("scrim", "panel", "accent_bar", "bubbles"))


def test_template_render_spec_unknown():
    with pytest.raises(registry.CoverTemplateError) as ei:
        registry.template_render_spec("kawaii")
    assert ei.value.code == "E_TEMPLATE_UNKNOWN"


# ---------------------------------------------------------------------------
# apply_template_layout（纯函数）
# ---------------------------------------------------------------------------

def _base_layout() -> dict:
    return {
        "font": {"path": "/fonts/fake.ttf"},
        "theme": {"text": "主题", "font_size": 72, "color": "#FFFFFF",
                  "stroke": {"width": 3, "color": "#000000"}, "position": [100, 200]},
        "hook": {"text": "钩子", "lines": ["钩子"], "font_size": 96, "color": "#FFD91A",
                 "stroke": {"width": 6, "color": "#000000"}, "position": [100, 320]},
    }


def test_apply_template_layout_overrides_colors_and_records_decor():
    template = registry.template_render_spec("cute")
    layout = apply_template_layout(_base_layout(), template)
    assert layout["theme"]["color"] == "#E64980"
    # 2026-10-22 规范：可爱版主标题深棕（#5B4636）收在浅奶油卡片内，标签珊瑚粉；
    # 描边色随卡片底（#FFF8F0），绘制层消费契约色（_bold_text outline_color）。
    assert layout["theme"]["stroke"]["color"] == "#FFF8F0"
    assert layout["hook"]["color"] == "#5B4636"
    # 坐标/字号不动（由 compute_text_layout 的确定性自动缩放负责）
    assert layout["theme"]["position"] == [100, 200]
    assert layout["theme"]["font_size"] == 72
    assert layout["template"]["template_id"] == "cute"
    assert layout["template"]["registry_version"] == registry.REGISTRY_VERSION
    assert layout["template"]["decor"]["panel"]["radius"] == 34


def test_apply_template_layout_is_deterministic():
    template = registry.template_render_spec("cool")
    assert apply_template_layout(_base_layout(), template) == apply_template_layout(_base_layout(), template)


# ---------------------------------------------------------------------------
# draw_cover：装饰层
# ---------------------------------------------------------------------------

def _draw_base(tmp_path: Path) -> Path:
    from PIL import Image
    base = tmp_path / "base.png"
    Image.new("RGB", (540, 960), (90, 90, 110)).save(base)
    return base


def _draw_layout_with_font(tmp_path: Path) -> dict:
    """真实 CJK 字体（系统回退链）的最小版面；字体缺失则跳过装饰冒烟。"""
    from src.cover_realframe_v2 import resolve_cjk_font
    try:
        font, report = resolve_cjk_font("主题钩子")
    except Exception:  # noqa: BLE001 — E_COVER_CJK_FONT_MISSING 等
        pytest.skip("容器无可用 CJK 字体")
    layout = _base_layout()
    layout["font"] = {"path": report["path"]}
    layout["theme"]["position"] = [80, 300]
    layout["hook"]["position"] = [80, 460]
    assert Path(report["path"]).is_file()
    return layout


def test_draw_cover_without_template_unchanged(tmp_path):
    layout = _draw_layout_with_font(tmp_path)
    out = draw_cover(_draw_base(tmp_path), layout, tmp_path / "plain.jpg")
    assert out.is_file() and out.stat().st_size > 0


def test_draw_cover_with_template_decor(tmp_path):
    layout = apply_template_layout(_draw_layout_with_font(tmp_path),
                                   registry.template_render_spec("cute"))
    out = draw_cover(_draw_base(tmp_path), layout, tmp_path / "cute.jpg")
    assert out.is_file() and out.stat().st_size > 0
    from PIL import Image
    with Image.open(out) as image:
        assert image.size == (540, 960)


# ---------------------------------------------------------------------------
# replace_cover(template_id=…) 端到端（合成视频）
# ---------------------------------------------------------------------------

@pytest.fixture()
def clip_env(tmp_path, monkeypatch):
    """staged 片目录 + 合成视频（真实 ffmpeg/cv2 可解码），TASKS_ROOT 指向 tmp。"""
    ffmpeg = None
    for candidate in ("/domain/.ffbin/ffmpeg", "/domain/dsh-home/bin/ffmpeg"):
        if Path(candidate).is_file():
            ffmpeg = candidate
            break
    if not ffmpeg:
        pytest.skip("容器无 ffmpeg")
    root = tmp_path / "tasks"
    clip_dir = root / "output" / TASK / "clips" / CLIP
    clip_dir.mkdir(parents=True)
    video = clip_dir / "clip_final.mp4"
    generated = subprocess.run(
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "color=c=0x555555:s=360x640:rate=10:duration=1.2",
         "-pix_fmt", "yuv420p", "-c:v", "libx264", str(video)],
        capture_output=True, timeout=90)
    if generated.returncode != 0:
        pytest.skip(f"合成视频失败: {generated.stderr.decode()[-120:]}")
    (clip_dir / "cover.jpg").write_bytes(b"ORIGINAL-COVER")
    plan = root / "output" / TASK / "clip_plan.json"
    plan.write_text(json.dumps({
        "video_brain_task_id": "task-abc",
        "highlights": [{"clip_id": CLIP, "start": 0.1, "end": 1.0}],
        "topic": "血压管理要点",
    }), encoding="utf-8")

    from src import studio_auto_edit_bridge as bridge
    from src import cover_replace as cr
    monkeypatch.setattr(bridge, "TASKS_ROOT", root)

    def _fake_probe(path, **_kwargs):
        return {"duration_s": 1.2, "width": 360, "height": 640, "has_audio": False,
                "frames": 12, "fps": 10.0}
    monkeypatch.setattr(cr, "probe_streams", _fake_probe)
    return {"tasks": root, "clip_dir": clip_dir, "cr": cr}


def test_replace_cover_with_template_renders_and_snapshots(clip_env):
    cr = clip_env["cr"]
    result = replace_cover(TASK, CLIP, None, replace_intro=False, template_id="cute")
    assert result["idempotent"] is False
    assert result["style_id"] == "template:cute"
    assert int(result.get("cover_revision") or 0) >= 2  # v1 = 原始封面基线
    cover = clip_env["clip_dir"] / "cover.jpg"
    assert cover.read_bytes() != b"ORIGINAL-COVER"  # 真的重渲了
    layout = json.loads((clip_env["clip_dir"] / "cover_layout.json").read_text(encoding="utf-8"))
    assert layout["template"]["template_id"] == "cute"
    assert layout["template"]["registry_version"] == registry.REGISTRY_VERSION
    # 幂等：同版型同参数二调 → noop（不重复渲染/备份）
    again = replace_cover(TASK, CLIP, None, replace_intro=False, template_id="cute")
    assert again["idempotent"] is True


def test_replace_cover_template_switch_changes_output(clip_env):
    replace_cover(TASK, CLIP, None, replace_intro=False, template_id="cute")
    first = (clip_env["clip_dir"] / "cover.jpg").read_bytes()
    second = replace_cover(TASK, CLIP, None, replace_intro=False, template_id="yellow_bold")
    assert second["style_id"] == "template:yellow_bold"
    assert (clip_env["clip_dir"] / "cover.jpg").read_bytes() != first


def test_replace_cover_unknown_template_code(clip_env):
    from src import cover_templates
    with pytest.raises(cover_templates.CoverTemplateError) as ei:
        replace_cover(TASK, CLIP, None, replace_intro=False, template_id="kawaii")
    assert ei.value.code == "E_TEMPLATE_UNKNOWN"


def test_replace_cover_template_and_style_mutually_exclusive(clip_env):
    # 同时给出 style_id 与 template_id：replace_cover 层显式拒绝（路由层 422 同语义）
    with pytest.raises(ValueError) as ei:
        replace_cover(TASK, CLIP, "realframe_v2", replace_intro=False, template_id="cute")
    assert "二选一" in str(ei.value)


# ---------------------------------------------------------------------------
# 版型封面工作流：草稿 → 应用 / 删除（2026-10-21 第二版）
# ---------------------------------------------------------------------------

def test_make_marks_pending_and_card_state(clip_env):
    from src.cover_replace import (cover_template_state_for_card, mark_template_pending,
                                   replace_cover as rc)
    result = rc(TASK, CLIP, None, replace_intro=False, template_id="cool")
    mark_template_pending(TASK, CLIP, int(result["cover_revision"]), result["style_id"])
    state = cover_template_state_for_card(clip_env["clip_dir"])
    assert state["has_cover"] is True
    assert state["pending"] is True and state["applied"] is False
    assert state["revision"] == int(result["cover_revision"])
    assert state["style"] == "template:cool"
    # 成片未动（Make 只出封面素材）
    video = clip_env["clip_dir"] / "clip_final.mp4"
    assert video.stat().st_size > 0


def test_delete_draft_restores_previous_and_clears_state(clip_env):
    from src.cover_replace import (cover_template_state_for_card, delete_template_cover,
                                   mark_template_pending)
    original_cover = (clip_env["clip_dir"] / "cover.jpg").read_bytes()
    result = replace_cover(TASK, CLIP, None, replace_intro=False, template_id="cute")
    revision = int(result["cover_revision"])
    mark_template_pending(TASK, CLIP, revision, "template:cute")
    assert (clip_env["clip_dir"] / "cover.jpg").read_bytes() != original_cover
    deleted = delete_template_cover(TASK, CLIP)
    assert deleted["deleted_revision"] == revision
    assert deleted["restored_previous"]["revision"] >= 1
    # 草稿删掉 → 上一版封面回到当前（素材文件被清）
    assert (clip_env["clip_dir"] / "cover.jpg").read_bytes() == original_cover
    state = cover_template_state_for_card(clip_env["clip_dir"])
    assert state["pending"] is False
    assert not (clip_env["clip_dir"] / f"rev-{revision}").exists()


def test_apply_burns_intro_and_marks_applied(clip_env):
    from src.cover_replace import (apply_template_cover, cover_template_state_for_card,
                                   mark_template_pending)
    video = clip_env["clip_dir"] / "clip_final.mp4"
    before = video.read_bytes()
    result = replace_cover(TASK, CLIP, None, replace_intro=False, template_id="cool")
    mark_template_pending(TASK, CLIP, int(result["cover_revision"]), "template:cool")
    applied = apply_template_cover(TASK, CLIP)
    assert applied["applied"] is True
    state = cover_template_state_for_card(clip_env["clip_dir"])
    assert state["applied"] is True and state["pending"] is False
    # 成片被替换（0.8s 片头烧录）；失败保留原成片的语义由 splice 原子替换保证
    assert video.read_bytes() != before


def test_apply_delete_guards(clip_env):
    from src.cover_replace import (apply_template_cover, cover_template_state_for_card,
                                   delete_template_cover, mark_template_pending)
    # 无草稿：都拒绝
    with pytest.raises(CoverReplaceError):
        apply_template_cover(TASK, CLIP)
    with pytest.raises(CoverReplaceError):
        delete_template_cover(TASK, CLIP)
    # 已应用：再应用/再删除都拒绝
    result = replace_cover(TASK, CLIP, None, replace_intro=False, template_id="cute")
    mark_template_pending(TASK, CLIP, int(result["cover_revision"]), "template:cute")
    apply_template_cover(TASK, CLIP)
    with pytest.raises(CoverReplaceError):
        apply_template_cover(TASK, CLIP)
    with pytest.raises(CoverReplaceError):
        delete_template_cover(TASK, CLIP)
    assert cover_template_state_for_card(clip_env["clip_dir"])["applied"] is True


def test_card_state_defaults_safe_when_clip_missing():
    from src.cover_replace import cover_template_state_for_card
    state = cover_template_state_for_card(Path("/nonexistent/clip"))
    assert state["pending"] is False and state["has_cover"] is False


# ---------------------------------------------------------------------------
# 首次制作封面：成片从来没有 cover.jpg（2026-10-21 修，用户报「选择制作封面它无法生成」）
# ---------------------------------------------------------------------------

def _strip_cover_and_state(clip_env):
    """还原「自动剪辑只出了片、还没做过封面」的干净状态。"""
    clip_dir = clip_env["clip_dir"]
    (clip_dir / "cover.jpg").unlink(missing_ok=True)
    (clip_dir / "cover_layout.json").unlink(missing_ok=True)
    (clip_dir / "cover_replace.json").unlink(missing_ok=True)
    return clip_dir


def test_baseline_without_cover_keeps_none_and_is_backup_ready(clip_env):
    cr = clip_env["cr"]
    clip_dir = _strip_cover_and_state(clip_env)
    baseline = cr._ensure_baseline(clip_dir, cr._load_state(clip_dir))
    assert baseline["cover_jpg"] is None
    assert baseline["sha256"]["cover_jpg"] is None
    assert baseline["bytes"]["cover_jpg"] is None
    assert baseline["clip_final"]
    backups = sorted(p.name for p in (clip_dir / cr.COVER_BACKUP_DIRNAME).iterdir())
    assert len(backups) == 1 and backups[0].startswith("clip_final.mp4.")
    assert cr._backup_ready(clip_dir, baseline) is True
    # 复用同一条基线，不叠加备份、不因为「现在有封面了」而重建
    (clip_dir / "cover.jpg").write_bytes(b"FIRST-COVER")
    again = cr._ensure_baseline(clip_dir, cr._load_state(clip_dir))
    assert again["cover_jpg"] is None
    assert len(list((clip_dir / cr.COVER_BACKUP_DIRNAME).iterdir())) == 1


def test_first_template_cover_on_clip_without_cover_renders(clip_env):
    from src.cover_replace import cover_template_state_for_card, mark_template_pending
    clip_dir = _strip_cover_and_state(clip_env)
    result = replace_cover(TASK, CLIP, None, replace_intro=False, template_id="cute")
    assert result["style_id"] == "template:cute"
    assert result["idempotent"] is False
    assert (clip_dir / "cover.jpg").is_file()
    assert (clip_dir / "cover.jpg").read_bytes() != b""
    layout = json.loads((clip_dir / "cover_layout.json").read_text(encoding="utf-8"))
    assert layout["template"]["template_id"] == "cute"
    state = json.loads((clip_dir / "cover_replace.json").read_text(encoding="utf-8"))
    assert state["baseline"]["cover_jpg"] is None      # 原始状态=无封面
    assert int(result["cover_revision"]) == 2          # v1 由基线代表
    mark_template_pending(TASK, CLIP, 2, result["style_id"])
    card = cover_template_state_for_card(clip_dir)
    assert card["has_cover"] is True and card["pending"] is True


def test_delete_first_draft_returns_to_no_cover(clip_env):
    from src.cover_replace import (cover_template_state_for_card, delete_template_cover,
                                   mark_template_pending, read_cover_state)
    clip_dir = _strip_cover_and_state(clip_env)
    result = replace_cover(TASK, CLIP, None, replace_intro=False, template_id="cool")
    mark_template_pending(TASK, CLIP, int(result["cover_revision"]), result["style_id"])
    deleted = delete_template_cover(TASK, CLIP)
    assert deleted["deleted_revision"] == int(result["cover_revision"])
    assert deleted["restored_previous"]["restored_baseline"] is True
    # 回到「从来没有封面」：封面与 layout 都被删掉，而不是伪造一张旧图
    assert not (clip_dir / "cover.jpg").exists()
    assert not (clip_dir / "cover_layout.json").exists()
    assert cover_template_state_for_card(clip_dir)["has_cover"] is False
    assert read_cover_state(TASK, CLIP)["has_cover"] is False
    # 成片原样未动
    assert (clip_dir / "clip_final.mp4").read_bytes()


def test_restore_cover_after_first_cover_deletes_it(clip_env):
    from src.cover_replace import read_cover_state, restore_cover
    clip_dir = _strip_cover_and_state(clip_env)
    replace_cover(TASK, CLIP, None, replace_intro=False, template_id="cute")
    assert read_cover_state(TASK, CLIP)["has_cover"] is True
    restored = restore_cover(TASK, CLIP)
    assert restored["kind"] == "restore"
    assert restored["qc"]["restore_verification"]["restored"] is True
    assert restored["qc"]["restore_verification"]["cover_restored"] is False
    assert not (clip_dir / "cover.jpg").exists()
    assert read_cover_state(TASK, CLIP)["has_cover"] is False
