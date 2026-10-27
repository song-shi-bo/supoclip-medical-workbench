"""realframe_v2 固化封面管线：真实视频帧 + 文字叠加（纯本地，零 AI 图片生成）。

定位（Owner 固化需求，2026-10-18）
--------------------------------
自动剪辑每条切片渲染完成后**自动**生成封面：从该切片的真实视频帧里确定性选一帧，
裁成 1080x1920（9:16、等比不拉伸），叠加固化风格的文字（顶部主题白色粗体黑描边、
主钩子 #FFD91A 黄色粗体黑实心描边），并产出 0.8s 静音片头 ``cover_intro.mp4``。

**硬约束**：
- 禁止调用 AI 图片生成模型、禁止换脸/抠图/重新生成人物——画面只来自 ``ffmpeg``
  对原视频的抽帧与几何裁剪，文字只用 Pillow 本地绘制；
- 文案允许一次**文本** LLM 调用（走 ``llm_budget.record_call("cover_copy")`` 记账，
  max_tokens ≤ 512，超时复用 ``content_analysis.LLM_CALL_TIMEOUT_SECONDS``）；
  失败/超时/非法输出一律规则回退（``source="rules"``），管线永不因封面文案失败阻塞；
- 封面文字语义：intro 独立 0.8s、正文从其后开始，封面文字在正文开始前完全退出，
  不得与人物字幕同屏（烧录字幕属于 transform 导出层；本模块把 intro 严格限制在
  0.8s 内并以此记录 layout，接线层复用 ``cover_replace.splice_intro`` 时长不变契约）。

流程（与规格逐条对应）
--------------------
1. 抽帧：仅在 ``[clip_start_ms, clip_end_ms]`` 内每隔 ``FRAME_STEP_SECONDS``（~2s）
   抽候选帧（``ffmpeg -ss`` 快 seek，候选上限 ``MAX_FRAME_CANDIDATES``；临时 png
   放系统 temp，用完即清理）。
2. 选帧：cv2 Haar 级联人脸检测（``cv2.data.haarcascades`` 自带，启动时验证存在）；
   确定性评分 = 人脸数（1 张最佳）+ 人脸面积占比 + 清晰度（Laplacian 方差，全帧 +
   人脸区）；低方差按运动模糊降权；**无脸帧也可入选**（降权并标注 ``no_face``）。
   「表情自然/闭眼」不做情绪判断，以清晰度方差作代理——此为文档明示的近似。
3. 裁剪：裁剪窗宽 = 源高 × (1080/1920)，x 中心 = 人脸 x 中心（无脸 = 画面中心），
   越界钳制；等比缩放不拉伸（先等比放大到高 1920，再居中裁到宽 1080）。
4. 文案：一次文本 LLM 调用产 3 组候选 ``{topic(2-6汉字), hook(≤12汉字),
   segment_ids(证据)}``，严格 JSON；约束写入 prompt（最多两行、必须由证据支持、
   禁通用词「健康科普」、禁虚构诊断/药物/数字/疗效/恐吓）。
5. 自动选一组：确定性评分（topic/hook 与证据段关键词重合度 + 长度合规），平局取
   第一组；选择依据写进 ``cover_copy.json``。
6. Pillow 绘制 1080x1920 ``cover.jpg``；CJK 字体沿字幕管线既有解析路径
   （``font_registry``：fonts/ 目录 + 系统 CJK 路径），绘制前用 ``getmask`` 逐字
   验证字形，缺字换下一个字体；全部缺失 → 报 ``E_COVER_CJK_FONT_MISSING`` 并列出
   fonts/ 全清单，绝不静默出豆腐块。字号与坐标全部写入 layout。
7. ``cover_intro.mp4``：cover.jpg loop 0.8s、无音轨、结尾 ~0.18s 淡出
   （``fade=t=out:st=0.62:d=0.18``）、yuv420p + faststart。
8. 幂等：键 = ``project_id + clip_id + cover_revision``；同键且四产物 sha 齐全 →
   直接返回缓存结果；重新制作 = revision+1，旧产物转 tasks 级
   ``.purge-trash`` 回收站（复用 ``studio_project_purge.TRASH_DIRNAME``），
   新产物发布失败时旧封面原样保留。

产物（四个，均返回路径 + sha256）
--------------------------------
``cover.jpg`` / ``cover_intro.mp4`` / ``cover_copy.json``（最终文案 + 其余候选 +
证据 segment_ids + 抽帧时间 + 裁剪矩形 + style=realframe_v2）/
``cover_layout.json``（字号/坐标/安全区/人脸框）。

医生贴纸：本期只做参数位（``sticker``）与文档，不实现合成。
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from .cover_replace import (
    CoverReplaceError,
    E_COVER_BAD_REQUEST,
    E_COVER_QC_FAILED,
    E_COVER_RENDER_FAILED,
    E_COVER_SOURCE_MISSING,
    _atomic_json,
    _cjk_len,
    _is_punct,
    _run,
    _sha256,
    resolve_ffmpeg,
    resolve_ffprobe,
)
from . import llm_budget

STYLE_ID = "realframe_v2"

# 画布与安全区（与 clip_render 9:16 导出画布同规格）
COVER_WIDTH = 1080
COVER_HEIGHT = 1920
SIDE_MARGIN = 72                                  # 左右安全边距（像素）
HOOK_WIDTH_RATIO = 0.8                            # 主钩子宽度 ≈ 安全画幅 80%
BOTTOM_SAFE_RATIO = 0.15                          # 底部 15% 平台安全区
THEME_TOP_RATIO = 0.08                            # 顶部主题基线起点

# 抽帧参数
FRAME_STEP_SECONDS = float(os.getenv("COVER_FRAME_STEP_SECONDS", "2.0"))
MAX_FRAME_CANDIDATES = int(os.getenv("COVER_MAX_FRAME_CANDIDATES", "40"))

# 片头（与 cover_replace.DEFAULT_INTRO_DURATION_S / 风格配置一致）
INTRO_DURATION_S = 0.8
INTRO_FADE_OUT_S = 0.18
INTRO_FPS = 25

# 文案 LLM（文本调用，禁的是图片生成；预算与超时都从既有链路取）
COPY_MAX_TOKENS = 512
COPY_CANDIDATE_COUNT = 3
HOOK_MAX_TOTAL_CHARS = 12
TOPIC_MAX_CHARS = 6
# 通用词/恐吓词黑名单（命中即弃用该候选）
COPY_FORBIDDEN_TERMS = ("健康科普", "专家提醒", "震惊", "必看", "转发救人")

# 运动模糊降权阈值（Laplacian 方差经验值：低于它视为疑似运动模糊）
MOTION_BLUR_VARIANCE = 60.0

LEDGER_FILENAME = "cover_realframe_v2.json"
COVER_JPG_NAME = "cover.jpg"
COVER_INTRO_NAME = "cover_intro.mp4"
COVER_COPY_NAME = "cover_copy.json"
COVER_LAYOUT_NAME = "cover_layout.json"
PRODUCT_NAMES = (COVER_JPG_NAME, COVER_INTRO_NAME, COVER_COPY_NAME, COVER_LAYOUT_NAME)

E_COVER_CJK_FONT_MISSING = "E_COVER_CJK_FONT_MISSING"
E_COVER_FRAME_EXTRACT_FAILED = "E_COVER_FRAME_EXTRACT_FAILED"

# 文案生成函数注入点（测试桩替换它即可零预算零真实调用）
CopyLlmFn = Callable[[str], str]

# 字幕管线既有 CJK 字体解析顺序（与 clip_render._subtitle_font 同一清单）
CJK_FONT_CANDIDATES = (
    "MicrosoftYaHeiBold.ttc", "wqy-zenhei.ttc", "unifont.otf",
    "NotoSansCJKsc-Regular.otf", "SourceHanSansSC-Regular.otf",
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# 1. 抽帧时间规划与提取
# ---------------------------------------------------------------------------

def plan_frame_times(clip_start_ms: int, clip_end_ms: int, *,
                     step_seconds: float = FRAME_STEP_SECONDS,
                     max_candidates: int = MAX_FRAME_CANDIDATES) -> list[dict[str, float]]:
    """切片内抽帧时刻表：相对时刻（封面语义用）+ 绝对时刻（源视频 seek 用）。

    首帧固定取切片起点 +0.2s（避开首帧黑场/转场），之后每 ``step_seconds`` 一帧，
    终点前不少于 0.2s；候选数超过 ``max_candidates`` 时按均匀间隔抽样压缩。
    """
    start_s = max(0.0, float(clip_start_ms) / 1000.0)
    end_s = float(clip_end_ms) / 1000.0
    if end_s <= start_s:
        raise CoverReplaceError(E_COVER_BAD_REQUEST,
                                f"切片区间非法: [{clip_start_ms}, {clip_end_ms}]")
    duration_s = end_s - start_s
    if duration_s < 1.0:                                 # 极短切片：单中点帧
        return [{"time_rel_s": round(duration_s / 2, 3),
                 "time_abs_s": round(start_s + duration_s / 2, 3),
                 "clip_duration_s": round(duration_s, 3)}]
    step = max(0.2, float(step_seconds))
    rel_times: list[float] = []
    current = 0.2
    while current < max(0.2, duration_s - 0.2) and len(rel_times) < max_candidates * 4:
        rel_times.append(round(current, 3))
        current += step
    if not rel_times:                                # 极短切片：至少抽中点一帧
        rel_times = [round(max(0.0, duration_s / 2), 3)]
    if len(rel_times) > max_candidates:              # 均匀抽样压到候选上限
        picked_indices = {round(i * (len(rel_times) - 1) / (max_candidates - 1))
                          for i in range(max_candidates)}
        rel_times = [rel_times[i] for i in sorted(picked_indices)]
    return [{"time_rel_s": t, "time_abs_s": round(start_s + t, 3),
             "clip_duration_s": round(duration_s, 3)} for t in rel_times]


def extract_candidate_frames(video: Path, planned_times: list[dict[str, float]],
                             workdir: Path) -> list[dict[str, Any]]:
    """按时刻表抽候选帧到 ``workdir``（ffmpeg -ss 快 seek，单帧输出）。

    返回 ``[{"time_rel_s", "time_abs_s", "path"}]``；个别时刻 seek 失败只跳过并
    记录（不中断），全部失败才报 ``E_COVER_FRAME_EXTRACT_FAILED``。
    """
    if not video.is_file():
        raise CoverReplaceError(E_COVER_SOURCE_MISSING, f"原视频不存在: {video}")
    ffmpeg = resolve_ffmpeg()
    workdir.mkdir(parents=True, exist_ok=True)
    extracted: list[dict[str, Any]] = []
    failures: list[str] = []
    for index, item in enumerate(planned_times):
        target = workdir / f"cand-{index:03d}-{int(item['time_rel_s'] * 1000):05d}.png"
        completed = _run([
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{item['time_abs_s']:.3f}", "-i", str(video),
            "-frames:v", "1", str(target),
        ], 120)
        if completed.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
            failures.append(f"t={item['time_abs_s']:.3f}s: {(completed.stderr or '')[-120:]}")
            target.unlink(missing_ok=True)
            continue
        extracted.append({"time_rel_s": item["time_rel_s"],
                          "time_abs_s": item["time_abs_s"], "path": target})
    if not extracted:
        raise CoverReplaceError(E_COVER_FRAME_EXTRACT_FAILED,
                                f"抽帧全部失败（{len(planned_times)} 个候选）: "
                                + "；".join(failures[:3]))
    return extracted


# ---------------------------------------------------------------------------
# 2. 选帧（确定性打分，全记录）
# ---------------------------------------------------------------------------

def haar_cascade_path() -> Path:
    """Haar 级联文件路径（cv2 自带数据，缺失时显式报错，绝不静默无人脸）。"""
    import cv2

    path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
    if not path.is_file():
        raise CoverReplaceError(E_COVER_RENDER_FAILED,
                                f"Haar 人脸级联缺失（{path}），无法按规格选帧；"
                                "请重装 opencv-python 或预置级联文件")
    return path


def detect_faces(image_bgr: Any, cascade: Any) -> list[list[int]]:
    """Haar 正脸检测：返回 ``[x, y, w, h]`` 列表（确定性参数，不做追踪）。"""
    import cv2

    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.equalizeHist(gray)
    faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5,
                                     minSize=(max(24, gray.shape[1] // 20),
                                              max(24, gray.shape[0] // 20)))
    return [list(map(int, face)) for face in faces]


def _laplacian_variance(image_bgr: Any, box: list[int] | None = None) -> float:
    """清晰度代理：Laplacian 方差（全帧或指定人脸区）。"""
    import cv2
    import numpy as np

    region = image_bgr
    if box is not None:
        x, y, w, h = box
        region = image_bgr[max(0, y):y + h, max(0, x):x + w]
    if region.size == 0:
        return 0.0
    gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def score_frame(image_bgr: Any, cascade: Any, *, index: int, time_rel_s: float,
                path: Path) -> dict[str, Any]:
    """单帧确定性评分（全记录，供 cover_copy.json / layout 审计）。

    - 人脸项：1 张最佳（3.0），2 张次之（2.0），0 张降权（1.0 且标 ``no_face``），
      3 张及以上 1.2；
    - 面积项：人脸面积占比 ×2（封顶 2.0）；
    - 清晰度项：``log1p(方差)`` 全帧与人脸区均值归一（0~3）；
    - 运动模糊：全帧方差 < ``MOTION_BLUR_VARIANCE`` 时总分 ×0.6 并标记。
    表情自然/闭眼不做情绪判断，以清晰度方差作代理（文档明示的近似）。
    """
    height, width = image_bgr.shape[:2]
    faces = detect_faces(image_bgr, cascade)
    face_area_ratio = min(1.0, sum(w * h for _, _, w, h in faces) / float(width * height))
    sharpness_full = _laplacian_variance(image_bgr)
    sharpness_face = (sum(_laplacian_variance(image_bgr, face) for face in faces) / len(faces)
                      if faces else None)
    face_term = {0: 1.0, 1: 3.0, 2: 2.0}.get(len(faces), 1.2)
    area_term = face_area_ratio * 2.0
    clarity = (math.log1p(sharpness_full) + math.log1p(sharpness_face or sharpness_full)) / 2
    clarity_term = min(3.0, clarity / math.log1p(1000.0) * 3.0)
    score = face_term + area_term + clarity_term
    motion_blur_suspect = sharpness_full < MOTION_BLUR_VARIANCE
    if motion_blur_suspect:
        score *= 0.6
    return {
        "index": int(index),
        "time_rel_s": float(time_rel_s),
        "path": str(path),
        "width": int(width),
        "height": int(height),
        "face_count": len(faces),
        "faces": faces,
        "face_area_ratio": round(face_area_ratio, 6),
        "sharpness_full": round(sharpness_full, 3),
        "sharpness_face": round(sharpness_face, 3) if sharpness_face is not None else None,
        "no_face": len(faces) == 0,
        "motion_blur_suspect": bool(motion_blur_suspect),
        "score": round(score, 4),
    }


def rank_frames(scored: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """确定性排序：分数降序、同分按抽帧序号升序（平局永远可复现）。"""
    return sorted(scored, key=lambda item: (-float(item["score"]), int(item["index"])))


# ---------------------------------------------------------------------------
# 3. 裁剪（9:16 → 1080x1920，等比不拉伸）
# ---------------------------------------------------------------------------

def plan_crop_frame(frame_width: int, frame_height: int,
                    focus_x: float | None = None) -> dict[str, int]:
    """裁剪矩形：宽 = 源高×(1080/1920)，x 中心 = ``focus_x``（无脸=画面中心），越界钳制。"""
    if frame_width <= 0 or frame_height <= 0:
        raise CoverReplaceError(E_COVER_BAD_REQUEST,
                                f"帧尺寸非法: {frame_width}x{frame_height}")
    # ceil 保证裁剪窗等比放大到高 1920 后宽度 ≥1080（round 会有 0.3% 级别拉伸）
    crop_w = min(frame_width, int(math.ceil(frame_height * COVER_WIDTH / COVER_HEIGHT)))
    center = frame_width / 2.0 if focus_x is None else float(focus_x)
    x = int(round(center - crop_w / 2.0))
    x = max(0, min(x, frame_width - crop_w))
    return {"x": int(x), "y": 0, "w": int(crop_w), "h": int(frame_height),
            "out_w": COVER_WIDTH, "out_h": COVER_HEIGHT,
            "focus_x": None if focus_x is None else round(float(focus_x), 2)}


def crop_to_cover(image_bgr: Any, crop: dict[str, int]) -> Any:
    """按裁剪矩形取窗 → 等比缩放到高 1920 → 居中裁到宽 1080（全程不拉伸）。"""
    import cv2

    x, y, w, h = crop["x"], crop["y"], crop["w"], crop["h"]
    window = image_bgr[y:y + h, x:x + w]
    scaled_w = max(COVER_WIDTH, int(round(window.shape[1] * COVER_HEIGHT / window.shape[0])))
    resized = cv2.resize(window, (scaled_w, COVER_HEIGHT), interpolation=cv2.INTER_AREA)
    offset = (scaled_w - COVER_WIDTH) // 2
    return resized[:, offset:offset + COVER_WIDTH]


# ---------------------------------------------------------------------------
# 4/5. 文案候选（LLM 一次文本调用 + 规则回退）与确定性选择
# ---------------------------------------------------------------------------

def clip_transcript_text(segments: list[dict[str, Any]]) -> str:
    """切片内转写正文（拼接分段文本；空分段跳过）。"""
    return "".join(str(item.get("text") or "") for item in segments).strip()


def copy_system_prompt() -> str:
    return (
        "你是医疗健康类短视频封面文案助手。只依据给出的转写文本提炼，"
        "输出严格 JSON，不要输出任何解释、Markdown 代码块或多余文本。"
        "格式：{\"candidates\":[{\"topic\":\"2-6个汉字\",\"hook\":\"不超过12个汉字\","
        "\"segment_ids\":[\"证据分段id\"]}]}，恰好 3 组。"
        "约束：主题与钩子必须由证据分段支持；最多两行；禁用通用词（如“健康科普”）；"
        "禁止虚构诊断、药物名称、具体数字、疗效承诺与恐吓表述。"
    )


def copy_user_prompt(segments: list[dict[str, Any]], keywords: list[str]) -> str:
    """一次文本 LLM 调用的用户提示词（分段带 id，便于模型给证据）。"""
    lines = [f"[{item.get('segment_id')}] {str(item.get('text') or '').strip()}"
             for item in segments if str(item.get("text") or "").strip()]
    if keywords:
        lines.append(f"【内容分析关键词，可优先参考】{'、'.join(str(k) for k in keywords[:10])}")
    return "转写文本：\n" + "\n".join(lines[:120])


def request_copy_from_llm(prompt: str, *, job_id: str | None = None) -> str:
    """真实文本 LLM 调用（模块默认实现；测试桩整体替换本函数 → 零预算零网络）。

    记账：``llm_budget.record_call("cover_copy", ...)`` 成功/失败都记；超时复用
    ``content_analysis.LLM_CALL_TIMEOUT_SECONDS``；max_tokens ≤ 512。
    """
    from .ai import _build_transcript_model, apply_settings_to_process_env, _get_missing_llm_key_error
    from .config import get_config
    from .content_analysis import LLM_CALL_TIMEOUT_SECONDS
    from pydantic_ai import Agent

    config = get_config()
    model_name = str(config.llm or "").strip()
    missing = _get_missing_llm_key_error(model_name, config) if model_name else "LLM 未配置"
    if missing:
        raise RuntimeError(f"LLM 不可用: {missing}")
    apply_settings_to_process_env(config.as_runtime_settings())
    agent: Agent[None, str] = Agent(model=_build_transcript_model(config),
                                    output_type=str, system_prompt=copy_system_prompt())

    async def _call(settings: dict[str, Any]) -> Any:
        return await asyncio.wait_for(agent.run(prompt, model_settings=settings),
                                      timeout=LLM_CALL_TIMEOUT_SECONDS)

    settings: dict[str, Any] = {"max_tokens": COPY_MAX_TOKENS, "temperature": 0.2,
                                "extra_body": {"thinking": {"type": "disabled"}}}
    try:
        try:
            result = asyncio.run(_call(settings))
        except Exception as exc:  # noqa: BLE001 — 网关拒参时降级一次（429 配额不重试）
            if "429" in str(exc)[:64]:
                raise
            settings.pop("extra_body", None)
            result = asyncio.run(_call(settings))
        usage = llm_budget.extract_usage(result)
        llm_budget.record_call("cover_copy", job_id=job_id, est_reserved=llm_budget.estimate_reserve(
            prompt, COPY_MAX_TOKENS), usage=usage, model=model_name, note="realframe_v2 文案候选")
        return str(result.output if hasattr(result, "output") else result)
    except Exception as exc:  # noqa: BLE001 — 失败同样记账，再交由上层规则回退
        llm_budget.record_call("cover_copy", job_id=job_id, est_reserved=llm_budget.estimate_reserve(
            prompt, COPY_MAX_TOKENS), model=model_name, note=f"realframe_v2 文案失败: {exc}"[:200])
        raise


def _strip_json_payload(raw: str) -> Any:
    """剥掉可能的 Markdown 代码栅栏后解析 JSON（严格：解析失败直接抛 ValueError）。"""
    text = str(raw or "").strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    return json.loads(text)


def _valid_candidate(entry: Any) -> dict[str, Any] | None:
    """单条候选校验与规整：topic 2-6 汉字、hook ≤12 汉字、segment_ids 是字符串列表。"""
    if not isinstance(entry, dict):
        return None
    topic = re.sub(r"\s+", "", str(entry.get("topic") or ""))
    hook = re.sub(r"\s+", "", str(entry.get("hook") or ""))
    if not (2 <= _cjk_len(topic) <= TOPIC_MAX_CHARS) or not (1 <= _cjk_len(hook) <= HOOK_MAX_TOTAL_CHARS):
        return None
    if any(term in topic or term in hook for term in COPY_FORBIDDEN_TERMS):
        return None
    segment_ids = entry.get("segment_ids")
    if not isinstance(segment_ids, list) or not segment_ids:
        return None
    segment_ids = [str(s) for s in segment_ids if str(s).strip()]
    if not segment_ids:
        return None
    return {"topic": topic, "hook": hook, "segment_ids": segment_ids, "source": "llm"}


def parse_copy_candidates(raw: str) -> list[dict[str, Any]]:
    """严格解析 LLM 输出：恰好可得 1-3 组合法候选，否则抛 ``ValueError``（触发回退）。"""
    payload = _strip_json_payload(raw)
    entries = payload.get("candidates") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        raise ValueError("文案输出不是候选列表")
    valid = [candidate for item in entries
             if (candidate := _valid_candidate(item)) is not None][:COPY_CANDIDATE_COUNT]
    if not valid:
        raise ValueError("文案输出没有合法候选")
    return valid


def rules_copy_candidates(segments: list[dict[str, Any]],
                          keywords: list[str] | None = None) -> list[dict[str, Any]]:
    """规则回退：content_analysis 关键词 + 切片内前几句确定性提取（``source="rules"``）。

    - topic：取在切片文本中出现的分析关键词（截到 6 字）；都没有则用首句前 6 字；
    - hook：切片内前几句里长度合规的句子截到 12 字；
    - segment_ids：支撑该候选的分段 id（关键词/句子所在分段）。
    """
    text = clip_transcript_text(segments)
    support: dict[str, list[str]] = {}
    for item in segments:
        body = str(item.get("text") or "")
        for keyword in [str(k) for k in (keywords or []) if str(k).strip()]:
            if keyword and keyword in body:
                support.setdefault(keyword, []).append(str(item.get("segment_id") or ""))
    sentences: list[tuple[str, str]] = []             # (句子, segment_id)
    for item in segments:
        body = str(item.get("text") or "").strip()
        if not body:
            continue
        segment_id = str(item.get("segment_id") or "")
        for sentence in re.split(r"[。！？；;!\?\n]+", body):
            sentence = sentence.strip()
            if sentence:
                sentences.append((sentence, segment_id))

    candidates: list[dict[str, Any]] = []
    used_topics: set[str] = set()
    for keyword in [str(k) for k in (keywords or []) if str(k).strip()]:
        topic = keyword[:TOPIC_MAX_CHARS]
        if topic in used_topics or _cjk_len(topic) < 2 or (text and topic not in text):
            continue
        if not sentences:                            # 没有句子可当钩子：该关键词无法成组
            continue
        used_topics.add(topic)
        hook, hook_source = _rules_hook(sentences, keyword)
        candidates.append({
            "topic": topic, "hook": hook, "source": "rules",
            "segment_ids": list(dict.fromkeys(support.get(keyword, []) + [hook_source])),
        })
        if len(candidates) >= COPY_CANDIDATE_COUNT:
            return candidates
    if not candidates and sentences:                 # 关键词全不可用：用首句兜底一组
        sentence, segment_id = sentences[0]
        topic = re.sub(r"\s+", "", sentence)[:TOPIC_MAX_CHARS]
        if _cjk_len(topic) >= 2:
            candidates.append({"topic": topic,
                               "hook": _clamp_hook(sentence), "source": "rules",
                               "segment_ids": [segment_id]})
    return candidates


def _rules_hook(sentences: list[tuple[str, str]], keyword: str) -> tuple[str, str]:
    """优先取含关键词的句子做钩子，否则取第一句；无句可取时返回空钩子。"""
    pool = [(sentence, segment_id) for sentence, segment_id in sentences if keyword in sentence]
    pool = pool or sentences
    if not pool:
        return "", ""
    sentence, segment_id = pool[0]
    return _clamp_hook(sentence), segment_id


def _clamp_hook(sentence: str) -> str:
    """句子截到 ≤12 汉字额度（按 cover_replace 固化规则计数），保住可读边界。"""
    kept: list[str] = []
    count = 0
    for char in sentence:
        if count >= HOOK_MAX_TOTAL_CHARS:
            break
        kept.append(char)
        if not char.isspace() and not _is_punct(char):
            count += 1
    hook = "".join(kept).strip()
    return hook if hook else sentence[:HOOK_MAX_TOTAL_CHARS]


def select_copy_candidate(candidates: list[dict[str, Any]], segments: list[dict[str, Any]],
                          keywords: list[str] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """确定性选一组：证据关键词重合度 + 长度合规 + 证据可回放，平局取第一组。"""
    if not candidates:
        raise CoverReplaceError(E_COVER_RENDER_FAILED, "没有可用的封面文案候选")
    text = clip_transcript_text(segments)
    keyword_list = [str(k) for k in (keywords or []) if str(k).strip()]
    segment_ids = {str(item.get("segment_id") or "") for item in segments}

    def score(candidate: dict[str, Any]) -> tuple[float, int]:
        combined = f"{candidate.get('topic')}{candidate.get('hook')}"
        overlap = sum(1 for keyword in keyword_list if keyword and keyword in combined)
        evidence = sum(1 for sid in candidate.get("segment_ids") or [] if sid in segment_ids)
        grounded = 1 if text and all(char in text for char in combined if "\u4e00" <= char <= "\u9fff") else 0
        length_ok = 1 if 2 <= _cjk_len(str(candidate.get("topic"))) <= TOPIC_MAX_CHARS else 0
        return (float(overlap) * 2 + grounded + evidence * 0.5 + length_ok, 0)

    scored = [(score(candidate), index, candidate) for index, candidate in enumerate(candidates)]
    best_score, best_index, best = max(scored, key=lambda item: (item[0], -item[1]))
    report = {
        "rule": "关键词重合度×2 + 证据可回放×0.5 + 忠于转写 + 长度合规；平局取第一组",
        "scores": [{"index": index, "topic": candidate.get("topic"),
                    "hook": candidate.get("hook"), "score": round(float(value[0]), 3)}
                   for value, index, candidate in scored],
        "selected_index": int(best_index),
    }
    return best, report


def generate_copy_candidates(segments: list[dict[str, Any]], keywords: list[str] | None,
                             *, llm_fn: CopyLlmFn | None = None,
                             job_id: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """文案候选入口：LLM 一次 → 失败/超时/非法 → 规则回退（永不抛出、永不阻塞）。"""
    meta: dict[str, Any] = {"llm_attempted": False, "source": "rules", "degrade_reason": None}
    text = clip_transcript_text(segments)
    if text:
        meta["llm_attempted"] = True
        try:
            caller = llm_fn or request_copy_from_llm
            raw = caller(copy_user_prompt(segments, keywords or []))
            candidates = parse_copy_candidates(raw)
            meta["source"] = "llm"
            return candidates, meta
        except Exception as exc:  # noqa: BLE001 — LLM 失败不是管线失败
            meta["degrade_reason"] = f"{type(exc).__name__}: {exc}"[:200]
    candidates = rules_copy_candidates(segments, keywords)
    if not candidates:                                # 极端：空转写也没有 → 硬造最小占位
        candidates = [{"topic": "本期看点", "hook": "医生原声讲解", "segment_ids": [],
                       "source": "rules"}]
    return candidates, meta


# ---------------------------------------------------------------------------
# 6. CJK 字体解析（沿字幕管线既有路径）与版面计算
# ---------------------------------------------------------------------------

def list_fonts_inventory() -> list[str]:
    """fonts/ 全清单（字体缺失报错时附上，供人工排查）。"""
    from .font_registry import FONTS_DIR

    try:
        return sorted(path.name for path in FONTS_DIR.iterdir() if path.suffix.lower() in {".ttf", ".otf", ".ttc"})
    except OSError:
        return []


def resolve_cjk_font(text: str) -> tuple[Any, dict[str, Any]]:
    """为给定文案解析一个**每个字都有字形**的 CJK 字体。

    解析顺序与字幕管线一致（``font_registry.find_font_path`` 的既有候选清单 +
    系统 CJK 路径）；fonts/ 里的 Anton/Bebas 等拉丁体不在此列。每一步都用
    ``ImageFont.truetype`` 真实加载并用 ``getmask`` 逐字验证；缺字/坏文件换下一
    候选。全部失败 → ``E_COVER_CJK_FONT_MISSING``（附 fonts/ 全清单），绝不静默
    出豆腐块。
    """
    from PIL import ImageFont
    from .font_registry import SYSTEM_CJK_FONT_PATHS, find_font_path

    chars = [char for char in dict.fromkeys(str(text or "")) if not char.isspace()]
    tried: list[dict[str, Any]] = []
    candidates: list[Path] = []
    for name in CJK_FONT_CANDIDATES:
        found = find_font_path(name)
        if found is not None:
            candidates.append(Path(found))
    candidates.extend(path for path in SYSTEM_CJK_FONT_PATHS)
    seen: set[Path] = set()
    for path in candidates:
        if path in seen or not path.is_file() or path.stat().st_size == 0:
            if path not in seen:
                tried.append({"path": str(path), "ok": False, "reason": "missing_or_empty"})
            seen.add(path)
            continue
        seen.add(path)
        try:
            font = ImageFont.truetype(str(path), 48)
            missing = [char for char in chars
                       if not font.getmask(char).getbbox()]
            if missing:
                tried.append({"path": str(path), "ok": False,
                              "reason": f"missing_glyphs:{''.join(missing[:8])}"})
                continue
            family = font.getname()
            return font, {"path": str(path), "family": f"{family[0]} {family[1]}",
                          "verified_chars": len(chars)}
        except Exception as exc:  # noqa: BLE001 — 坏字体文件换下一个候选
            tried.append({"path": str(path), "ok": False, "reason": f"{type(exc).__name__}: {exc}"[:120]})
    raise CoverReplaceError(
        E_COVER_CJK_FONT_MISSING,
        "找不到覆盖封面文案全部汉字的 CJK 字体（已验证: "
        + json.dumps(tried, ensure_ascii=False)
        + "；fonts/ 清单: " + ", ".join(list_fonts_inventory()) + "）")


def _wrap_hook_lines(hook: str, font: Any, max_width: int) -> list[str]:
    """把钩子按像素宽度贪心换行（≤2 行；超宽自动缩字号由调用方循环控制）。

    两行封顶时剩余文本**并入第二行**（宁可行宽超了让调用方缩字号，绝不静默截断——
    旧实现把余量追加成第三行再切掉，钩子结尾会丢字，长文案实测丢「解读」二字）。
    """
    from PIL import ImageFont

    lines: list[str] = []
    current = ""
    for char in hook:
        candidate = current + char
        if current and font.getlength(candidate) > max_width:
            lines.append(current)
            current = char
        else:
            current = candidate
        if len(lines) == 2:                          # 两行上限：剩余原样并入第二行
            lines[-1] = lines[-1] + hook[len("".join(lines)):]
            break
    if current:
        lines.append(current)
    return lines[:2]


def compute_text_layout(base_width: int, base_height: int, topic: str, hook: str,
                        font_path: str, faces: list[list[int]], *,
                        crop: dict[str, int], sticker: dict[str, Any] | None = None,
                        theme_start_size: int = 72,
                        theme_top_ratio: float | None = None,
                        hook_start_size: int = 96,
                        ) -> dict[str, Any]:
    """确定性版面：字号自动缩放、避让人脸眼/嘴区与底部 15% 安全区；全部坐标入 layout。

    ``theme_start_size`` / ``theme_top_ratio`` / ``hook_start_size``：封面版型
    （cover_templates）可覆盖的起始字号与垂直锚点；自动缩宽与人脸避让逻辑对版型
    一视同仁。主标题起始档（hook）≥ 主题标签起始档（theme），保证「标签小、标题大」。
    """
    from PIL import Image, ImageDraw, ImageFont

    safe_width = base_width - SIDE_MARGIN * 2
    hook_target_width = int(safe_width * HOOK_WIDTH_RATIO)
    dummy = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    bottom_limit = int(base_height * (1.0 - BOTTOM_SAFE_RATIO))

    # 主题字号：从版型起始档（缺省 72）往下找首个宽度合规档
    theme_size = max(36, int(theme_start_size))
    theme_font = ImageFont.truetype(font_path, theme_size)
    while theme_size > 36 and dummy.textbbox((0, 0), topic, font=theme_font,
                                             stroke_width=3)[2] > safe_width:
        theme_size -= 4
        theme_font = ImageFont.truetype(font_path, theme_size)

    # 主钩子字号：从版型起始档（缺省 96）往下找「≤2 行且每行 ≤ 目标宽」的最大档
    hook_size = max(40, int(hook_start_size))
    hook_font = ImageFont.truetype(font_path, hook_size)
    hook_lines = _wrap_hook_lines(hook, hook_font, hook_target_width)
    while hook_size > 40:
        hook_font = ImageFont.truetype(font_path, hook_size)
        hook_lines = _wrap_hook_lines(hook, hook_font, hook_target_width)
        widest = max((dummy.textbbox((0, 0), line, font=hook_font)[2] for line in hook_lines),
                     default=0)
        if len(hook_lines) <= 2 and widest <= hook_target_width:
            break
        hook_size -= 4

    # 人脸眼/嘴区（人脸框上 72% 向外扩 24px）——文字块不得与其相交。
    # 0.72：覆盖嘴部（原 0.60 只护到眼/鼻），用户规范要求「不遮挡眼睛、嘴部」。
    avoidance_zones = [[max(0, x - 24), max(0, y - 24), x + w + 24, y + int(h * 0.72) + 24]
                       for x, y, w, h in faces]

    def _block_rect(top: int) -> tuple[int, int]:
        height_sum = 0
        widest = 0
        for index, line in enumerate([topic] + hook_lines):
            font = theme_font if index == 0 else hook_font
            stroke = 3 if index == 0 else 6
            box = dummy.textbbox((0, 0), line, font=font, stroke_width=stroke)
            widest = max(widest, box[2] - box[0])
            height_sum += (box[3] - box[1]) + (28 if index == 0 else 20)
        return widest, height_sum

    top = int(base_height * (THEME_TOP_RATIO if theme_top_ratio is None
                             else max(0.02, min(0.6, float(theme_top_ratio)))))
    avoidance_report = "none"
    width_sum, height_sum = _block_rect(top)
    for _attempt in range(40):
        overlaps = any(not (top + height_sum < zone[1] or top > zone[3])
                       for zone in avoidance_zones)
        if not overlaps or top + height_sum >= bottom_limit:
            if overlaps:
                avoidance_report = "degraded_no_clear_space"
            break
        top += 24
        avoidance_report = "shifted_below_face_zone"
    left = (base_width - width_sum) // 2
    layout = {
        "style": STYLE_ID,
        "canvas": {"width": base_width, "height": base_height},
        "safe_area": {"side_margin": SIDE_MARGIN,
                      "bottom_safe_ratio": BOTTOM_SAFE_RATIO,
                      "bottom_limit_y": bottom_limit},
        "font": {"path": font_path},
        "theme": {"text": topic, "font_size": theme_size, "color": "#FFFFFF",
                  "stroke": {"width": 3, "color": "#000000"},
                  "position": [left, top]},
        "hook": {"text": hook, "lines": hook_lines, "font_size": hook_size,
                 "color": "#FFD91A",
                 "stroke": {"width": 6, "color": "#000000"},
                 "target_width": hook_target_width,
                 "position": [left, top + (28 + theme_size)]},
        "crop": crop,
        "faces": faces,
        "avoidance": {"zones": avoidance_zones, "report": avoidance_report},
        "sticker": {"requested": sticker is not None, "applied": False,
                    "note": "医生贴纸为本期参数位，合成留待后续版本"},
        "intro": {"duration_s": INTRO_DURATION_S, "fade_out_s": INTRO_FADE_OUT_S,
                  "text_exit_before_body": True},
    }
    return layout


# ---------------------------------------------------------------------------
# 7. Pillow 绘制 cover.jpg
# ---------------------------------------------------------------------------

def apply_template_layout(layout: dict[str, Any], template: dict[str, Any]) -> dict[str, Any]:
    """把版型契约覆盖到已计算的版面上（颜色/描边入 theme/hook；装饰入 layout.template）。

    ``template`` 是 cover_templates.template_render_spec 的产物（含 layout 契约）。
    只改颜色、描边与装饰，不碰坐标与字号（那些由 compute_text_layout 的确定性
    自动缩放 + 版型起始字号/锚点决定），全部覆盖项原样记入 layout 供追溯。
    """
    spec_layout = template.get("layout") or {}
    theme_over = spec_layout.get("theme") or {}
    hook_over = spec_layout.get("hook") or {}
    theme, hook = layout["theme"], layout["hook"]
    if theme_over.get("color"):
        theme["color"] = str(theme_over["color"])
    if theme_over.get("stroke_color"):
        theme["stroke"]["color"] = str(theme_over["stroke_color"])
    if theme_over.get("stroke_width"):
        theme["stroke"]["width"] = int(theme_over["stroke_width"])
    if hook_over.get("color"):
        hook["color"] = str(hook_over["color"])
    if hook_over.get("stroke_color"):
        hook["stroke"]["color"] = str(hook_over["stroke_color"])
    if hook_over.get("stroke_width"):
        hook["stroke"]["width"] = int(hook_over["stroke_width"])
    layout["template"] = {
        "template_id": str(template.get("template_id") or ""),
        "name": str(template.get("name") or ""),
        "registry_version": str(template.get("registry_version") or ""),
        "decor": {key: value for key, value in spec_layout.items()
                  if key in {"scrim", "panel", "accent_bar", "bubbles"}},
    }
    return layout


def _template_text_block_rect(layout: dict[str, Any]) -> list[int]:
    """模板面板/强调条用的文字块外接矩形（PIL 实测 bbox，含描边）。"""
    from PIL import Image, ImageDraw, ImageFont

    font_path = str(layout["font"]["path"])
    theme, hook = layout["theme"], layout["hook"]
    dummy = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    theme_font = ImageFont.truetype(font_path, int(theme["font_size"]))
    hook_font = ImageFont.truetype(font_path, int(hook["font_size"]))
    boxes = [dummy.textbbox(tuple(theme["position"]), str(theme["text"]),
                            font=theme_font, stroke_width=int(theme["stroke"]["width"]))]
    top = int(hook["position"][1])
    for line in hook.get("lines") or []:
        box = dummy.textbbox((int(hook["position"][0]), top), str(line),
                             font=hook_font, stroke_width=int(hook["stroke"]["width"]))
        boxes.append(box)
        top += (box[3] - box[1]) + 20
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _draw_template_decor(image: "Image.Image", layout: dict[str, Any]) -> "Image.Image":
    """模板装饰（确定性、无模型）：暗幕 → 气泡 → 文字块面板 → 强调条（RGBA 合成）。"""
    from PIL import Image, ImageDraw, ImageFont

    decor = ((layout.get("template") or {}).get("decor")) or {}
    if not any(decor.get(key) for key in {"scrim", "bubbles", "panel", "accent_bar"}):
        return image
    width, height = image.size
    font_path = str(layout["font"]["path"])
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    scrim = decor.get("scrim")
    if scrim:
        band_h = max(1, int(height * float(scrim.get("height_ratio") or 0.5)))
        anchor = str(scrim.get("anchor") or "bottom")
        y0 = {"bottom": height - band_h, "middle": (height - band_h) // 2}.get(anchor, 0)
        color = tuple(int(c) for c in (scrim.get("color") or [0, 0, 0]))
        max_alpha = max(0, min(255, int(scrim.get("opacity") or 140)))
        for offset in range(band_h):
            y = y0 + offset
            if not 0 <= y < height:
                continue
            ratio = offset / max(1, band_h - 1)
            alpha = max_alpha if anchor == "middle" else int(max_alpha * (ratio if anchor == "bottom" else 1.0 - ratio))
            draw.line([(0, y), (width, y)], fill=color + (alpha,))

    for bubble in decor.get("bubbles") or []:
        cx = int(width * float(bubble.get("cx_ratio") or 0.0))
        cy = int(height * float(bubble.get("cy_ratio") or 0.0))
        radius = max(2, int(width * float(bubble.get("r_ratio") or 0.1)))
        color = tuple(int(c) for c in (bubble.get("color") or [255, 255, 255])) + (max(0, min(255, int(bubble.get("opacity") or 180))),)
        draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=color)

    block = (_template_text_block_rect(layout)
             if (decor.get("panel") or decor.get("accent_bar")) else None)
    panel = decor.get("panel")
    if panel and block:
        pad = int(panel.get("pad") or 24)
        rect = [block[0] - pad, block[1] - pad, block[2] + pad, block[3] + pad]
        fill = tuple(int(c) for c in (panel.get("fill") or [255, 255, 255])) + (max(0, min(255, int(panel.get("opacity") or 230))),)
        outline = panel.get("outline") or {}
        outline_color = tuple(int(c) for c in (outline.get("color") or [17, 24, 39])) + (255,)
        draw.rounded_rectangle(rect, radius=int(panel.get("radius") or 16), fill=fill,
                               outline=outline_color if outline.get("width") else None,
                               width=int(outline.get("width") or 0))
    bar = decor.get("accent_bar")
    if bar and block:
        bar_height = max(2, int(bar.get("height") or 10))
        bar_width = max(8, int(width * float(bar.get("width_ratio") or 0.5)))
        center_x = (block[0] + block[2]) // 2
        color = tuple(int(c) for c in (bar.get("color") or [220, 38, 38])) + (255,)
        draw.rectangle([center_x - bar_width // 2, block[3] + 12,
                        center_x + bar_width // 2, block[3] + 12 + bar_height], fill=color)

    return Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")


def draw_cover(base_png: Path, layout: dict[str, Any], out_path: Path) -> Path:
    """底帧 + 模板装饰（可选）+ 主题 + 主钩子 → cover.jpg。

    无版型：主题（白粗体黑描边）+ 主钩子（#FFD91A 粗体黑实心描边），行为不变；
    有版型：颜色/描边已被 apply_template_layout 覆盖，装饰层按契约先画。
    """
    from PIL import Image, ImageDraw, ImageFont

    image = _draw_template_decor(Image.open(base_png).convert("RGB"), layout)
    draw = ImageDraw.Draw(image)
    font_path = str(layout["font"]["path"])
    theme = layout["theme"]
    hook = layout["hook"]
    theme_font = ImageFont.truetype(font_path, int(theme["font_size"]))
    hook_font = ImageFont.truetype(font_path, int(hook["font_size"]))

    def _bold_text(position: tuple[int, int], text: str, font: Any, color: str,
                   outline_width: int, bold_width: int,
                   outline_color: str = "#000000") -> None:
        # 第一遍：契约描边色加粗到 outline+bold；第二遍：本色描边补出粗体芯。
        # 描边色来自版型契约（theme/hook.stroke.color）；无版型缺省纯黑，行为不变。
        draw.text(position, text, font=font, fill=color,
                  stroke_width=outline_width + bold_width, stroke_fill=outline_color)
        draw.text(position, text, font=font, fill=color,
                  stroke_width=bold_width, stroke_fill=color)

    _bold_text(tuple(theme["position"]), str(theme["text"]), theme_font,
               str(theme["color"]), int(theme["stroke"]["width"]), 2,
               str(theme["stroke"].get("color") or "#000000"))
    hook_top = int(hook["position"][1])
    for line in hook["lines"]:
        _bold_text((int(hook["position"][0]), hook_top), line, hook_font,
                   str(hook["color"]), int(hook["stroke"]["width"]), 2,
                   str(hook["stroke"].get("color") or "#000000"))
        box = draw.textbbox((0, 0), line, font=hook_font, stroke_width=int(hook["stroke"]["width"]))
        hook_top += (box[3] - box[1]) + 20
    out_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = out_path.with_name(f".{out_path.name}.{uuid.uuid4().hex}.tmp")
    image.save(temporary, format="JPEG", quality=90)
    temporary.replace(out_path)
    return out_path


# ---------------------------------------------------------------------------
# 8. cover_intro.mp4（0.8s 静音、结尾 0.18s 淡出、yuv420p+faststart）
# ---------------------------------------------------------------------------

def build_intro_command(ffmpeg: str, cover_jpg: Path, out_path: Path, *,
                        duration_s: float = INTRO_DURATION_S,
                        fade_out_s: float = INTRO_FADE_OUT_S,
                        fps: int = INTRO_FPS) -> list[str]:
    """cover.jpg → 0.8s 无音轨静帧片头；结尾 ``fade=t=out``，yuv420p + faststart。"""
    fade_start = round(max(0.0, duration_s - fade_out_s), 3)
    return [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-loop", "1", "-i", str(cover_jpg),
        "-vf", f"fade=t=out:st={fade_start}:d={fade_out_s},format=yuv420p",
        "-r", str(fps), "-frames:v", str(int(round(duration_s * fps))),
        "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-t", f"{duration_s:.3f}", "-movflags", "+faststart", str(out_path),
    ]


def build_cover_intro(cover_jpg: Path, out_path: Path, *, duration_s: float = INTRO_DURATION_S,
                      fade_out_s: float = INTRO_FADE_OUT_S, fps: int = INTRO_FPS) -> Path:
    """生成 0.8s 静音片头产物（语义：封面文字在正文开始前完全退出）。"""
    ffmpeg = resolve_ffmpeg()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    completed = _run(build_intro_command(ffmpeg, cover_jpg, out_path,
                                         duration_s=duration_s, fade_out_s=fade_out_s,
                                         fps=fps), 300)
    if completed.returncode != 0 or not out_path.is_file():
        raise CoverReplaceError(E_COVER_RENDER_FAILED,
                                f"cover_intro.mp4 生成失败: {(completed.stderr or '')[-300:]}")
    return out_path


def probe_intro(path: Path) -> dict[str, Any]:
    """ffprobe 验证片头产物：时长/无音轨/帧数（QC 与测试共用）。"""
    ffprobe = resolve_ffprobe()
    completed = _run([
        ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
        str(path),
    ], 120)
    if completed.returncode != 0:
        raise CoverReplaceError(E_COVER_RENDER_FAILED,
                                f"ffprobe 读取 intro 失败: {(completed.stderr or '')[-200:]}")
    payload = json.loads(completed.stdout or "{}")
    video = next((s for s in payload.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in payload.get("streams", []) if s.get("codec_type") == "audio"), None)
    return {
        "duration_s": float((video or {}).get("duration")
                            or payload.get("format", {}).get("duration") or 0.0),
        "has_audio": audio is not None,
        "width": int((video or {}).get("width") or 0),
        "height": int((video or {}).get("height") or 0),
        "frames": int((video or {}).get("nb_frames") or 0),
        "pix_fmt": (video or {}).get("pix_fmt"),
    }


# ---------------------------------------------------------------------------
# 8b. staged 成片的片头替换（重编码回退）
# ---------------------------------------------------------------------------

def splice_intro_reencode(clip_path: Path, cover_jpg: Path, *, intro_duration_s: float = 0.8,
                          fade_in_s: float = 0.18, fade_out_s: float = 0.18) -> dict[str, Any]:
    """把 staged 成片（clip_render 单次编码，0.8s 处**没有关键帧**）前 N 秒替换为封面静帧。

    ``cover_replace.splice_intro`` 的三段流拷贝契约依赖「片头边界在关键帧上」；
    staged 成片不满足（QC ``tail_frames_bit_identical`` 必然失败），所以这里走
    **等长重编码**：``concat(封面静帧 0.8s, 原片 [0.8s, end])``，音频原样保留
    （不裁剪、不位移——封面窗内播原声，与既有 head 段语义一致），总时长/分辨率/
    帧率不变。QC 断言：时长 ±1 帧、分辨率/pix_fmt/帧率不变、音轨保留、首帧 I 帧、
    0.2s 处画面 ≈ 新封面（SSIM ≥ 0.95，复用 cover_replace 的相似度判据）。
    输出先写临时文件，QC 通过才 ``os.replace``（失败原片毫发无损）。
    """
    from . import cover_replace

    ffmpeg = resolve_ffmpeg()
    before = cover_replace.probe_streams(clip_path)
    width, height, fps = before["width"], before["height"], before["fps"]
    duration = before["duration_s"]
    if duration <= intro_duration_s:
        raise CoverReplaceError(E_COVER_BAD_REQUEST,
                                f"成片 {duration:.3f}s 短于片头 {intro_duration_s:.3f}s，拒绝整片替换")
    temp_root = Path(tempfile.mkdtemp(dir=str(clip_path.parent), prefix=".cover-splice-"))
    merged = temp_root / "merged.mp4"
    try:
        fade_out_start = round(max(0.0, intro_duration_s - fade_out_s), 3)
        head_chain = (
            f"[1:v]trim=duration={intro_duration_s:.6f},setpts=PTS-STARTPTS,split=2[cvr][cvb];"
            # 封面可能不是成片同比例（如 9:16 封面配 3:4 成片）：模糊封面铺底 + 完整封面
            # 居中（contain，不裁切）。黑边会让深色封面看起来像「黑屏什么都没有」。
            f"[cvb]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},boxblur=luma_radius=24:luma_power=2:chroma_radius=12:chroma_power=1[bgb];"
            f"[cvr]scale={width}:{height}:force_original_aspect_ratio=decrease[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,"
            f"fps={fps},fade=t=in:st=0:d={fade_in_s},"
            f"fade=t=out:st={fade_out_start}:d={fade_out_s},setsar=1,format=yuv420p[head];"
            f"[0:v]trim=start={intro_duration_s:.6f},setpts=PTS-STARTPTS[tail];"
            f"[head][tail]concat=n=2:v=1:a=0[v]"
        )
        command = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(clip_path),
            # -loop 1 的静帧输入必须限时，否则 concat 永不结束（挂死）
            "-loop", "1", "-t", f"{intro_duration_s:.6f}", "-i", str(cover_jpg),
            "-filter_complex", head_chain,
            "-map", "[v]",
        ]
        if before["has_audio"]:
            command += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "128k"]
        else:
            command += ["-an"]
        command += [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
            "-r", cover_replace._fps_arg(fps),
            "-movflags", "+faststart", str(merged),
        ]
        completed = _run(command, 600)
        if completed.returncode != 0 or not merged.is_file():
            # 与既有 splice 失败同码（E_COVER_QC_FAILED）：调用方按「片头替换失败」
            # 语义回滚保旧，封面图与成片原样保留。
            raise CoverReplaceError(E_COVER_QC_FAILED,
                                    f"片头重编码替换失败: {(completed.stderr or '')[-300:]}")

        after = cover_replace.probe_streams(merged)
        after_duration = _real_duration_s(merged) or after["duration_s"]
        tolerance = max(1, int(round(fps * 0.04)))
        checks: list[dict[str, Any]] = []
        failures: list[str] = []

        def check(name: str, passed: bool, detail: str) -> None:
            checks.append({"check": name, "passed": bool(passed), "detail": detail})
            if not passed:
                failures.append(f"{name}: {detail}")

        frame_delta = abs(after["frames"] - before["frames"])
        check("total_frames_unchanged", frame_delta <= tolerance,
              f"{after['frames']} vs {before['frames']}（差 {frame_delta} 帧，容差 ±{tolerance}）")
        duration_delta = abs(after["duration_s"] - duration)
        check("duration_unchanged", duration_delta <= tolerance / fps + 1e-3,
              f"{after['duration_s']:.6f}s vs {duration:.6f}s")
        check("resolution_unchanged", (after["width"], after["height"]) == (width, height),
              f"{after['width']}x{after['height']} vs {width}x{height}")
        check("fps_unchanged", abs(after["fps"] - fps) < 1e-6, f"{after['fps']} vs {fps}")
        if before["has_audio"]:
            check("audio_preserved", after["has_audio"],
                  f"音轨 {after.get('acodec')} vs {before.get('acodec')}")
        else:
            check("audio_absent_kept", not after["has_audio"], "原片无音轨，替换后同样不引入")
        keyframes = cover_replace.keyframe_list(merged)
        check("first_frame_is_iframe", bool(keyframes) and keyframes[0]["frame"] == 0,
              f"首 I 帧={keyframes[0]['frame'] if keyframes else 'None'}")
        reference = temp_root / "expected_head.png"
        _compose_head_reference(cover_jpg, reference, width, height)
        similarity = cover_replace.frame_similarity_to_image(
            merged, min(0.2, intro_duration_s / 2), reference, temp_root)
        check("intro_frame_matches_new_cover", similarity >= 0.95,
              f"0.2s 处帧与新封面 SSIM {similarity}（阈值 0.95）")
        qc = {
            "passed": not failures, "failures": failures, "checks": checks,
            "mode": "reencode_replace", "intro_duration_s": intro_duration_s,
            "fade_in_s": fade_in_s, "fade_out_s": fade_out_s,
            "cover_similarity": similarity,
            "before": {"frames": before["frames"], "duration_s": duration,
                       "has_audio": before["has_audio"], "fps": fps},
            "after": {"frames": after["frames"], "duration_s": after_duration,
                      "has_audio": after["has_audio"], "fps": after["fps"]},
        }
        if not qc["passed"]:
            raise CoverReplaceError(E_COVER_QC_FAILED, "；".join(failures))
        os.replace(merged, clip_path)
        return qc
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 9/10. 台账与幂等（键 = project_id + clip_id + cover_revision）
# ---------------------------------------------------------------------------

def _ledger_path(out_dir: Path) -> Path:
    return out_dir / LEDGER_FILENAME


def load_ledger(out_dir: Path) -> dict[str, Any]:
    path = _ledger_path(out_dir)
    if not path.is_file():
        return {"project_id": None, "clip_id": None, "current_revision": 0, "entries": {}}
    try:
        ledger = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"project_id": None, "clip_id": None, "current_revision": 0, "entries": {}}
    ledger.setdefault("current_revision", 0)
    ledger.setdefault("entries", {})
    return ledger


def _products_complete(out_dir: Path, entry: dict[str, Any]) -> bool:
    """四产物齐全且 sha 全部对得上才算完整（不完整 = 缓存未命中）。"""
    products = entry.get("products") or {}
    for name in PRODUCT_NAMES:
        record = products.get(name)
        if not isinstance(record, dict) or not record.get("sha256"):
            return False
        path = out_dir / name
        if not path.is_file() or _sha256(path) != record["sha256"]:
            return False
    return True


def _trash_dir_for(out_dir: Path, project_id: str, clip_id: str, revision: int) -> Path:
    """tasks 级回收站（复用 studio_project_purge.TRASH_DIRNAME 常量与目录约定）。"""
    from .studio_project_purge import TRASH_DIRNAME

    tasks_root = out_dir
    for parent in [out_dir, *out_dir.parents]:
        if parent.name == "output" and parent.parent.is_dir():
            tasks_root = parent.parent
            break
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return tasks_root / TRASH_DIRNAME / f"cover-{project_id or 'p'}-{clip_id}-rev{revision:03d}-{stamp}"


def _retire_previous_products(out_dir: Path, project_id: str, clip_id: str,
                              previous_revision: int) -> dict[str, Any] | None:
    """旧封面四产物搬入回收站（新产物已就绪才调用；失败保旧由调用顺序保证）。"""
    existing = [name for name in PRODUCT_NAMES if (out_dir / name).is_file()]
    if not existing:
        return None
    trash = _trash_dir_for(out_dir, project_id, clip_id, previous_revision)
    trash.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    failures: list[str] = []
    for name in existing:
        try:
            shutil.move(str(out_dir / name), str(trash / name))
            moved.append(name)
        except OSError as exc:
            failures.append(f"{name}: {exc}")
    return {"trash_dir": str(trash), "moved": moved, "failures": failures}


def record_intro_applied(out_dir: Path, cover_revision: int, clip_final_sha256: str) -> None:
    """接线层（worker）在片头拼接成功后记录成片 sha——重启恢复防二次拼接。"""
    ledger = load_ledger(out_dir)
    entry = ledger.get("entries", {}).get(str(int(cover_revision)))
    if not isinstance(entry, dict):
        return
    entry["intro_clip_final_sha256"] = str(clip_final_sha256)
    entry["intro_applied_at"] = _now_iso()
    _atomic_json(_ledger_path(out_dir), ledger)


def intro_already_applied(out_dir: Path, clip_final: Path) -> bool:
    """当前 clip_final 是否已烧过本 revision 的片头（sha 对得上即视为已应用）。"""
    ledger = load_ledger(out_dir)
    revision = str(int(ledger.get("current_revision") or 0))
    entry = ledger.get("entries", {}).get(revision) or {}
    expected = entry.get("intro_clip_final_sha256")
    if not expected or not clip_final.is_file():
        return False
    try:
        return _sha256(clip_final) == expected
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def build_cover(video_path: Path, *, project_id: str, clip_id: str,
                clip_start_ms: int, clip_end_ms: int,
                segments: list[dict[str, Any]], out_dir: Path,
                analysis_keywords: list[str] | None = None,
                cover_revision: int | None = None,
                topic_hint: str | None = None,
                sticker: dict[str, Any] | None = None,
                job_id: str | None = None,
                llm_fn: CopyLlmFn | None = None,
                copy_override: dict[str, Any] | None = None,
                force: bool = False,
                template: dict[str, Any] | None = None) -> dict[str, Any]:
    """生成 realframe_v2 封面四产物；同键幂等返回缓存，重制 = revision+1。

    ``copy_override``：显式文案（重新制作封面/改标题场景），形如
    ``{"topic":..., "hook":..., "segment_ids":[...]}``；给出且 topic 合规时跳过
    LLM 与规则回退，``source="user_override"``。
    返回字典含每个产物的 ``path`` + ``sha256``、最终文案、选择依据、版本号与
    ``cached`` 标记；任何一步失败抛 ``CoverReplaceError``（接线层捕获后只警告
    不拦截切片发布）。
    """
    if not clip_id or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", str(clip_id)):
        raise CoverReplaceError(E_COVER_BAD_REQUEST, f"非法 clip_id: {clip_id!r}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ledger = load_ledger(out_dir)
    ledger["project_id"] = str(project_id)
    ledger["clip_id"] = str(clip_id)
    current = int(ledger.get("current_revision") or 0)
    current_entry = ledger.get("entries", {}).get(str(current)) or {}

    # 幂等：同 revision 且四产物 sha 齐全 → 直接返回缓存
    if cover_revision is None and not force and current > 0 and _products_complete(out_dir, current_entry):
        return _result_from_entry(out_dir, current_entry, cached=True, revision=current)

    revision = int(cover_revision) if cover_revision is not None else current + 1
    if revision <= 0:
        raise CoverReplaceError(E_COVER_BAD_REQUEST, f"非法 cover_revision: {revision}")

    planned_times = plan_frame_times(clip_start_ms, clip_end_ms)
    segments = [item for item in (segments or []) if str(item.get("text") or "").strip()]
    keywords = [str(k) for k in (analysis_keywords or []) if str(k).strip()]

    with tempfile.TemporaryDirectory(prefix="cover-realframe-v2-") as temp_root:
        workdir = Path(temp_root)
        extracted = extract_candidate_frames(Path(video_path), planned_times, workdir / "frames")

        # 选帧（确定性，全记录）；解码失败的候选帧跳过（不中断）
        import cv2

        cascade = cv2.CascadeClassifier(str(haar_cascade_path()))
        scored: list[dict[str, Any]] = []
        for index, item in enumerate(extracted):
            image = cv2.imread(str(item["path"]))
            if image is None:
                continue
            scored.append(score_frame(image, cascade, index=index,
                                      time_rel_s=float(item["time_rel_s"]),
                                      path=item["path"]))
        if not scored:
            raise CoverReplaceError(E_COVER_FRAME_EXTRACT_FAILED,
                                    "候选帧全部无法解码，无法选帧")
        ranked = rank_frames(scored)
        chosen = ranked[0]
        chosen_source = extracted[chosen["index"]]
        chosen_image = cv2.imread(str(chosen["path"]))
        focus_x = (sum(x + w / 2 for x, _, w, _ in chosen["faces"]) / len(chosen["faces"])
                   if chosen["faces"] else None)
        crop = plan_crop_frame(chosen["width"], chosen["height"], focus_x)
        cover_base = workdir / "cover-base.png"
        import cv2 as _cv2

        _cv2.imwrite(str(cover_base), crop_to_cover(chosen_image, crop))

        # 文案：显式覆盖 → LLM 一次 → 规则回退；选择确定性
        override = copy_override if isinstance(copy_override, dict) else None
        if override and _cjk_len(str(override.get("topic") or "")) >= 2:
            candidates = [{"topic": re.sub(r"\s+", "", str(override["topic"])),
                           "hook": _clamp_hook(str(override.get("hook") or "")),
                           "segment_ids": [str(s) for s in (override.get("segment_ids") or [])],
                           "source": "user_override"}]
            copy_meta = {"llm_attempted": False, "source": "user_override",
                         "degrade_reason": None}
        else:
            candidates, copy_meta = generate_copy_candidates(segments, keywords,
                                                             llm_fn=llm_fn, job_id=job_id)
        if topic_hint and all(str(c.get("topic")) != str(topic_hint) for c in candidates):
            hint_candidate = rules_copy_candidates(
                [{"segment_id": "hint", "text": str(topic_hint)}], [str(topic_hint)])
            candidates = candidates + hint_candidate
        selected, selection_report = select_copy_candidate(candidates, segments, keywords)
        topic = str(selected.get("topic") or "")
        hook = str(selected.get("hook") or "")

        # 字体与版面（缺字换字体；全缺 → 显式报错，绝不出豆腐块）
        font, font_report = resolve_cjk_font(topic + hook)
        template_theme = (template or {}).get("layout", {}).get("theme") or {}
        template_hook = (template or {}).get("layout", {}).get("hook") or {}
        layout = compute_text_layout(COVER_WIDTH, COVER_HEIGHT, topic, hook,
                                     font_report["path"], chosen["faces"],
                                     crop=crop, sticker=sticker,
                                     theme_start_size=int(template_theme.get("start_size") or 72),
                                     theme_top_ratio=(float(template_theme["top_ratio"])
                                                      if template_theme.get("top_ratio") is not None else None),
                                     hook_start_size=int(template_hook.get("start_size") or 96))
        layout["font"].update({"family": font_report["family"],
                               "verified_chars": font_report["verified_chars"]})
        if template:
            apply_template_layout(layout, template)

        # 产物先落在**与目标同盘**的临时目录（系统 tmp 与 tasks 常跨设备，
        # os.replace 会 EXDEV），全部成功后才发布（失败保旧）；候选帧仍走系统 tmp。
        staging = out_dir / f".cover-staging-{uuid.uuid4().hex[:10]}"
        staging.mkdir()
        try:
            draw_cover(cover_base, layout, staging / COVER_JPG_NAME)
            build_cover_intro(staging / COVER_JPG_NAME, staging / COVER_INTRO_NAME)
            copy_doc = {
                "style": STYLE_ID,
                "template": layout.get("template"),
                "project_id": str(project_id),
                "clip_id": str(clip_id),
                "cover_revision": revision,
                "final": {"topic": topic, "hook": hook,
                          "segment_ids": list(selected.get("segment_ids") or []),
                          "source": selected.get("source")},
                "selection": selection_report,
                "candidates": candidates,
                "copy_meta": copy_meta,
                "keywords_used": keywords,
                "frame": {"planned_times": planned_times,
                          "selected_time_rel_s": chosen["time_rel_s"],
                          "selected_time_abs_s": chosen_source["time_abs_s"],
                          "no_face": chosen["no_face"]},
                "crop": crop,
                "frame_scores": [{key: value for key, value in item.items() if key != "path"}
                                 for item in scored],
            }
            _atomic_json(staging / COVER_COPY_NAME, copy_doc)
            _atomic_json(staging / COVER_LAYOUT_NAME, layout)

            # 发布：旧产物先转回收站（新四产物已就绪），再同盘原子搬入
            retirement = None
            if current > 0 and revision > current:
                retirement = _retire_previous_products(out_dir, project_id, clip_id, current)
            products: dict[str, Any] = {}
            for name in PRODUCT_NAMES:
                source = staging / name
                target = out_dir / name
                os.replace(source, target)
                products[name] = {"path": str(target), "sha256": _sha256(target),
                                  "bytes": target.stat().st_size}
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    entry = {
        "revision": revision,
        "project_id": str(project_id),
        "clip_id": str(clip_id),
        "applied_at": _now_iso(),
        "style": STYLE_ID,
        "template": layout.get("template"),
        "products": products,
        "final_copy": {"topic": topic, "hook": hook,
                       "segment_ids": list(selected.get("segment_ids") or []),
                       "source": selected.get("source")},
        "frame_time_rel_s": chosen["time_rel_s"],
        "no_face": chosen["no_face"],
        "copy_source": copy_meta.get("source"),
        "retired_previous": retirement,
        "intro_clip_final_sha256": None,
    }
    ledger["current_revision"] = revision
    ledger.setdefault("entries", {})[str(revision)] = entry
    _atomic_json(_ledger_path(out_dir), ledger)
    return _result_from_entry(out_dir, entry, cached=False, revision=revision)


def _result_from_entry(out_dir: Path, entry: dict[str, Any], *, cached: bool,
                       revision: int) -> dict[str, Any]:
    """对外结果：四产物 path+sha256、最终文案、版本与缓存标记。"""
    products = entry.get("products") or {}
    return {
        "style": STYLE_ID,
        "template": entry.get("template"),
        "project_id": entry.get("project_id"),
        "clip_id": entry.get("clip_id"),
        "cover_revision": int(revision),
        "cached": bool(cached),
        "cover_path": str(out_dir / COVER_JPG_NAME),
        "cover_intro_path": str(out_dir / COVER_INTRO_NAME),
        "cover_copy_path": str(out_dir / COVER_COPY_NAME),
        "cover_layout_path": str(out_dir / COVER_LAYOUT_NAME),
        "products": {name: {"path": str(out_dir / name),
                            "sha256": (products.get(name) or {}).get("sha256"),
                            "bytes": (products.get(name) or {}).get("bytes")}
                     for name in PRODUCT_NAMES},
        "cover_copy": entry.get("final_copy") or {},
        "frame_time_rel_s": entry.get("frame_time_rel_s"),
        "no_face": entry.get("no_face"),
        "copy_source": entry.get("copy_source"),
        "retired_previous": entry.get("retired_previous"),
        "intro_clip_final_sha256": entry.get("intro_clip_final_sha256"),
    }


def _compose_head_reference(cover_jpg: Path, target: Path, width: int, height: int) -> None:
    """按与片头相同的 scale+pad 规则把封面合成成「期望的首帧」，供 QC 比对。

    成片比例与封面不一致时（9:16 封面 + 3:4 成片），直接拿原封面做 SSIM 会把
    「正常加黑边」误判成失败；比对期望帧才是真正的「片头是不是这张封面」。
    """
    ffmpeg = resolve_ffmpeg()
    completed = _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                      "-i", str(cover_jpg),
                      "-filter_complex",
                      f"[0:v]split=2[bg][fg];"
                      f"[bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
                      f"crop={width}:{height},boxblur=luma_radius=24:luma_power=2:chroma_radius=12:chroma_power=1[bgb];"
                      f"[fg]scale={width}:{height}:force_original_aspect_ratio=decrease[fgs];"
                      f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2",
                      "-frames:v", "1", str(target)], 120)
    if completed.returncode != 0 or not target.is_file():
        raise CoverReplaceError(E_COVER_QC_FAILED,
                                f"生成片头期望帧失败: {(completed.stderr or '')[-200:]}")


def _real_duration_s(path: Path) -> float:
    """用 ffprobe 直接读成片真实时长（QC 用）。

    刻意不走 ``cover_replace.probe_streams``：那个函数在单测里会被打桩成固定帧数/时长，
    而「前置片头后时长必须变长」这条断言必须是文件的真实事实，不能被桩值覆盖。
    """
    ffprobe = resolve_ffprobe()
    proc = _run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", str(path)], 120)
    try:
        return float((proc.stdout or "").strip())
    except ValueError:
        return 0.0


def append_image_still(video_path: Path, cover_jpg: Path, *, duration_s: float = 1.5) -> dict[str, Any]:
    """把封面静帧**追加到成片末尾**（正片内容原样在前，静音封面段在后）。

    用于时间线上放在正片之后的 image_clip 实例的最终渲染：总时长 = 原时长 + duration_s，
    原片音频/字幕不受影响；封面段静音。QC：时长 ±1 帧、分辨率/帧率不变、音轨保留。
    """
    from . import cover_replace

    ffmpeg = resolve_ffmpeg()
    before = cover_replace.probe_streams(video_path)
    width, height, fps = before["width"], before["height"], before["fps"]
    duration = before["duration_s"]
    temp_root = Path(tempfile.mkdtemp(dir=str(video_path.parent), prefix=".image-tail-"))
    merged = temp_root / "merged.mp4"
    try:
        tail_chain = (
            f"[0:v]setpts=PTS-STARTPTS[main];"
            f"[1:v]trim=duration={duration_s:.6f},setpts=PTS-STARTPTS,"
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"fps={fps},setsar=1,format=yuv420p[tail];"
            f"[main][tail]concat=n=2:v=1:a=0[v]"
        )
        silence_input: list[str] = []
        if before["has_audio"]:
            layout = str(before.get("channel_layout") or "mono")
            sample_rate = int(before.get("sample_rate") or 44100)
            silence_input = ["-f", "lavfi", "-t", f"{duration_s:.6f}",
                             "-i", f"anullsrc=r={sample_rate}:cl={layout}"]
            tail_chain += (
                f";[2:a]atrim=duration={duration_s:.6f},asetpts=PTS-STARTPTS[tailsil];"
                f"[0:a]asetpts=PTS-STARTPTS[mainsil];"
                # 顺序 = 正片在前、静音在后（与视频 [main][tail] 一致）；写反会把整段
                # 正片音频推到静音后面，音画完全错位（链式导出测试抓出过）。
                f"[mainsil][tailsil]concat=n=2:v=0:a=1[a]"
            )
        command = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(video_path),
            "-loop", "1", "-t", f"{duration_s:.6f}", "-i", str(cover_jpg),
            *silence_input,
            "-filter_complex", tail_chain,
            "-map", "[v]",
        ]
        if before["has_audio"]:
            command += ["-map", "[a]", "-c:a", "aac", "-b:a", "128k"]
        else:
            command += ["-an"]
        command += [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
            "-r", cover_replace._fps_arg(fps),
            "-movflags", "+faststart", str(merged),
        ]
        completed = _run(command, 600)
        if completed.returncode != 0 or not merged.is_file():
            raise CoverReplaceError(E_COVER_QC_FAILED,
                                    f"封面尾段拼接失败: {(completed.stderr or '')[-300:]}")
        after = cover_replace.probe_streams(merged)
        after_duration = _real_duration_s(merged) or after["duration_s"]
        tolerance = max(1, int(round(fps * 0.04)))
        checks: list[dict[str, Any]] = []
        failures: list[str] = []

        def check(name: str, passed: bool, detail: str) -> None:
            checks.append({"check": name, "passed": bool(passed), "detail": detail})
            if not passed:
                failures.append(f"{name}: {detail}")

        check("duration_grew_by_image",
              abs(after_duration - (duration + duration_s)) <= tolerance / fps + 1e-3,
              f"{after_duration:.6f}s vs 期望 {duration + duration_s:.6f}s")
        check("resolution_unchanged", (after["width"], after["height"]) == (width, height),
              f"{after['width']}x{after['height']} vs {width}x{height}")
        if before["has_audio"]:
            check("audio_preserved", after["has_audio"], f"音轨 {after.get('acodec')}")
        qc = {
            "passed": not failures, "failures": failures, "checks": checks,
            "mode": "append_image", "image_duration_s": duration_s,
            "before": {"duration_s": duration, "has_audio": before["has_audio"], "fps": fps},
            "after": {"duration_s": after_duration, "has_audio": after["has_audio"], "fps": fps},
        }
        if not qc["passed"]:
            raise CoverReplaceError(E_COVER_QC_FAILED, "；".join(failures))
        os.replace(merged, video_path)
        return qc
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def _audio_peak_db(path: Path, start_s: float, duration_s: float) -> float | None:
    """读 ``[start_s, start_s + duration_s]`` 窗口音频峰值（dB）；无音轨返回 None。"""
    ffmpeg = resolve_ffmpeg()
    proc = _run([ffmpeg, "-hide_banner", "-loglevel", "info",
                 "-ss", f"{max(0.0, start_s):.3f}",
                 "-t", f"{max(0.05, duration_s):.3f}", "-i", str(path),
                 "-map", "0:a:0", "-af", "volumedetect", "-f", "null", "-"], 300)
    text = (proc.stderr or "") + (proc.stdout or "")
    match = re.search(r"max_volume:\s*(-?[0-9.]+|-inf)\s*dB", text)
    if not match:
        return None
    return -999.0 if match.group(1) == "-inf" else float(match.group(1))


def insert_image_still(video_path: Path, cover_jpg: Path, *, start_s: float,
                       duration_s: float = 1.5) -> dict[str, Any]:
    """把封面静帧**插入正片中间** ``start_s`` 处：``[0..S] + 封面静帧(静音) + [S..末尾]``。

    时间线拖拽落点的最终渲染（区别于片头 `splice_intro_prepend` 与片尾
    `append_image_still`）：正片在 S 处被切开，封面静帧（模糊铺底 + 完整封面居中，
    与片头同语义）插进中间，封面段静音、前后正片音频原样保留。

    QC（不通过抛错、原片毫发无损）：总时长 = 原时长 + duration_s（±1 帧）、
    分辨率/帧率/音轨不变、插入段静音（峰值 ≤ -45dB）、插入中点帧 ≈ 封面合成帧、
    切点前一帧与原片同位置帧相似（正片内容没被挪动）。
    """
    from . import cover_replace

    ffmpeg = resolve_ffmpeg()
    before = cover_replace.probe_streams(video_path)
    width, height, fps = before["width"], before["height"], before["fps"]
    duration = before["duration_s"]
    start = max(0.0, min(float(start_s or 0.0), max(0.0, duration - 0.05)))
    if start <= 0.05:
        raise CoverReplaceError(E_COVER_BAD_REQUEST,
                                f"插入位置 {start:.3f}s 落在片头，请走片头前置通道（cover_intro）")
    if duration - start <= 0.05:
        raise CoverReplaceError(E_COVER_BAD_REQUEST,
                                f"插入位置 {start:.3f}s 已在成片末尾，请走片尾追加通道")
    fade_s = max(0.05, min(0.2, duration_s / 3))
    temp_root = Path(tempfile.mkdtemp(dir=str(video_path.parent), prefix=".image-insert-"))
    merged = temp_root / "merged.mp4"
    try:
        fade_out_start = round(max(0.0, duration_s - fade_s), 3)
        # 切点前一帧参考（重编码前从原片抽，用于「正片没被挪动」的连续性比对）。
        cut_probe_ts = max(0.0, start - min(0.1, start / 2))
        before_cut_png = temp_root / "before-cut.png"
        extracted = _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                          "-ss", f"{cut_probe_ts:.3f}", "-i", str(video_path),
                          "-frames:v", "1", str(before_cut_png)], 120)
        chain = (
            f"[1:v]trim=duration={duration_s:.6f},setpts=PTS-STARTPTS,split=2[cvr][cvb];"
            f"[cvb]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},boxblur=luma_radius=24:luma_power=2:chroma_radius=12:chroma_power=1[bgb];"
            f"[cvr]scale={width}:{height}:force_original_aspect_ratio=decrease[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,fps={fps},"
            f"fade=t=in:st=0:d={fade_s},fade=t=out:st={fade_out_start}:d={fade_s},"
            f"setsar=1,format=yuv420p[img];"
            f"[0:v]trim=start=0:end={start:.6f},setpts=PTS-STARTPTS[pre];"
            f"[0:v]trim=start={start:.6f},setpts=PTS-STARTPTS[post];"
            f"[pre][img][post]concat=n=3:v=1:a=0[v]"
        )
        silence_input: list[str] = []
        if before["has_audio"]:
            layout = str(before.get("channel_layout") or "mono")
            sample_rate = int(before.get("sample_rate") or 44100)
            silence_input = ["-f", "lavfi", "-t", f"{duration_s:.6f}",
                             "-i", f"anullsrc=r={sample_rate}:cl={layout}"]
            chain += (
                f";[2:a]atrim=duration={duration_s:.6f},asetpts=PTS-STARTPTS[sil];"
                f"[0:a]atrim=start=0:end={start:.6f},asetpts=PTS-STARTPTS[apre];"
                f"[0:a]atrim=start={start:.6f},asetpts=PTS-STARTPTS[apost];"
                f"[apre][sil][apost]concat=n=3:v=0:a=1[a]"
            )
        command = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(video_path),
            "-loop", "1", "-t", f"{duration_s:.6f}", "-i", str(cover_jpg),
            *silence_input,
            "-filter_complex", chain,
            "-map", "[v]",
        ]
        if before["has_audio"]:
            command += ["-map", "[a]", "-c:a", "aac", "-b:a", "128k"]
        else:
            command += ["-an"]
        command += [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
            "-r", cover_replace._fps_arg(fps),
            "-movflags", "+faststart", str(merged),
        ]
        completed = _run(command, 600)
        if completed.returncode != 0 or not merged.is_file():
            raise CoverReplaceError(E_COVER_QC_FAILED,
                                    f"封面插入渲染失败: {(completed.stderr or '')[-300:]}")
        after = cover_replace.probe_streams(merged)
        after_duration = _real_duration_s(merged) or after["duration_s"]
        tolerance = max(1, int(round(fps * 0.04)))
        checks: list[dict[str, Any]] = []
        failures: list[str] = []

        def check(name: str, passed: bool, detail: str) -> None:
            checks.append({"check": name, "passed": bool(passed), "detail": detail})
            if not passed:
                failures.append(f"{name}: {detail}")

        check("duration_grew_by_image",
              abs(after_duration - (duration + duration_s)) <= tolerance / fps + 1e-3,
              f"{after_duration:.6f}s vs 期望 {duration + duration_s:.6f}s")
        check("resolution_unchanged", (after["width"], after["height"]) == (width, height),
              f"{after['width']}x{after['height']} vs {width}x{height}")
        check("fps_unchanged", abs(after["fps"] - fps) < 1e-6, f"{after['fps']} vs {fps}")
        if before["has_audio"]:
            check("audio_preserved", after["has_audio"], f"音轨 {after.get('acodec')}")
            window = max(0.05, duration_s - 2 * fade_s)
            peak = _audio_peak_db(merged, start + fade_s, window)
            check("insert_audio_silent", peak is not None and peak <= -45.0,
                  f"插入段峰值 {peak} dB（要求 ≤ -45dB：封面静帧期间不得播出原声）")
        expected_cover = temp_root / "expected-insert.png"
        _compose_head_reference(cover_jpg, expected_cover, width, height)
        similarity = cover_replace.frame_similarity_to_image(
            merged, start + duration_s / 2, expected_cover, temp_root)
        check("insert_frame_matches_cover", similarity >= 0.95,
              f"插入中点帧与封面合成 SSIM {similarity}（阈值 0.95）")
        if extracted.returncode == 0 and before_cut_png.is_file():
            continuity = cover_replace.frame_similarity_to_image(
                merged, cut_probe_ts, before_cut_png, temp_root)
            check("content_before_cut_untouched", continuity >= 0.95,
                  f"切点前帧与原片 SSIM {continuity}（阈值 0.95：正片内容不得被挪动）")
        qc = {
            "passed": not failures, "failures": failures, "checks": checks,
            "mode": "insert_image", "image_duration_s": duration_s, "insert_start_s": start,
            "before": {"duration_s": duration, "has_audio": before["has_audio"], "fps": fps},
            "after": {"duration_s": after_duration, "has_audio": after["has_audio"], "fps": fps},
        }
        if not qc["passed"]:
            raise CoverReplaceError(E_COVER_QC_FAILED, "；".join(failures))
        os.replace(merged, video_path)
        return qc
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def _head_audio_peak_db(path: Path, duration_s: float) -> float | None:
    """读文件前 ``duration_s`` 秒音频的峰值（dB）；无音轨返回 None。"""
    ffmpeg = resolve_ffmpeg()
    proc = _run([ffmpeg, "-hide_banner", "-loglevel", "info",
                 "-t", f"{max(0.05, duration_s):.3f}", "-i", str(path),
                 "-map", "0:a:0", "-af", "volumedetect", "-f", "null", "-"], 300)
    text = (proc.stderr or "") + (proc.stdout or "")
    match = re.search(r"max_volume:\s*(-?[0-9.]+|-inf)\s*dB", text)
    if not match:
        return None
    return -999.0 if match.group(1) == "-inf" else float(match.group(1))


def splice_intro_prepend(clip_path: Path, cover_jpg: Path, *,
                         intro_duration_s: float = 0.8,
                         previous_intro_s: float = 0.0,
                         fade_in_s: float = 0.18,
                         fade_out_s: float = 0.18) -> dict[str, Any]:
    """把封面作为**片头加到正片前面**（时长变长 ``intro - previous``，正片内容不丢）。

    与 :func:`splice_intro_reencode`（等长替换、封面窗内仍播原声）语义不同，这里按
    「封面是正片之前独立的一段」处理：

    * 视频 = 封面静帧 ``intro_duration_s`` + 原片从 ``previous_intro_s`` 起的全部画面；
    * 音频 = 原片音频从 ``previous_intro_s`` 起，整体后移 ``intro_duration_s``
      （``adelay``），因此**片头段静音，正片音频从片头之后开始**，绝不被封面覆盖；
    * 已烧录在画面里的字幕随正片一起后移，同样从片头之后开始；
    * ``previous_intro_s`` 用于**重复应用**：旧片头先被裁掉再加新封面，不会越叠越长。

    QC（不通过就抛错、原片毫发无损）：时长 = 原时长 - previous + intro（±1 帧）、
    分辨率/帧率/像素格式不变、音轨保留、片头段峰值 ≤ -45dB（静音）、
    0.2s 处画面 ≈ 新封面（SSIM ≥ 0.95）。
    """
    from . import cover_replace

    ffmpeg = resolve_ffmpeg()
    before = cover_replace.probe_streams(clip_path)
    width, height, fps = before["width"], before["height"], before["fps"]
    duration = before["duration_s"]
    previous = max(0.0, min(float(previous_intro_s or 0.0), max(0.0, duration - 0.05)))
    if duration - previous <= 0.05:
        raise CoverReplaceError(E_COVER_BAD_REQUEST,
                                f"成片 {duration:.3f}s 扣掉旧片头后没有正片内容，拒绝应用")
    expected_duration = duration - previous + intro_duration_s
    temp_root = Path(tempfile.mkdtemp(dir=str(clip_path.parent), prefix=".cover-prepend-"))
    merged = temp_root / "merged.mp4"
    try:
        fade_out_start = round(max(0.0, intro_duration_s - fade_out_s), 3)
        head_chain = (
            f"[1:v]trim=duration={intro_duration_s:.6f},setpts=PTS-STARTPTS,split=2[cvr][cvb];"
            # 封面可能不是成片同比例（如 9:16 封面配 3:4 成片）：模糊封面铺底 + 完整封面
            # 居中（contain，不裁切）。黑边会让深色封面看起来像「黑屏什么都没有」。
            f"[cvb]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},boxblur=luma_radius=24:luma_power=2:chroma_radius=12:chroma_power=1[bgb];"
            f"[cvr]scale={width}:{height}:force_original_aspect_ratio=decrease[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,"
            f"fps={fps},fade=t=in:st=0:d={fade_in_s},"
            f"fade=t=out:st={fade_out_start}:d={fade_out_s},setsar=1,format=yuv420p[head];"
            f"[0:v]trim=start={previous:.6f},setpts=PTS-STARTPTS[tail];"
            f"[head][tail]concat=n=2:v=1:a=0[v]"
        )
        # 音频用「静音片头 + 正片音频」显式 concat：片头段一定是静音，正片音频整体后移。
        # （不用 adelay：adelay 与 -filter_complex 混用时会产生非单调 DTS，实测片头段仍带原声。）
        silence_input: list[str] = []
        if before["has_audio"]:
            layout = str(before.get("channel_layout") or "mono")
            sample_rate = int(before.get("sample_rate") or 44100)
            silence_input = ["-f", "lavfi", "-t", f"{intro_duration_s:.6f}",
                             "-i", f"anullsrc=r={sample_rate}:cl={layout}"]
            head_chain += (
                f";[2:a]atrim=duration={intro_duration_s:.6f},asetpts=PTS-STARTPTS[sil];"
                f"[0:a]atrim=start={previous:.6f},asetpts=PTS-STARTPTS[main];"
                f"[sil][main]concat=n=2:v=0:a=1[a]"
            )
        command = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(clip_path),
            "-loop", "1", "-t", f"{intro_duration_s:.6f}", "-i", str(cover_jpg),
            *silence_input,
            "-filter_complex", head_chain,
            "-map", "[v]",
        ]
        if before["has_audio"]:
            command += ["-map", "[a]", "-c:a", "aac", "-b:a", "128k"]
        else:
            command += ["-an"]
        command += [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
            "-r", cover_replace._fps_arg(fps),
            "-movflags", "+faststart", str(merged),
        ]
        completed = _run(command, 600)
        if completed.returncode != 0 or not merged.is_file():
            raise CoverReplaceError(E_COVER_QC_FAILED,
                                    f"片头前置重编码失败: {(completed.stderr or '')[-300:]}")

        after = cover_replace.probe_streams(merged)
        after_duration = _real_duration_s(merged) or after["duration_s"]
        tolerance = max(1, int(round(fps * 0.04)))
        checks: list[dict[str, Any]] = []
        failures: list[str] = []

        def check(name: str, passed: bool, detail: str) -> None:
            checks.append({"check": name, "passed": bool(passed), "detail": detail})
            if not passed:
                failures.append(f"{name}: {detail}")

        check("duration_grew_by_intro",
              abs(after_duration - expected_duration) <= tolerance / fps + 1e-3,
              f"{after_duration:.6f}s vs 期望 {expected_duration:.6f}s"
              f"（原 {duration:.3f}s，旧片头 {previous:.3f}s + 新片头 {intro_duration_s:.3f}s）")
        check("resolution_unchanged", (after["width"], after["height"]) == (width, height),
              f"{after['width']}x{after['height']} vs {width}x{height}")
        check("fps_unchanged", abs(after["fps"] - fps) < 1e-6, f"{after['fps']} vs {fps}")
        if before["has_audio"]:
            check("audio_preserved", after["has_audio"],
                  f"音轨 {after.get('acodec')} vs {before.get('acodec')}")
        else:
            check("audio_absent_kept", not after["has_audio"], "原片无音轨，前置片头同样不引入")
        if before["has_audio"]:
            peak = _head_audio_peak_db(merged, intro_duration_s - 0.05)
            check("intro_head_silent", peak is not None and peak <= -45.0,
                  f"片头段峰值 {peak} dB（要求 ≤ -45dB：正片音频不得被封面覆盖/提前）")
        reference = temp_root / "expected_head.png"
        _compose_head_reference(cover_jpg, reference, width, height)
        similarity = cover_replace.frame_similarity_to_image(
            merged, min(0.2, intro_duration_s / 2), reference, temp_root)
        check("intro_frame_matches_new_cover", similarity >= 0.95,
              f"0.2s 处帧与新封面 SSIM {similarity}（阈值 0.95）")
        qc = {
            "passed": not failures, "failures": failures, "checks": checks,
            "mode": "prepend", "intro_duration_s": intro_duration_s,
            "previous_intro_s": previous, "fade_in_s": fade_in_s, "fade_out_s": fade_out_s,
            "cover_similarity": similarity,
            "before": {"frames": before["frames"], "duration_s": duration,
                       "has_audio": before["has_audio"], "fps": fps},
            "after": {"frames": after["frames"], "duration_s": after_duration,
                      "has_audio": after["has_audio"], "fps": after["fps"]},
        }
        if not qc["passed"]:
            raise CoverReplaceError(E_COVER_QC_FAILED, "；".join(failures))
        os.replace(merged, clip_path)
        return qc
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)
