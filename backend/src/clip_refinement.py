"""两步切片规划·第二步：切片精修（2026-11-05 两步制，clip_refinement）。

定位（docs/two-step-planning-design.md）：第一步范围规划（clip_planning，
提示词A）产出 clip_plan.json 后，**立即逐条切片精修**——每条切片的原始
SRT + 该切片规划信息（主题/钩子）→ 提示词B → 四段结构化输出（精修 SRT /
待核对清单 / 合规处理清单 / 结尾文字卡建议）→ cue 与纠错分段按时间轴对齐 →
``tasks/plans/<job_id>/clip_refinement.json`` 落盘。

防混机制（硬要求）：
* 系统提示词是用户提示词B**原文逐字**（docs/prompts/step2-slice-refinement.md），
  与提示词A完全独立；逐字节一致性断言见 tests/unit/test_clip_refinement.py；
* 本模块不 import 也不引用 clip_planning 的任何提示词/Agent 构建路径；
* 输出是四段 markdown 文本（B 输出不是 JSON），由本模块解析器切块、
  SRT 部分按标准语法解析后与 segment 时间轴对齐——与第一步的
  ``_SlicePlanOut`` JSON schema 完全分离。

兜底契约（2026-11-04 用户拍板）：精修失败按**切片粒度**回退原始纠错转写 +
记 warning（source=fallback 如实标注，绝不冒充），不阻断任务。
消费侧：成片烧录字幕**不动**（仍用原纠错转写）；本产物只供右侧面板展示与
文案导出精修版。
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import llm_budget
from .content_analysis import (
    LLM_CALL_TIMEOUT_SECONDS,
    _model_ready,
)

# =====================================================================
# 第二步·切片精修系统提示词——用户提示词B原文逐字，一字不动。
# 防混契约：与 docs/prompts/step2-slice-refinement.md 逐字节一致（测试断言）。
# 注意：原文含 {theme}/{hook} 字样是「输入说明」，由用户提示填真实值，
# 系统提示词原样携带，绝不可 str.format。
# =====================================================================
PROMPT_REFINE_VERBATIM = """# 角色
你是医疗科普短视频的文案精修引擎。输入为单个切片的原始 SRT（含时间码、说话人）和该切片的规划信息。任务：在不改变原意、不新增任何医学信息的前提下，把口语转写稿整理成医生能顺口重录、可直接上屏的规范 SRT，并附三份核对清单。精修只允许两种操作：删除无用的完整句子；删除句子中的语气词（啊、呃、嗯、那个、就是、然后呢等）。除此之外不得改写、润色、增删任何字词或调整语序。

# 输入
- 原始 SRT：序号 / 时间码 / 文本；说话人若未标注，按上下文判断并在输出中标注
- 切片规划信息：主题：{theme}；钩子首句：{hook}

# 一、输出形式
1. 主体为 SRT：序号 + 起止时间码 + 文本，每条一屏；时间码与原素材对齐，全程保持时间轴连续、说话顺序不变；
2. 每条文本以角色标注开头：（医生）或（家属）；
3. 单句不超过 15 个字（标点不计），一句一个意思，按口语停顿断句；原句过长拆成多条时，拆出的条目共用原句起止时间码区间逐条推进，不重叠、不留空档；拆条只允许断句，不允许增删改任何字词；
4. 没有信息价值的句子整条删除，时间码并入相邻条，最终时间码合计等于切片总时长。

# 二、删句与语气词（仅有的两种操作）
1. 语气词从句子中删除，句子其余文字逐字保留；删除语气词后无实义的句子整条删除；
2. 寒暄、重复、跑题、无信息量的句子整条删除；有用但不完整的句子保留，不得改写；
3. 禁止换词、禁止书面化改写、禁止把口语改成书面腔、禁止概括或缩写原句。

# 三、数据与数字处理
1. 数字一律逐字保留原样；无权威出处的精确数字、明显不严谨的口头数字（如"千万分之一"），将其所在句子整条删除；
2. 精确百分比不做具象化翻译，定性表达建议写入合规处理清单供人工参考，不进主体。

# 四、合规红线（违反任何一条即视为错误输出）
1. 含绝对化用语（肯定、一定、100%、根治、最好、没坏处、不用担心及其同义表达）的句子：整条删除，不得改写软化；
2. 含疗效承诺、恐慌制造、具体药品或产品推荐的句子：整条删除；涉及用药的句子保留原话，不得改写药名；
3. 个体化诊疗表述保留原话，不得改写成普遍结论；
4. 不得新增原稿中没有的医学结论、数据、病例或建议（包括解释、补充、因人而异提示），只能在删除和去语气词范围内整理；
5. 含患者姓名、住址、"年龄+病种"等可识别组合的句子：整条删除，不做句内泛化。

# 五、输出结构（依次输出四部分）
## 1. 精修 SRT
按上述规则整理后的主体。
## 2. 待核对清单
原音存疑处逐条列出：时间码 + 原文 + 存疑原因（识别错误/听不清/术语不确定），不要自行猜测补全。
## 3. 合规处理清单
逐条列出：删除的句子（时间码 + 原文 + 删除原因）；未删除但建议人工考虑软化/具象化/补"因人而异"提示的句子（时间码 + 原文 + 建议 + 依据规则）。建议只进清单，不进主体。
## 4. 结尾文字卡建议
给出 2~3 版"面诊遵医嘱"结尾卡文案，每版不超过 30 字，供人工挑选。

# 输出前自检
- 主体每个字都来自原稿：只发生过整句删除和语气词删除两种操作；无解释、无建议、无新增提示，这些只出现在清单。
- 时间码无重叠、无缺口，合计等于切片时长；
- 每条 ≤15 字（标点除外），角色标注齐全；
- 全文无绝对化用语、无新增医学信息、无未脱敏隐私。"""

REFINE_PROMPT_VERSION = "clip-refine-prompt/2026-11-08-b2"
REFINE_SCHEMA_VERSION = "clip-refinement/v1"
REFINE_ADAPTER_VERSION = "clip-refinement-adapter/v2"

# 提示词B自检规则「单句不超过 15 个字（标点不计）」：程序侧只记 warning 不改写。
REFINE_MAX_CUE_CHARS = 15
# 说话人标签（提示词B固定两类；模型按上下文推断，无 diarization）。
REFINE_SPEAKERS = ("医生", "家属")
# 语气词清单（与提示词B「啊、呃、嗯、那个、就是、然后呢等」同源）：只删不改
# 校验器对精修侧与转写基准侧同表清理后再比对——模型保留语气词也合法（两侧同删）。
REFINE_FILLER_WORDS = ("然后呢", "那个", "就是", "啊", "呃", "嗯")
# 只删不改句切分：句终止符（含省略号与半角），与用户口径（。？！）一致。
_REFINE_SENT_SPLIT_RE = re.compile(r"[。！？…!?]+")

_SECTION_TITLES = (
    ("srt", re.compile(r"^#{0,6}\s*1[\.、）\)]?\s*精修\s*SRT\s*$")),
    ("pending", re.compile(r"^#{0,6}\s*2[\.、）\)]?\s*待核对清单\s*$")),
    ("compliance", re.compile(r"^#{0,6}\s*3[\.、）\)]?\s*合规处理清单\s*$")),
    ("ending", re.compile(r"^#{0,6}\s*4[\.、）\)]?\s*结尾文字卡建议\s*$")),
)

_SRT_TIMECODE_RE = re.compile(
    r"^(\d{1,3}):([0-5]\d):([0-5]\d)(?:[.,](\d{1,3}))?$")
_SRT_TIMECODE_PAIR_RE = re.compile(
    r"^(\d{1,3}):([0-5]\d):([0-5]\d)(?:[.,](\d{1,3}))?\s*-->\s*"
    r"(\d{1,3}):([0-5]\d):([0-5]\d)(?:[.,](\d{1,3}))?\s*$")
_SPEAKER_RE = re.compile(r"^（(医生|家属)）\s*")
_PUNCT_RE = re.compile(r"[，。！？、；：,.!?;:\"'“”‘’（）()\[\]【】《》…—～~\s\-]+")


def _prompt_sha_of(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def prompt_sha256() -> str:
    """提示词B指纹（进 provenance；改一个字 → 缓存身份变化）。"""
    return _prompt_sha_of(PROMPT_REFINE_VERBATIM)


# ---------------------------------------------------------------- 时间工具
def _ms_to_srt_timecode(ms: int) -> str:
    total = max(0, int(ms))
    h, rem = divmod(total, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms_part = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms_part:03d}"


def _single_timecode_ms(value: str) -> int | None:
    match = _SRT_TIMECODE_RE.fullmatch(value.strip())
    if not match:
        return None
    def _part(h: str, m: str, s: str, ms: str | None) -> int:
        return ((int(h) * 3600 + int(m) * 60 + int(s)) * 1000) + int((ms or "0").ljust(3, "0"))
    return _part(match.group(1), match.group(2), match.group(3), match.group(4))


def _srt_timecode_to_ms(value: str) -> int | None:
    """时间码行 → 起始毫秒（兼容单时间码与 start --> end 两种输入）。"""
    pair = _srt_timecode_pair(value)
    return pair[0] if pair else _single_timecode_ms(value)


def _srt_timecode_pair(value: str) -> tuple[int, int] | None:
    """``start --> end`` 时间码行 → (起始毫秒, 结束毫秒)；非时间码行 → None。"""
    text = value.strip()
    if "-->" not in text:
        return None
    pair = _SRT_TIMECODE_PAIR_RE.fullmatch(text)
    if not pair:
        return None
    def _part(h: str, m: str, s: str, ms: str | None) -> int:
        return ((int(h) * 3600 + int(m) * 60 + int(s)) * 1000) + int((ms or "0").ljust(3, "0"))
    return (_part(pair.group(1), pair.group(2), pair.group(3), pair.group(4)),
            _part(pair.group(5), pair.group(6), pair.group(7), pair.group(8)))


def _cue_char_count(text: str) -> int:
    """提示词B的 15 字口径：标点不计。"""
    return len(_PUNCT_RE.sub("", text or ""))


# ---------------------------------------------------------------- 输入拼装
def build_clip_srt(window_segments: list[dict[str, Any]]) -> str:
    """切片窗口的纠错转写 → 原始 SRT（源片绝对时间码，供提示词B对齐）。"""
    blocks: list[str] = []
    for index, seg in enumerate(window_segments, 1):
        start_ms = int(seg.get("start_ms") or 0)
        end_ms = int(seg.get("end_ms") or 0)
        text = str(seg.get("corrected_text") or seg.get("text") or "").strip()
        if not text or end_ms <= start_ms:
            continue
        blocks.append("\n".join((
            str(index), f"{_ms_to_srt_timecode(start_ms)} --> {_ms_to_srt_timecode(end_ms)}", text)))
    return "\n\n".join(blocks)


def build_refine_user_prompt(theme: str, hook: str, clip_srt: str) -> str:
    """提示词B用户提示：规划信息（真实 theme/hook）+ 原始 SRT。"""
    return (f"切片规划信息：主题：{str(theme or '').strip()}；"
            f"钩子首句：{str(hook or '').strip()}\n\n原始 SRT：\n{clip_srt}")


# ---------------------------------------------------------------- 输出解析
def parse_refined_output(text: str) -> dict[str, Any] | None:
    """提示词B输出 → 四段结构（srt 文本 / 待核对 / 合规 / 结尾卡）。

    按标题切块（容忍 `## 1. 精修 SRT` / `## 1 精修 SRT` 等变体）；
    SRT 段解析不出任何 cue → None（调用方按切片粒度回退）。
    """
    if not isinstance(text, str) or not text.strip():
        return None
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        hit = None
        for key, pattern in _SECTION_TITLES:
            if pattern.fullmatch(stripped):
                hit = key
                break
        if hit:
            current = hit
            sections.setdefault(current, [])
            continue
        if current:
            sections[current].append(line)
    srt_text = "\n".join(sections.get("srt", [])).strip()
    cues = _parse_srt_cues(srt_text)
    if not cues:
        return None

    def _lines(key: str) -> list[str]:
        return [line.strip(" -•\t") for line in sections.get(key, [])
                if line.strip(" -•\t")]

    return {
        "srt": srt_text,
        "cues": cues,
        "checklist_pending": _lines("pending"),
        "checklist_compliance": _lines("compliance"),
        "ending_cards": _lines("ending"),
        "sections_found": sorted(sections.keys()),
    }


def _parse_srt_cues(srt_text: str) -> list[dict[str, Any]]:
    """标准 SRT → cue 列表（含说话人解析；多行文本合并为空格拼接）。

    容忍 LLM 省略块间空行：独立的纯数字行只有**下一行是时间码**时才按序号
    处理（裸数字 cue 文本如「10%」不受影响）。
    """
    cues: list[dict[str, Any]] = []
    lines = srt_text.splitlines()
    start_ms: int | None = None
    end_ms: int | None = None
    body: list[str] = []

    def flush() -> None:
        nonlocal start_ms, end_ms, body
        if start_ms is not None and body:
            cues.append(_make_cue(len(cues) + 1, start_ms, end_ms, body))
        start_ms, end_ms, body = None, None, []

    for position, line in enumerate(lines):
        stripped = line.strip()
        pair = _srt_timecode_pair(stripped)
        if pair is not None:
            flush()
            start_ms, end_ms = pair
            continue
        if start_ms is None:
            continue                      # 序号行 / 空行 / 序言杂散行
        if not stripped:
            flush()
            continue
        next_line = next((item.strip() for item in lines[position + 1:] if item.strip()), "")
        if stripped.isdigit() and _srt_timecode_pair(next_line) is not None:
            flush()                       # 省略空行时的下一块序号行
            continue
        body.append(stripped)
    flush()
    return cues


def _make_cue(index: int, start_ms: int, end_ms: int | None, body: list[str]) -> dict[str, Any]:
    text = " ".join(body).strip()
    speaker = ""
    match = _SPEAKER_RE.match(text)
    if match:
        speaker = match.group(1)
        text = text[match.end():].strip()
    return {"index": index, "start_ms": start_ms,
            "end_ms": end_ms if end_ms is not None else start_ms,
            "text": text, "speaker": speaker,
            "char_count": _cue_char_count(text)}


# ---------------------------------------------------------------- cue 对齐
def align_cues_to_segments(cues: list[dict[str, Any]], window_segments: list[dict[str, Any]],
                           clip_start_ms: int, clip_end_ms: int,
                           warnings: list[str]) -> list[dict[str, Any]]:
    """精修 cue → 纠错分段键控对齐（segment_id 权威，时间轴不断裂）。

    * 每个 cue 挂到**重叠最长的**分段；同一分段的多个 cue 文本按序拼接；
    * cue 全部落在 [0, clip_duration] 内却不在源片窗口内 → 识别为切片相对
      时间码，程序平移 clip_start_ms（记 warning）；
    * cue 超窗/倒挂/超 15 字（标点不计）→ 只记 warning（B 的自检是模型责任，
      程序如实留痕，不冒充改写）。
    """
    if not cues:
        return []
    span = clip_end_ms - clip_start_ms
    if all(int(c["start_ms"]) < clip_start_ms and int(c["end_ms"]) < clip_start_ms for c in cues) \
            and all(int(c["end_ms"]) <= span for c in cues):
        warnings.append("精修时间码为切片相对时间，已程序侧平移到源片绝对时间")
        for cue in cues:
            cue["start_ms"] = int(cue["start_ms"]) + clip_start_ms
            cue["end_ms"] = int(cue["end_ms"]) + clip_start_ms
    segments: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for seg in window_segments:
        entry = {"segment_id": str(seg.get("segment_id") or ""),
                 "start_ms": int(seg.get("start_ms") or 0),
                 "end_ms": int(seg.get("end_ms") or 0),
                 "refined_text": "", "speaker": ""}
        segments.append(entry)
        by_id[entry["segment_id"]] = entry
    for cue in cues:
        c_start, c_end = int(cue["start_ms"]), int(cue["end_ms"])
        if c_end <= c_start:
            warnings.append(f"cue#{cue['index']} 时间码倒挂（{c_start}>{c_end}），该条丢弃并留痕")
            continue
        if c_end <= clip_start_ms or c_start >= clip_end_ms:
            warnings.append(f"cue#{cue['index']} 时间码落在切片窗口外"
                            f"（[{c_start},{c_end}] vs [{clip_start_ms},{clip_end_ms}]），该条丢弃并留痕")
            continue
        best = None
        best_overlap = 0
        for seg in segments:
            overlap = min(c_end, seg["end_ms"]) - max(c_start, seg["start_ms"])
            if overlap > best_overlap:
                best, best_overlap = seg, overlap
        if best is None:
            best = min(segments, key=lambda s: min(abs(c_start - s["start_ms"]),
                                                   abs(c_end - s["end_ms"])))
            warnings.append(f"cue#{cue['index']} 与任何分段无重叠，已就近挂载到 "
                            f"{best['segment_id']}")
        parts = [p for p in (best["refined_text"], cue["text"]) if p]
        best["refined_text"] = " ".join(parts)
        if cue.get("speaker"):
            best["speaker"] = str(cue["speaker"])
        if cue.get("char_count", 0) > REFINE_MAX_CUE_CHARS:
            warnings.append(f"cue#{cue['index']} 正文 {cue['char_count']} 字超过提示词B上限 "
                            f"{REFINE_MAX_CUE_CHARS} 字（标点不计），如实留痕")
    segments = [seg for seg in segments if seg["refined_text"]]
    if not segments and window_segments:
        warnings.append("没有任何 cue 对齐到分段（时间码全部无效）")
    return segments


# ---------------------------------------------------------------- 只删不改执法
def _normalize_for_delete_only(text: str) -> str:
    """只删不改比对归一：去标点空白 + 两侧同表去语气词（保留语气词也合法）。"""
    value = _PUNCT_RE.sub("", str(text or ""))
    for _ in range(3):
        before = value
        for word in REFINE_FILLER_WORDS:
            value = value.replace(word, "")
        if value == before:
            break
    return value


def enforce_delete_only(aligned_segments: list[dict[str, Any]],
                        window_segments: list[dict[str, Any]],
                        warnings: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """「只删不改」执法（2026-11-08 用户规则，提示词B b2）。

    判断进提示词、执法进代码：把切片窗口纠错转写去标点/语气词清理为基准后，
    aligned_segments 里每一段的精修句（按句终止符切分）必须是基准的**顺序保持
    子串**——整句删除合法、语气词删/留合法；改写、润色、换词、新增即违规。
    违规段整段回退原纠错转写（speaker 一并清空，如实留痕），不冒充精修。
    返回 (segments, reverted_segment_ids)。
    """
    source_by_id = {str(seg.get("segment_id") or ""):
                    str(seg.get("corrected_text") or seg.get("text") or "").strip()
                    for seg in window_segments}
    baseline = _normalize_for_delete_only("".join(source_by_id.values()))
    if not baseline:
        return aligned_segments, []
    pointer = 0
    reverted: list[str] = []
    for seg in aligned_segments:
        violations: list[str] = []
        for chunk in _REFINE_SENT_SPLIT_RE.split(str(seg.get("refined_text") or "")):
            needle = _normalize_for_delete_only(chunk)
            if not needle:
                continue
            pos = baseline.find(needle, pointer)
            if pos < 0:
                violations.append(needle[:30])
            else:
                pointer = pos + len(needle)
        if violations:
            seg["refined_text"] = source_by_id.get(str(seg.get("segment_id") or ""), "")
            seg["speaker"] = ""
            reverted.append(str(seg.get("segment_id") or ""))
            warnings.append(f"只删不改校验：{seg['segment_id']} 含非原话内容"
                            f"（{'；'.join(violations)}），该段已回退原纠错转写")
    return aligned_segments, reverted


# ---------------------------------------------------------------- LLM 调用
# 测试桩注入口：None = 生产默认（真实文本 LLM，llm_budget 记账）。
_REFINE_LLM_FN = None
_refine_call_metadata: dict[str, Any] = {}
# 显式输出上限（与规划同一网关预留语义：必须显式设置，防 429）。
PLAN_REFINE_MAX_OUTPUT_TOKENS = max(1024, int(os.getenv("CONTENT_REFINE_MAX_TOKENS", "4096")))


def _run_refine_agent(user_prompt: str) -> tuple[str, str]:
    """单切片精修 LLM 调用，返回 (精修文本, model)。独立成函数便于测试替身替换。"""
    ready, model_name, reason = _model_ready()
    if not ready:
        raise RuntimeError(f"LLM 未配置：{reason}")
    if _REFINE_LLM_FN is not None:
        return str(_REFINE_LLM_FN(user_prompt)), model_name
    from .ai import _build_transcript_model, apply_settings_to_process_env
    from .config import get_config
    from pydantic_ai import Agent

    config = get_config()
    apply_settings_to_process_env(config.as_runtime_settings())
    settings: dict[str, Any] = {"max_tokens": PLAN_REFINE_MAX_OUTPUT_TOKENS, "temperature": 0.2}
    from . import content_analysis as ca
    if ca.THINKING_DISABLED and ca._thinking_supported is not False:
        settings["extra_body"] = {"thinking": {"type": "disabled"}}
    agent: Agent[None, str] = Agent(
        model=_build_transcript_model(config),
        output_type=str,
        system_prompt=PROMPT_REFINE_VERBATIM,
    )
    _refine_call_metadata.clear()
    result = asyncio.run(asyncio.wait_for(
        agent.run(user_prompt, model_settings=settings), timeout=LLM_CALL_TIMEOUT_SECONDS))
    _refine_call_metadata.update({"usage": llm_budget.extract_usage_details(result),
                                  "settings": copy.deepcopy(settings)})
    return str(result.output), model_name


# ---------------------------------------------------------------- 主入口
def refine_clips(plan_record: dict[str, Any], corrected_segments: list[dict[str, Any]], *,
                 job_id: str | None = None, progress=None) -> dict[str, Any]:
    """clip_plan.json + 纠错转写 → clip_refinement 记录（逐切片，失败粒度回退）。

    本函数**绝不抛出**规划类异常：任何失败都转成该切片 source=fallback +
    warnings（兜底契约）；只有计划里没有可用切片时才返回 clips={}。
    """
    started = time.monotonic()
    clips_in = [c for c in (plan_record.get("clips") or []) if isinstance(c, dict)]
    by_segment: list[dict[str, Any]] = []
    for seg in corrected_segments:
        start_ms = int(seg.get("start_ms") or 0)
        end_ms = int(seg.get("end_ms") or 0)
        text = str(seg.get("corrected_text") or seg.get("text") or "").strip()
        if text and end_ms > start_ms:
            by_segment.append({"segment_id": str(seg.get("segment_id") or ""),
                               "start_ms": start_ms, "end_ms": end_ms, "corrected_text": text})
    warnings_top: list[str] = []
    llm_error: str | None = None
    llm_calls = 0
    fallback_all = False
    try:
        _, model_probe, probe_reason = _model_ready()
        fallback_all = not model_probe
        if fallback_all:
            llm_error = probe_reason or "LLM 未配置"
    except Exception as exc:  # noqa: BLE001 — 探测失败等同未配置
        fallback_all = True
        llm_error = f"{type(exc).__name__}: {str(exc)[:200]}"

    results: dict[str, dict[str, Any]] = {}
    usage_records: list[dict[str, Any]] = []
    reserved_total = 0
    for order, clip in enumerate(clips_in, 1):
        clip_id = str(clip.get("clip_id") or f"clip-{order:02d}")
        clip_start = int(clip.get("start_ms") or 0)
        clip_end = int(clip.get("end_ms") or 0)
        window = [seg for seg in by_segment
                  if seg["end_ms"] > clip_start and seg["start_ms"] < clip_end]
        warnings: list[str] = []
        if not window:
            warnings.append("切片窗口内没有纠错分段，无法精修（如实回退空产物）")
            results[clip_id] = _fallback_entry(clip, [], warnings, reason="empty_window")
            continue
        if fallback_all:
            warnings.append(f"LLM 不可用，回退原始纠错转写（{llm_error}）")
            results[clip_id] = _fallback_entry(clip, window, warnings, reason="llm_unavailable")
            continue
        if progress:
            progress(f"切片精修（{order}/{len(clips_in)}）：{clip.get('topic') or clip_id}")
        clip_srt = build_clip_srt(window)
        user_prompt = build_refine_user_prompt(
            str(clip.get("topic") or ""), str(clip.get("hook_first_line") or ""), clip_srt)
        estimate = llm_budget.estimate_reserve(user_prompt, PLAN_REFINE_MAX_OUTPUT_TOKENS)
        reserved_total += estimate
        try:
            output, model_name = _run_refine_agent(user_prompt)
            usage = dict(_refine_call_metadata.get("usage") or
                         llm_budget.extract_usage_details(None))
            usage_records.append(usage)
            llm_budget.record_call("clip_refinement", job_id=job_id,
                                   est_reserved=estimate, usage=usage, model=model_name)
            llm_calls += 1
            parsed = parse_refined_output(output)
            if parsed is None:
                raise ValueError("精修输出解析失败：未找到可用的精修 SRT 段")
            segments = align_cues_to_segments(parsed["cues"], window, clip_start, clip_end, warnings)
            if not segments:
                raise ValueError("精修 cue 全部无法对齐到纠错分段")
            segments, reverted_ids = enforce_delete_only(segments, window, warnings)
            if len(reverted_ids) >= len(segments) and reverted_ids:
                warnings.append("全部分段未通过只删不改校验，整片回退原始纠错转写")
                results[clip_id] = _fallback_entry(clip, window, warnings,
                                                   reason="delete_only_violation")
                continue
            entry = {
                "segments": segments,
                "cues": parsed["cues"],
                "srt": parsed["srt"],
                "checklist_pending": parsed["checklist_pending"],
                "checklist_compliance": parsed["checklist_compliance"],
                "ending_cards": parsed["ending_cards"],
                "source": "llm",
                "warnings": warnings,
                "prompt_version": REFINE_PROMPT_VERSION,
                "prompt_sha256": prompt_sha256(),
                "model": model_name,
                "theme": str(clip.get("topic") or ""),
                "hook_first_line": str(clip.get("hook_first_line") or ""),
            }
            if reverted_ids:
                entry["reverted_segments"] = reverted_ids   # 句级执法留痕（部分回退）
            results[clip_id] = entry
        except Exception as exc:  # noqa: BLE001 — 兜底契约：按切片粒度回退
            warnings.append(f"精修失败，回退原始纠错转写：{type(exc).__name__}: {str(exc)[:200]}")
            results[clip_id] = _fallback_entry(clip, window, warnings, reason="refine_failed")
    fallback_count = sum(1 for item in results.values() if item.get("source") != "llm")
    if fallback_count:
        warnings_top.append(f"{fallback_count}/{len(clips_in)} 条切片精修回退原始纠错转写"
                            + (f"（{llm_error}）" if llm_error and fallback_all else ""))
    model_name_top = None
    for item in results.values():
        if item.get("model"):
            model_name_top = item["model"]
            break
    return {
        "refinement_version": REFINE_SCHEMA_VERSION,
        "prompt_version": REFINE_PROMPT_VERSION,
        "prompt_sha256": prompt_sha256(),
        "adapter_version": REFINE_ADAPTER_VERSION,
        "plan_source": plan_record.get("plan_source"),
        "model": model_name_top,
        "llm_requested": not fallback_all,
        "llm_error": llm_error,
        "llm_calls": llm_calls,
        "llm_duration_ms": int((time.monotonic() - started) * 1000),
        "llm_reserved_tokens_estimate": reserved_total,
        "llm_prompt_tokens": sum(int(row.get("prompt_tokens") or 0) for row in usage_records),
        "llm_completion_tokens": sum(int(row.get("completion_tokens") or 0) for row in usage_records),
        "llm_usage": ca_usage(usage_records),
        "clip_count": len(clips_in),
        "fallback_count": fallback_count,
        "warnings": warnings_top,
        "clips": results,
        "job_id": job_id,
        "plan_fingerprint": plan_fingerprint(plan_record),
        "created_at_utc": datetime.now(UTC).isoformat(),
    }


def ca_usage(usage_records: list[dict[str, Any]]) -> dict[str, Any]:
    """按 content_analysis 的口径聚合 usage（与 planning 记录形状一致）。"""
    from . import content_analysis as _ca
    return _ca._aggregate_usage(usage_records)


def _fallback_entry(clip: dict[str, Any], window: list[dict[str, Any]],
                    warnings: list[str], *, reason: str) -> dict[str, Any]:
    """回退条目：segments = 原始纠错转写（refined_text 即 corrected_text，如实标注）。"""
    return {
        "segments": [{"segment_id": seg["segment_id"],
                      "refined_text": seg["corrected_text"], "speaker": ""}
                     for seg in window],
        "cues": [],
        "srt": "",
        "checklist_pending": [],
        "checklist_compliance": [],
        "ending_cards": [],
        "source": "fallback",
        "fallback_reason": reason,
        "warnings": warnings,
        "prompt_version": REFINE_PROMPT_VERSION,
        "prompt_sha256": prompt_sha256(),
        "model": None,
        "theme": str(clip.get("topic") or ""),
        "hook_first_line": str(clip.get("hook_first_line") or ""),
    }


def plan_fingerprint(plan_record: dict[str, Any]) -> str:
    """计划指纹（恢复判据）：clip_id + 边界 + 计划来源。计划变 → 精修必重跑。"""
    payload = [{"clip_id": c.get("clip_id"), "start_ms": c.get("start_ms"),
                "end_ms": c.get("end_ms"), "topic": c.get("topic"),
                "hook_first_line": c.get("hook_first_line")}
               for c in (plan_record.get("clips") or []) if isinstance(c, dict)]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def refinements_root() -> Path:
    """精修目录：``STUDIO_TASKS_ROOT/plans/<job_id>/clip_refinement.json``。"""
    base = os.getenv("STUDIO_TASKS_ROOT")
    if not base:
        base = str(Path(__file__).resolve().parent.parent / "tasks")
    return Path(base) / "plans"


def save_refinement_record(job_id: str, record: dict[str, Any]) -> dict[str, Any]:
    """clip_refinement.json 原子落盘（.part + os.replace），与 clip_plan.json 同目录。"""
    directory = refinements_root() / job_id
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "clip_refinement.json"
    record["refinement_artifact_path"] = str(target)
    temporary = target.with_name(f"{target.name}.{uuid.uuid4().hex}.part")
    try:
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return {"path": str(target), "size_bytes": target.stat().st_size}


def load_refinement_record(job_id: str, *, expected_fingerprint: str | None = None) -> dict[str, Any] | None:
    """读取已持久化的精修产物（worker 重启恢复用，绝不重复调用模型）。

    文件缺失 / JSON 损坏 / clips 不是 dict / 指纹不匹配 → ``None``。
    """
    target = refinements_root() / job_id / "clip_refinement.json"
    if not target.is_file():
        return None
    try:
        record = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or not isinstance(record.get("clips"), dict):
        return None
    if expected_fingerprint and str(record.get("plan_fingerprint") or "") != expected_fingerprint:
        return None
    return record


# ---------------------------------------------------------------- 文案导出
def build_copy_markdown(job_id: str, clip_id: str, plan_record: dict[str, Any] | None,
                        refinement: dict[str, Any]) -> str:
    """切片文案导出（精修版 markdown）：带时间轴 cue + 核对清单 + 结尾卡。

    精修产物缺失或该切片为 fallback 时，回退用纠错转写分段拼装并如实标注
    「精修版未生成（fallback）」——绝不冒充精修文本。
    """
    entry = (refinement.get("clips") or {}).get(clip_id) or {}
    clip = next((c for c in ((plan_record or {}).get("clips") or [])
                 if isinstance(c, dict) and str(c.get("clip_id")) == clip_id), {}) or {}
    theme = str(clip.get("topic") or entry.get("theme") or "")
    hook = str(entry.get("hook_first_line") or clip.get("hook_first_line") or "")
    source = str(entry.get("source") or "fallback")
    lines: list[str] = [f"# {clip_id} 文案 · {theme or '未命名主题'}", ""]
    lines.append(f"- 任务：{job_id}")
    duration_s = (int(clip.get("duration_ms") or 0)) / 1000
    if duration_s:
        lines.append(f"- 切片：{clip_id} · 时长 {duration_s:.1f}s")
    if hook:
        lines.append(f"- 钩子首句：{hook}")
    lines.append(f"- 文本来源：{'模型精修版' if source == 'llm' else '精修版未生成（fallback，原始纠错转写）'}"
                 f"（{REFINE_PROMPT_VERSION}）")
    lines.append("")
    lines.append("## 带时间轴")
    lines.append("")
    if source == "llm" and entry.get("cues"):
        for cue in entry["cues"]:
            start_s = int(cue["start_ms"]) // 1000
            end_s = max(start_s + 1, int(cue["end_ms"]) // 1000)
            speaker = f"（{cue['speaker']}）" if cue.get("speaker") else ""
            lines.append(f"[{_mmss(start_s)}–{_mmss(end_s)}] {speaker}{cue['text']}")
    else:
        for seg in entry.get("segments") or []:
            lines.append(f"- [{seg.get('segment_id')}] {seg.get('refined_text')}")
    if entry.get("checklist_pending"):
        lines += ["", "## 待核对清单（原音存疑，未猜测补全）", ""]
        lines += [f"- {row}" for row in entry["checklist_pending"]]
    if entry.get("checklist_compliance"):
        lines += ["", "## 合规处理清单", ""]
        lines += [f"- {row}" for row in entry["checklist_compliance"]]
    if entry.get("ending_cards"):
        lines += ["", "## 结尾文字卡建议（供人工挑选）", ""]
        lines += [f"- {row}" for row in entry["ending_cards"]]
    if entry.get("warnings"):
        lines += ["", "> 程序留痕：" + "；".join(str(w) for w in entry["warnings"][:5])]
    lines.append("")
    return "\n".join(lines)


def _mmss(total_seconds: int) -> str:
    minutes, seconds = divmod(max(0, int(total_seconds)), 60)
    return f"{minutes:02d}:{seconds:02d}"


# ---------------------------------------------------------------- 面板数据
def build_transcript_payload(job_id: str, clip_id: str, plan_record: dict[str, Any] | None,
                             refinement: dict[str, Any]) -> dict[str, Any]:
    """右侧「切片精修文案」面板数据（2026-11-05，实施清单第 6 项消费侧）。

    行结构固定四列语义：``index / start_ms / end_ms / speaker / text``，其中
    时间码已换算成**切片相对毫秒**（模板口径 00:00–00:02；cue 原值是源片
    绝对时间，减去计划里该切片的 start_ms）。fallback 条目的 segments 没有
    时间码字段（如实回退，不编造）→ ``start_ms/end_ms`` 为 None，前端显示 —。

    精修产物缺失或该切片不存在时由路由层给 404；这里只做拼装，绝不抛错。
    计划缺失（或该切片不在计划里）时无法换算相对时间码——**不按 0 偏移
    冒充相对时间**，行时间码置 None 并追加 warning（前端时间列显示 —）。
    """
    entry = (refinement.get("clips") or {}).get(clip_id) or {}
    clip = next((c for c in ((plan_record or {}).get("clips") or [])
                 if isinstance(c, dict) and str(c.get("clip_id")) == clip_id), None)
    relative_known = clip is not None
    clip_start = int((clip or {}).get("start_ms") or 0)
    source = str(entry.get("source") or "fallback")

    rows: list[dict[str, Any]] = []
    if source == "llm" and entry.get("cues"):
        for cue in entry["cues"]:
            start_ms = int(cue.get("start_ms") or 0)
            end_ms = int(cue.get("end_ms") or 0)
            rows.append({"index": int(cue.get("index") or len(rows) + 1),
                         "start_ms": max(0, start_ms - clip_start) if relative_known else None,
                         "end_ms": max(0, end_ms - clip_start) if relative_known else None,
                         "speaker": str(cue.get("speaker") or ""),
                         "text": str(cue.get("text") or "")})
    else:
        for order, seg in enumerate(entry.get("segments") or [], 1):
            start_ms = None if seg.get("start_ms") is None else int(seg.get("start_ms") or 0)
            end_ms = None if seg.get("end_ms") is None else int(seg.get("end_ms") or 0)
            rows.append({"index": order,
                         "start_ms": None if (start_ms is None or not relative_known)
                         else max(0, start_ms - clip_start),
                         "end_ms": None if (end_ms is None or not relative_known)
                         else max(0, end_ms - clip_start),
                         "speaker": str(seg.get("speaker") or ""),
                         "text": str(seg.get("refined_text") or "")})
    warnings = [str(w) for w in (entry.get("warnings") or [])]
    if not relative_known:
        warnings.append("缺少切片计划，无法换算切片相对时间码（时间列显示为空，不按 0 偏移冒充）")
    return {
        "task_id": job_id,
        "clip_id": clip_id,
        "theme": str((clip or {}).get("topic") or entry.get("theme") or ""),
        "source": source,
        "fallback_reason": entry.get("fallback_reason"),
        "prompt_version": str(entry.get("prompt_version")
                              or refinement.get("prompt_version") or ""),
        "model": entry.get("model"),
        "rows": rows,
        "warnings": warnings,
        "checklist_pending": [str(r) for r in (entry.get("checklist_pending") or [])],
        "checklist_compliance": [str(r) for r in (entry.get("checklist_compliance") or [])],
        "ending_cards": [str(r) for r in (entry.get("ending_cards") or [])],
    }
