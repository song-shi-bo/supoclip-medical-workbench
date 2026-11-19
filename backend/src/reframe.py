"""人脸/主体重构图 MVP（阶段2 P4 2026-11-18）：横转竖的人脸跟踪裁切。

**定位（用户需求原文对照）**
* 单人脸跟踪：主脸策略（面积×置信度最大者），**绝不自动切人**；
* 上半身安全区：crop 窗口纵向定位保脸（face 上沿落窗口上部），横向跟踪脸+手；
* 平滑移动：复用 ``video_utils.build_crop_trajectory``（SmoothDamp 临界阻尼
  弹簧 + 死区 5% + 中值滤波 + 缺口前后填充——存量资产接线，不重造）；
* 手势区域保护：MediaPipe Pose 手腕关键点靠近画面主体时并入跟踪目标（手到哪
  跟到哪的保守版：手腕在窗口可达范围内才并入，防止远处误抬手拉飞画面）；
* 字幕避让：竖裁方向（pan_y）检查脸是否落进成片字幕带（MarginV 360 区域），
  落入记 warning（MVP 不做内容级避让；pan_x 方向字幕带纵向固定，无此问题）；
* 检测失败回退居中裁切：轨迹稀疏/无人脸 → 静态居中（绝不阻断渲染）；
* 多人场景保守：第二张脸面积 ≥ 主脸 60% 持续 >2s → 整条禁用动态跟踪，
  定帧主脸开头位置（不自动频繁切人）。

**接线（clip_render）**：``REFRAME_MODE=auto`` 时渲染前分析源片切片窗口
（3fps@480w 单遍解码），产出裁切 spec——横宽源（宽于 9:16）走 x 平滑跟踪
（y=0），竖高源走静态 y 定位（MVP），9:16 源零开销直通。默认 **off**：
不开则行为与历史完全一致（指纹 payload 也不加键，不失效任何缓存）。

**降级链**：mediapipe/cv2 缺失、抽帧失败、任何异常 → 静态居中或直通，
绝不抛错阻断渲染（与字幕可读性层同哲学）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

#: 目标画幅（竖屏）
TARGET_ASPECT = 9.0 / 16.0
#: 纵横比判定容差（±2% 内视为 9:16 直通）
_ASPECT_TOL = 0.02
#: 多人保守：第二脸面积 / 主脸面积 阈值与持续时间
_SECOND_FACE_RATIO = 0.60
_SECOND_FACE_MIN_S = 2.0
#: 手势并入：手腕与脸中心的距离超过 N 倍窗口半宽 → 忽略（防远处误抬手拉飞）
_WRIST_REACH_WIN_HALF_WIDTHS = 1.2
#: 采样参数（与 video_utils.analyze_vertical_clip 同量级）
_SAMPLE_FPS = 3.0
_PROC_WIDTH = 480
_EDGE_GRADIENT_TH = 24.0

_FFMPEG_FALLBACK_DIRS = ("/domain/dsh-home/bin", "/usr/local/bin", "/usr/bin")


# ---------------------------------------------------------------------------
# 配置与工具
# ---------------------------------------------------------------------------


def reframe_mode(env: dict | None = None) -> str:
    """重构图模式：``off``（默认，行为与历史一致）| ``auto``。"""
    e = os.environ if env is None else env
    mode = str(e.get("REFRAME_MODE") or "").strip().lower()
    return mode if mode in {"off", "auto"} else "off"


def gesture_enabled(env: dict | None = None) -> bool:
    """手势保护开关（默认 on；Pose 初始化失败自动降级为仅脸）。"""
    e = os.environ if env is None else env
    raw = str(e.get("REFRAME_GESTURE") or "").strip().lower()
    return raw not in {"0", "false", "off", "no"} if raw else True


def _even(value: float) -> int:
    v = max(2, int(round(value)))
    return v - (v % 2)


def _resolve_ffmpeg(env: dict) -> str:
    name = str(env.get("CONTENT_RENDER_FFMPEG") or "ffmpeg")
    if shutil.which(name):
        return name
    for directory in _FFMPEG_FALLBACK_DIRS:
        candidate = Path(directory) / name
        if candidate.is_file():
            return str(candidate)
    return name


def _resolve_ffprobe(env: dict) -> str:
    ffmpeg = _resolve_ffmpeg(env)
    if Path(ffmpeg).name.startswith("ffmpeg"):
        return str(Path(ffmpeg).with_name(Path(ffmpeg).name.replace("ffmpeg", "ffprobe")))
    return "ffprobe"


def probe_dims(source: Path, env: dict | None = None) -> tuple[int, int]:
    """ffprobe 源片宽高；失败返回 (0, 0)（调用方直通）。"""
    e = os.environ if env is None else env
    try:
        proc = subprocess.run(
            [_resolve_ffprobe(e), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "json", str(source)],
            capture_output=True, text=True, timeout=60)
        data = json.loads(proc.stdout or "{}")
        stream = (data.get("streams") or [{}])[0]
        return int(stream.get("width") or 0), int(stream.get("height") or 0)
    except Exception:
        return 0, 0


# ---------------------------------------------------------------------------
# 采样帧结构（纯数据，可单测）
# ---------------------------------------------------------------------------


@dataclass
class Sample:
    """单采样帧的人脸/手势观测（比例坐标 0-1）。"""

    t: float                                   # 虚拟时间轴秒
    cx: float | None = None                    # 主脸中心 x
    cy: float | None = None                    # 主脸中心 y
    fh: float = 0.0                            # 主脸高度（比例）
    area: float = 0.0                          # 主脸面积（比例）
    second_ratio: float = 0.0                  # 第二脸面积/主脸面积（多人判据）
    wrists: tuple[float, float] | None = None  # 双手腕 x（比例；None=未检测）


# ---------------------------------------------------------------------------
# 采样分析（单遍解码 + MediaPipe Face/Pose，Haar 降级）
# ---------------------------------------------------------------------------


def analyze_track(source: Path, start_ms: float, end_ms: float, *,
                  ranges: list[tuple[int, int]] | None = None,
                  env: dict | None = None) -> tuple[list[Sample], list[str]]:
    """切片窗口内采样主脸轨迹（虚拟时间轴；净化区间按映射回源采样）。

    返回 ``(samples, warnings)``。检测器缺失/抽帧失败 → 空轨迹 + warning
    （调用方走静态回退），绝不抛错。
    """
    e = os.environ if env is None else env
    warnings: list[str] = []
    try:
        import cv2
        import numpy as np
        import mediapipe as mp
    except ImportError as exc:
        return [], [f"重构图依赖缺失，回退静态裁切: {type(exc).__name__}"]

    from .subtitle_readability import _virtual_to_source_ms  # 复用净化时间轴映射

    ffmpeg = _resolve_ffmpeg(e)
    proc_w = _PROC_WIDTH
    duration_s = max(0.2, (end_ms - start_ms) / 1000.0)
    # 抽帧源时间基准：净化多区间从首区间起整段解码（缝隙帧采样后不影响跟踪）
    if ranges:
        src_start_s = ranges[0][0] / 1000.0
        src_dur_s = (ranges[-1][1] - ranges[0][0]) / 1000.0
    else:
        src_start_s = start_ms / 1000.0
        src_dur_s = duration_s
    src_w, src_h = probe_dims(source, e)
    if src_w <= 0 or src_h <= 0:
        return [], ["重构图源片尺寸探测失败，回退静态裁切"]
    proc_h = max(2, int(round(proc_w * src_h / src_w)))
    frame_bytes = proc_w * proc_h * 3
    command = [ffmpeg, "-v", "error", "-an", "-sn",
               "-ss", f"{max(0.0, src_start_s):.3f}", "-t", f"{src_dur_s:.3f}",
               "-i", str(source),
               "-vf", f"fps={_SAMPLE_FPS:.3f},scale={proc_w}:{proc_h}",
               "-pix_fmt", "bgr24", "-f", "rawvideo", "-"]
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
    except Exception as exc:
        return [], [f"重构图抽帧失败，回退静态裁切: {type(exc).__name__}"]

    face_det = None
    pose = None
    try:
        face_det = mp.solutions.face_detection.FaceDetection(
            model_selection=1, min_detection_confidence=0.5)
        if gesture_enabled(e):
            try:
                pose = mp.solutions.pose.Pose(model_complexity=0,
                                              min_detection_confidence=0.5)
            except Exception:
                pose = None  # Pose 不可用 → 仅脸跟踪（不阻断）
        samples: list[Sample] = []
        idx = 0
        while True:
            raw = proc.stdout.read(frame_bytes)
            if not raw or len(raw) < frame_bytes:
                break
            t_virtual = idx / _SAMPLE_FPS
            if t_virtual > duration_s + 0.5:
                break
            frame = np.frombuffer(raw, dtype=np.uint8).reshape(proc_h, proc_w, 3)
            sample = Sample(t=t_virtual)
            det = _detect_frame_faces(frame, face_det)
            if det is not None:
                sample.cx, sample.cy, sample.fh, sample.area, sample.second_ratio = det
                if pose is not None:
                    sample.wrists = _detect_wrists(frame, pose)
            samples.append(sample)
            idx += 1
        return samples, warnings
    except Exception as exc:
        warnings.append(f"重构图分析异常降级: {type(exc).__name__}")
        return [], warnings
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass
        if face_det is not None:
            try:
                face_det.close()
            except Exception:
                pass
        if pose is not None:
            try:
                pose.close()
            except Exception:
                pass


def _detect_frame_faces(frame, face_det) -> tuple | None:
    """单帧 → (cx, cy, fh, area, second_ratio)（比例坐标）；无人脸 None。"""
    import cv2

    h, w = frame.shape[:2]
    frame_area = float(max(1, w * h))
    boxes: list[tuple[float, float, float, float, float]] = []  # (cx,cy,fw,fh,conf)
    try:
        results = face_det.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        if results.detections:
            for det in results.detections:
                box = det.location_data.relative_bounding_box
                conf = float(det.score[0]) if det.score else 0.5
                bw, bh = float(box.width), float(box.height)
                if bw > 0.01 and bh > 0.01:
                    boxes.append((float(box.xmin) + bw / 2.0,
                                  float(box.ymin) + bh / 2.0,
                                  bw, bh, conf))
    except Exception:
        return None
    if not boxes:
        return None
    scored = sorted(boxes, key=lambda b: b[2] * b[3] * b[4], reverse=True)
    cx, cy, bw, bh, _conf = scored[0]
    area = bw * bh
    second = scored[1][2] * scored[1][3] if len(scored) > 1 else 0.0
    return cx, cy, bh, area, (second / area if area > 0 else 0.0)


def _detect_wrists(frame, pose) -> tuple[float, float] | None:
    """单帧双手腕 x（比例）；未检出返回 None。"""
    import cv2

    try:
        results = pose.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        if not results.pose_landmarks:
            return None
        marks = results.pose_landmarks.landmark
        left, right = marks[15], marks[16]  # LEFT_WRIST / RIGHT_WRIST
        xs = [m.x for m in (left, right) if m.visibility > 0.5]
        if not xs:
            return None
        if len(xs) == 1:
            return (xs[0], xs[0])
        return (min(xs), max(xs))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 纯决策函数（可单测）
# ---------------------------------------------------------------------------


def decide_axes(src_w: int, src_h: int) -> tuple[str, int, int]:
    """纵横比决策：``("passthrough"| "pan_x" | "pan_y", crop_w, crop_h)``。

    宽于 9:16（±2%）→ pan_x（满高、裁宽）；高于 9:16 → pan_y（满宽、裁高）；
    接近 9:16 → passthrough（零裁切）。
    """
    if src_w <= 0 or src_h <= 0:
        return "passthrough", 0, 0
    aspect = src_w / float(src_h)
    if abs(aspect - TARGET_ASPECT) / TARGET_ASPECT <= _ASPECT_TOL:
        return "passthrough", src_w, src_h
    if aspect > TARGET_ASPECT:
        return "pan_x", _even(src_h * TARGET_ASPECT), src_h
    return "pan_y", src_w, _even(src_w / TARGET_ASPECT)


def is_multi_person(samples: list[Sample]) -> bool:
    """第二脸面积 ≥ 主脸 60% 持续 >2s → 多人保守成立（不自动切人）。"""
    run_start: float | None = None
    prev_t: float | None = None
    for s in samples:
        hot = s.second_ratio >= _SECOND_FACE_RATIO and s.cx is not None
        if hot:
            if run_start is None:
                run_start = s.t
            if prev_t is not None and s.t - prev_t > 1.0 / _SAMPLE_FPS * 4:
                run_start = s.t  # 断档重计
            prev_t = s.t
            if s.t - run_start >= _SECOND_FACE_MIN_S:
                return True
        else:
            run_start = None
            prev_t = None
    return False


def detected_ratio(samples: list[Sample]) -> float:
    if not samples:
        return 0.0
    return sum(1 for s in samples if s.cx is not None) / float(len(samples))


def merge_wrist_center(face_cx: float, wrists: tuple[float, float] | None,
                       crop_w_frac: float) -> float:
    """手势保护（MVP）：手腕在脸附近（≤1.2×窗口半宽）→ 目标中心并入手腕区间。

    远处手腕忽略（防止误抬手把画面拉飞）。无人脸/无手腕 → 原样返回。
    """
    if wrists is None:
        return face_cx
    reach = crop_w_frac / 2.0 * _WRIST_REACH_WIN_HALF_WIDTHS
    lo, hi = wrists
    near = [x for x in (lo, hi) if abs(x - face_cx) <= reach]
    if not near:
        return face_cx
    lo2, hi2 = min([face_cx] + near), max([face_cx] + near)
    return (lo2 + hi2) / 2.0


def static_y_for_face(cy: float, fh: float, crop_h_frac: float) -> float:
    """pan_y 静态纵向定位（上半身安全区）：脸高上沿以上留 ~10% 头顶空间。

    返回 crop 窗口 top 的比例坐标，调用方钳到 [0, 1-crop_h_frac]。
    """
    face_top = cy - fh / 2.0
    return face_top - 0.10 * crop_h_frac


# ---------------------------------------------------------------------------
# 主入口：spec 构建
# ---------------------------------------------------------------------------


def build_reframe_spec(source: Path, start_ms: float, end_ms: float, *,
                       ranges: list[tuple[int, int]] | None = None,
                       env: dict | None = None) -> dict:
    """分析切片窗口 → 裁切 spec（reframe.json 审计文档）。

    spec 含 ``mode``（pan_x/pan_y/static/passthrough）、偶数像素 crop 尺寸、
    x keyframes（pan_x）或静态 y（pan_y）、降级原因与统计。任何异常降级为
    静态居中或直通，绝不抛错。
    """
    e = os.environ if env is None else env
    src_w, src_h = probe_dims(source, e)
    axes, crop_w, crop_h = decide_axes(src_w, src_h)
    spec: dict = {
        "config": {"sample_fps": _SAMPLE_FPS, "proc_width": _PROC_WIDTH,
                   "gesture": gesture_enabled(e),
                   "second_face_ratio": _SECOND_FACE_RATIO,
                   "second_face_min_s": _SECOND_FACE_MIN_S},
        "src_w": src_w, "src_h": src_h,
        "crop_w": crop_w if axes != "passthrough" else src_w,
        "crop_h": crop_h if axes != "passthrough" else src_h,
        "x_keys": [], "static_y_frac": None,
        "warnings": [], "stats": {}, "mode": "passthrough", "reason": "",
    }
    if axes == "passthrough":
        spec["reason"] = "源片已接近 9:16，零裁切直通"
        return spec
    if src_w <= 0 or src_h <= 0:
        spec.update({"mode": "passthrough", "reason": "源片尺寸探测失败，直通"})
        return spec
    try:
        samples, analyze_warnings = analyze_track(source, start_ms, end_ms,
                                                  ranges=ranges, env=e)
        spec["warnings"].extend(analyze_warnings)
        crop_w_frac = crop_w / float(src_w)
        crop_h_frac = crop_h / float(src_h)
        multi = is_multi_person(samples)
        ratio = detected_ratio(samples)
        sparse = ratio < max(3.0 / max(1, len(samples)), 0.2)
        spec["stats"] = {"samples": len(samples), "detected_ratio": round(ratio, 4),
                         "multi_person": multi}
        face_samples = [s for s in samples if s.cx is not None]
        if samples and not multi and ratio >= 0.2:
            if axes == "pan_x":
                # 手势保护并入 → 目标中心序列（像素，源坐标系）
                width = float(src_w)
                merged = [
                    (s.t, merge_wrist_center(s.cx, s.wrists, crop_w_frac) * width,
                     s.area)
                    for s in samples if s.cx is not None]
                try:
                    from . import video_utils
                    keys = video_utils.build_crop_trajectory(
                        [(t, cx, a) for (t, cx, a) in merged], int(width), int(crop_w))
                except Exception as exc:
                    keys = []
                    spec["warnings"].append(f"轨迹平滑降级: {type(exc).__name__}")
                if keys:
                    spec.update({"mode": "pan_x",
                                 "x_keys": [[round(t, 3), int(x)] for t, x in keys],
                                 "reason": "单脸跟踪（SmoothDamp 平滑）"})
                    # 字幕带提示（pan_x 纵向满高，band 固定，无避让问题）
                    return spec
                spec.update({"mode": "static",
                             "reason": "轨迹信号不足，回退静态人脸框"})
                spec["static_x_frac"] = face_samples[0].cx - crop_w_frac / 2.0
                return spec
            # pan_y：静态纵向定位（上半身安全区），x=0
            med_cy = sorted(s.cy for s in face_samples)[len(face_samples) // 2]
            med_fh = sorted(s.fh for s in face_samples)[len(face_samples) // 2]
            y_frac = static_y_for_face(med_cy, med_fh, crop_h_frac)
            y_frac = max(0.0, min(y_frac, 1.0 - crop_h_frac))
            spec.update({"mode": "pan_y", "static_y_frac": round(y_frac, 4),
                         "reason": "竖高源静态纵向定位（上半身安全区）"})
            # 字幕避让检查：脸下沿是否落进成片字幕带（band ≈ 73%~81% 输出高）
            face_bottom_in_crop = (med_cy + med_fh / 2.0 - y_frac) / crop_h_frac
            if face_bottom_in_crop > 0.73:
                spec["warnings"].append(
                    f"字幕避让提示：人脸下沿（{face_bottom_in_crop:.2f}）落入成片字幕带，"
                    f"建议人工检查构图")
            return spec
        # 回退：多人保守 / 稀疏 / 无人脸 → 静态（有检出定帧主脸开头，零检出居中）
        reason = ("multi_person_conservative：第二脸持续在画，禁用动态跟踪，"
                  "定帧主脸开头位置" if multi else
                  "sparse_track：人脸检出率过低，回退静态定帧/居中")
        spec.update({"mode": "static", "reason": reason,
                     "static_x_frac": None, "static_y_frac": None})
        if axes == "pan_x" and samples and face_samples:
            spec["static_x_frac"] = face_samples[0].cx - crop_w_frac / 2.0
        return spec
    except Exception as exc:  # 任何异常 → 静态居中，绝不阻断渲染
        spec.update({"mode": "static", "reason": f"分析异常降级: {type(exc).__name__}",
                     "static_x_frac": None, "static_y_frac": None})
        return spec


# ---------------------------------------------------------------------------
# ffmpeg 滤镜串
# ---------------------------------------------------------------------------


def crop_filter(spec: dict) -> str | None:
    """spec → ffmpeg ``crop`` 滤镜段；passthrough 返回 None。

    x 表达式来自 ``build_smooth_pan_expression``（逗号已转义，可直接进滤镜链）；
    静态模式用数值（居中钳制）。
    """
    mode = spec.get("mode")
    if mode not in {"pan_x", "pan_y", "static"}:
        return None
    w, h = int(spec["crop_w"]), int(spec["crop_h"])
    src_w, src_h = int(spec["src_w"]), int(spec["src_h"])
    if mode == "pan_x":
        keys = [(float(t), int(x)) for t, x in (spec.get("x_keys") or [])]
        if keys:
            from . import video_utils
            x_expr = video_utils.build_smooth_pan_expression(keys)
            return f"crop={w}:{h}:'{x_expr}':0"
        sx = spec.get("static_x_frac")
        x0 = (max(0.0, min(sx, 1.0)) * src_w - w / 2.0) if sx is not None else (src_w - w) / 2.0
        x0 = _even(max(0.0, min(x0, src_w - w)))
        return f"crop={w}:{h}:{x0}:0"
    if mode == "pan_y":
        y_frac = spec.get("static_y_frac")
        if y_frac is None:
            y_frac = 0.0
        y0 = _even(max(0.0, min(float(y_frac), 1.0)) * (src_h - h))
        return f"crop={w}:{h}:0:{y0}"
    # static：pan_x 无轨迹 / 无人脸 → 居中
    sx = spec.get("static_x_frac")
    x0 = (max(0.0, min(sx, 1.0)) * src_w - w / 2.0) if sx is not None else (src_w - w) / 2.0
    x0 = _even(max(0.0, min(x0, max(0, src_w - w))))
    y0 = _even(max(0.0, min(float(spec.get("static_y_frac") or 0.0), 1.0)) * max(0, src_h - h))
    return f"crop={w}:{h}:{x0}:{y0}"
