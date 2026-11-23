"""字幕可读性层（阶段2 P2 2026-11-18）：亮背景自适应底板 + 字幕安全区校验。

**亮背景自适应（验收项「复杂背景可读率≥98%」）**
白字+2px 黑描边在明亮/复杂背景（白墙、亮皮肤、灯光、高纹理）下对比度不足。
本层在渲染前对每条 cue 的中点时刻抽源片帧，统计**字幕带区域**（下三分之一
黄金位，与烧录位置同坐标换算）的亮度均值（灰度 0-255）与边缘密度（Sobel
幅度梯度占比），按判定表决定该 cue 用描边还是半透明底板：

    亮度 ≥ LUMA(170) 或 边缘密度 ≥ EDGE(0.18) → 半透明黑底板（SubBox 样式）
    其余 → 现状描边（Sub 样式，产物与历史一致）

底板 = ASS BorderStyle=3 + 半透明黑 OutlineColour（alpha 0x60 ≈ 62% 不透明），
白字压底板在任何背景上都可读——这是抖音默认大字幕的通行做法。

**失败降级**：ffmpeg 抽帧失败 / numpy 缺失 / 任何异常 → 全部回退描边
（``mode=fallback``），绝不阻断渲染。**开关** ``SUBTITLE_READABILITY=off``
可整体停用（运维开关）。

**安全区校验（验收项「字幕安全区自动检测」）**：平台档案常量表（抖音/视频号/
公众号的底部交互 UI 高度比例），静态校验 MarginV=360 的字幕带是否落在平台
安全区内——纯离线计算，不需要抽帧。

坐标口径：源片与成片同构图（当前渲染链只整帧缩放不做裁切），字幕带用
PlayRes 1080×1920 的比例换算（band_top/band_bottom 占帧高分数），抽帧后按
比例裁区域统计——与分辨率无关。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from . import subtitle_cueing as _cueing

# ---------------------------------------------------------------------------
# 配置（环境变量）
# ---------------------------------------------------------------------------


def _env_float(e: dict, key: str, default: float) -> float:
    try:
        return float(e.get(key, "") or default)
    except ValueError:
        return default


def _env_int(e: dict, key: str, default: int) -> int:
    try:
        return int(e.get(key, "") or default)
    except ValueError:
        return default


def enabled(env: dict | None = None) -> bool:
    """可读性自适应开关（默认开；off = 全部描边，历史行为）。"""
    e = os.environ if env is None else env
    raw = str(e.get("SUBTITLE_READABILITY") or "").strip().lower()
    if not raw:
        return True
    return raw in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# 字幕带几何（与 subtitle_cueing 烧录常量同源换算，PlayRes 1080x1920）
# ---------------------------------------------------------------------------


def subtitle_band_fractions(max_lines: int = 2) -> tuple[float, float]:
    """字幕带在帧高上的占比区间 (top, bottom)。

    块高 ≈ max_lines × 字号 × 1.15（ASS 行距近似），底部锚点 = MarginV。
    与 :data:`subtitle_cueing.BURN_MARGIN_V_PX` 等常量同源，改烧录位置时此处
    自动跟随。
    """
    h = float(_cueing.BURN_PLAYRES_H)
    block_h = max_lines * _cueing.BURN_FONT_SIZE_PX * 1.15
    top = max(0.0, (h - _cueing.BURN_MARGIN_V_PX - block_h) / h)
    bottom = min(1.0, (h - _cueing.BURN_MARGIN_V_PX) / h)
    return round(top, 4), round(bottom, 4)


# ---------------------------------------------------------------------------
# 帧采样与统计
# ---------------------------------------------------------------------------

_FFMPEG_FALLBACK_DIRS = ("/domain/dsh-home/bin", "/usr/local/bin", "/usr/bin")
_SAMPLE_W, _SAMPLE_H = 270, 480      # 9:16 降采样（约 1080x1920 的 1/4）
_EDGE_GRADIENT_TH = 24.0             # 梯度阈值（0-255 灰度）


def _resolve_ffmpeg(env: dict | None = None) -> str:
    e = os.environ if env is None else env
    name = str(e.get("CONTENT_RENDER_FFMPEG") or "ffmpeg")
    if shutil.which(name):
        return name
    for directory in _FFMPEG_FALLBACK_DIRS:
        candidate = Path(directory) / name
        if candidate.is_file():
            return str(candidate)
    return name


def _grab_gray_frame(ffmpeg: str, source: Path, t_seconds: float,
                     env: dict | None = None) -> bytes | None:
    """抽取单帧降采样灰度 raw（宽×高 bytes）；失败返回 None。"""
    cmd = [ffmpeg, "-v", "error", "-ss", f"{max(0.0, t_seconds):.3f}",
           "-i", str(source), "-frames:v", "1",
           "-vf", f"scale={_SAMPLE_W}:{_SAMPLE_H}",
           "-pix_fmt", "gray", "-f", "rawvideo", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=20)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    raw = proc.stdout
    if len(raw) < _SAMPLE_W * _SAMPLE_H:
        return None
    return raw[:_SAMPLE_W * _SAMPLE_H]


def band_stats(gray_raw: bytes, band: tuple[float, float]) -> dict | None:
    """单帧字幕带统计：亮度均值（0-255）+ 边缘密度（梯度>阈值的像素占比）。

    需要 numpy；缺失返回 None（调用方降级）。纯计算，无 I/O。
    """
    try:
        import numpy as np
    except ImportError:
        return None
    frame = np.frombuffer(gray_raw, dtype=np.uint8).reshape(_SAMPLE_H, _SAMPLE_W)
    top, bottom = band
    y0 = int(frame.shape[0] * top)
    y1 = max(y0 + 2, int(frame.shape[0] * bottom))
    band_px = frame[y0:y1, :].astype(np.float32)
    luma = float(band_px.mean())
    gx = np.abs(np.diff(band_px, axis=1))
    gy = np.abs(np.diff(band_px, axis=0))
    grad = np.concatenate([gx.ravel(), gy.ravel()])
    density = float((grad > _EDGE_GRADIENT_TH).mean())
    return {"luma": round(luma, 1), "edge_density": round(density, 4)}


# ---------------------------------------------------------------------------
# 判定表（纯函数，可单测）
# ---------------------------------------------------------------------------


def decide_band(stats: dict, luma_th: float = 170.0, edge_th: float = 0.18) -> str:
    """单帧判定：``"box"``（需要底板）| ``"outline"``（现状描边即可）。"""
    if stats.get("luma", 0.0) >= luma_th or stats.get("edge_density", 0.0) >= edge_th:
        return "box"
    return "outline"


# ---------------------------------------------------------------------------
# 主入口：逐 cue 分析 → 每条样式决定 + 报告
# ---------------------------------------------------------------------------


def _virtual_to_source_ms(t_ms: float, ranges: list[tuple[int, int]] | None) -> float:
    """净化虚拟时间轴 → 源片绝对 ms（cue 中点抽帧用）。无 ranges 原样返回。"""
    if not ranges:
        return t_ms
    offset = 0
    last_source = float(ranges[0][0])
    for (s, e) in ranges:
        rel = t_ms - offset
        span = e - s
        if 0 <= rel <= span:
            return s + rel
        offset += span
        last_source = float(e)
    return last_source  # 落在区间缝隙（已删除片段）→ 钳到最近区间末尾


def analyze_cues(source: Path, cues: list[dict], *,
                 ranges: list[tuple[int, int]] | None = None,
                 clip_start_ms: float = 0.0,
                 env: dict | None = None) -> dict:
    """逐 cue 可读性分析。

    ``cues`` 时间为切片内相对秒；单区间路径按 ``clip_start_ms`` 换算源片绝对
    时间，净化多区间路径按虚拟时间轴反解（与 words 平移同构）。
    返回 ``{"mode": "analyzed"|"off"|"fallback", "box_cues": [索引(0基)],
    "samples": [...], "pre_mitigation_readability": 占比, "reason": ...}``。
    ``box_cues`` 供 ASS 层用 SubBox 样式；任何异常 → fallback 全描边。
    """
    e = os.environ if env is None else env
    luma_th = _env_float(e, "SUBTITLE_READABILITY_LUMA", 170.0)
    edge_th = _env_float(e, "SUBTITLE_READABILITY_EDGE", 0.18)
    max_frames = max(1, _env_int(e, "SUBTITLE_READABILITY_MAX_FRAMES", 24))

    doc: dict = {"mode": "off", "box_cues": [], "samples": [],
                 "pre_mitigation_readability": None, "reason": ""}
    if not enabled(e):
        doc["reason"] = "SUBTITLE_READABILITY=off"
        return doc
    if not cues:
        doc["reason"] = "no cues"
        return doc
    band = subtitle_band_fractions()
    try:
        import numpy  # noqa: F401 —— 先探依赖，缺失直接 fallback
        ffmpeg = _resolve_ffmpeg(e)
        # 抽帧上限：cue 多于 max_frames 时均匀抽样，其余 cue 沿用相邻判定
        idxs = list(range(len(cues)))
        if len(idxs) > max_frames:
            step = len(idxs) / max_frames
            idxs = [int(i * step) for i in range(max_frames)]
        verdict_by_sample: dict[int, str] = {}
        for idx in idxs:
            cue = cues[idx]
            mid_ms = (float(cue["start"]) + float(cue["end"])) / 2.0 * 1000.0
            if ranges:
                src_ms = _virtual_to_source_ms(mid_ms, ranges)
            else:
                src_ms = clip_start_ms + mid_ms
            raw = _grab_gray_frame(ffmpeg, source, src_ms / 1000.0, e)
            if raw is None:
                continue
            stats = band_stats(raw, band)
            if stats is None:
                continue
            verdict = decide_band(stats, luma_th, edge_th)
            verdict_by_sample[idx] = verdict
            doc["samples"].append({"cue": idx, **stats, "verdict": verdict})
        if not verdict_by_sample:
            doc.update({"mode": "fallback",
                        "reason": "无有效采样帧（抽帧失败/依赖缺失），全部回退描边"})
            return doc
        # 未采样 cue 继承最近采样点判定（前向填充）
        box_cues: list[int] = []
        last = "outline"
        for idx in range(len(cues)):
            if idx in verdict_by_sample:
                last = verdict_by_sample[idx]
            if last == "box":
                box_cues.append(idx)
        n = len(cues)
        readable_raw = sum(1 for idx in range(n)
                           if (verdict_by_sample.get(idx, last)) == "outline")
        doc.update({
            "mode": "analyzed", "box_cues": box_cues,
            "pre_mitigation_readability": round(readable_raw / n, 4),
            "reason": f"亮度阈值 {luma_th} / 边缘阈值 {edge_th}，采样 {len(verdict_by_sample)}/{n} 条",
        })
        return doc
    except Exception as exc:  # 任何异常 → 降级，绝不阻断渲染
        doc.update({"mode": "fallback",
                    "reason": f"分析异常降级: {type(exc).__name__}"})
        return doc


# ---------------------------------------------------------------------------
# 安全区档案（静态校验，离线可完成）
# ---------------------------------------------------------------------------

#: 平台交互 UI 高度档案（占帧高比例）。抖音底部点赞/评论/文案区最深。
SAFE_ZONE_PROFILES: dict[str, dict[str, float]] = {
    "douyin": {"top_ui_frac": 0.06, "bottom_ui_frac": 0.16},
    "shipinhao": {"top_ui_frac": 0.05, "bottom_ui_frac": 0.14},
    "baijiahao": {"top_ui_frac": 0.05, "bottom_ui_frac": 0.12},
    "default": {"top_ui_frac": 0.05, "bottom_ui_frac": 0.12},
}


def check_safe_zone(env: dict | None = None, max_lines: int = 2) -> dict:
    """校验当前烧录位置是否落在平台安全区内（静态，纯计算）。

    判据：字幕带底边距（MarginV/PlayResH）≥ 档案底部 UI 高度；带顶不越过
    顶部 UI。返回报告 dict（含 pass/reason），写入 warnings.json。
    """
    e = os.environ if env is None else env
    name = str(e.get("SUBTITLE_PLATFORM_PROFILE") or "douyin").strip().lower()
    profile = SAFE_ZONE_PROFILES.get(name, SAFE_ZONE_PROFILES["default"])
    if name not in SAFE_ZONE_PROFILES:
        doc = {"profile": name, "resolved": "default", "pass": True,
               "reason": f"未知档案 {name}，按 default 校验"}
        name = "default"
    else:
        doc = {"profile": name, "resolved": name}
    h = float(_cueing.BURN_PLAYRES_H)
    band_top, band_bottom = subtitle_band_fractions(max_lines)
    bottom_margin_frac = _cueing.BURN_MARGIN_V_PX / h
    top_ok = band_top >= profile["top_ui_frac"]
    bottom_ok = bottom_margin_frac >= profile["bottom_ui_frac"]
    doc.update({
        "margin_v_px": _cueing.BURN_MARGIN_V_PX,
        "band_top_frac": band_top, "band_bottom_frac": band_bottom,
        "bottom_margin_frac": round(bottom_margin_frac, 4),
        "profile_top_ui_frac": profile["top_ui_frac"],
        "profile_bottom_ui_frac": profile["bottom_ui_frac"],
        "pass": bool(top_ok and bottom_ok),
        "reason": ("" if (top_ok and bottom_ok) else
                   f"字幕带越界：顶部{'越' if not top_ok else '未'}越界，"
                   f"底部留白 {bottom_margin_frac:.3f} <={'' if not bottom_ok else '>'}"
                   f" 档案 {profile['bottom_ui_frac']}"),
    })
    return doc
