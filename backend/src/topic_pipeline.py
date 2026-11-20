"""选题卡生成管线（阶段3）：拆解草稿 / 原创文稿生成 / 硬校验器。

职责边界：
- 拆解（brief draft）：热点转写稿 → 选题卡字段草稿。风险边界等医疗红线字段
  不自动生成，必须由医生/策划人工填写。
- 生稿（shooting script）：选题卡 + 转写参考稿 + 医生补充说明 → 医生原创拍摄稿。
  参考稿只用于立意与结构参考，提示词明确禁止复用原句。
- 硬校验（validate_script_draft）：机器守规层，在人工审批之前运行。
  违规分两级：hard（直接拒绝，不落库）/ soft（警告，落库后随响应返回）。

LLM 调用复用 ai.py 的模型构造（runtime_config.llm，provider:model 格式），
未配置模型时显式报错，不静默降级（与工作台整体原则一致）。
"""

import json
import logging
import re
from typing import Optional

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from .ai import (
    _build_transcript_model,
    _get_missing_llm_key_error,
    _split_llm_name,
)
from .config import get_config
from .runtime_settings import apply_settings_to_process_env

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 状态与身份常量
# ---------------------------------------------------------------------------

# hot_topics.status 选题卡状态机（在原 new/used/archived 基础上扩展）
TOPIC_STATUS_NEW = "new"                      # 入库默认（豆包推送）
TOPIC_STATUS_BRIEFED = "briefed"              # 拆解完成，待医生审批
TOPIC_STATUS_DOCTOR_APPROVED = "doctor_approved"  # 医学审批通过（排期/生稿前置条件）
TOPIC_STATUS_SCHEDULED = "scheduled"          # 已生成拍摄文稿
TOPIC_STATUS_USED = "used"                    # 素材已上传/已有成片（沿用原值）
TOPIC_STATUS_ARCHIVED = "archived"            # 弃用（沿用原值）

TOPIC_STATUS_ALLOW_BRIEF_SAVE = {TOPIC_STATUS_NEW, TOPIC_STATUS_BRIEFED}
TOPIC_STATUS_ALLOW_APPROVE = {TOPIC_STATUS_NEW, TOPIC_STATUS_BRIEFED}
TOPIC_STATUS_ALLOW_GENERATE = {TOPIC_STATUS_DOCTOR_APPROVED, TOPIC_STATUS_SCHEDULED}

# scripts.origin 文稿身份：由写入通道决定，不由内容声明
SCRIPT_ORIGIN_TRANSCRIPT = "transcript_reference"  # 豆包 ingest 通道：热点转写参考稿
SCRIPT_ORIGIN_SHOOTING = "shooting_script"         # 工作台内部通道：医生原创拍摄稿

# scripts.status（沿用双库设计）：draft → approved → shooting → uploaded → rendered → published / archived

# 审批必填最小集：核心结论 / 风险边界 / 禁止表达 / 用户问题 / 建议时长
REQUIRED_BRIEF_KEYS = ("user_question", "suggested_duration_seconds")
DEFAULT_DURATION_SECONDS = 60

# 中文口播语速经验值：3.0（慢）~ 4.2（快）字/秒，默认 3.5
SPEECH_CHARS_PER_SECOND = 3.5
SPEED_RANGE = (3.0, 4.2)


# ---------------------------------------------------------------------------
# LLM 结构化输出模型
# ---------------------------------------------------------------------------


class BriefDraft(BaseModel):
    """拆解草稿：从热点转写稿提炼的选题卡字段（不含医疗红线字段）。"""

    target_audience: str = Field(default="", description="目标人群，如：新确诊高血压患者")
    content_type: str = Field(default="", description="内容类型，如：60秒口播科普")
    user_question: str = Field(default="", description="视频回答的用户问题")
    core_conclusion: str = Field(default="", description="视频要讲透的核心结论（草稿，须医生确认）")
    prerequisites: list[str] = Field(default_factory=list, description="结论成立的前提条件")
    suggested_hook_type: str = Field(default="", description="建议钩子类型：患者问题/医生结论/反常识")
    suggested_duration_seconds: int = Field(default=DEFAULT_DURATION_SECONDS, description="建议时长（秒）")
    suggested_structure: str = Field(default="", description="建议结构，如：钩子-前提-结论-CTA")
    suggested_cta: str = Field(default="", description="建议结尾引导")
    key_points: list[str] = Field(default_factory=list, description="值得保留的内容要点")


class ShootingScriptDraft(BaseModel):
    """原创拍摄稿草稿：医生可照此口播的完整文稿。"""

    title: str = Field(default="", description="文稿标题（去重与检索用）")
    hook: str = Field(default="", description="开场 5 秒钩子（口播原话）")
    body: str = Field(default="", description="口播正文")
    cta: str = Field(default="", description="结尾引导（口播原话）")
    shooting_notes: str = Field(default="", description="拍摄备注：景别/道具/时长目标")
    refs: list[str] = Field(default_factory=list, description="医学依据：指南/共识/文献条目")


# ---------------------------------------------------------------------------
# 硬校验器
# ---------------------------------------------------------------------------

# 粗筛红线：宁可漏报不可误报的保守模式（精准管控靠医生填写的 forbidden_expr）
_RED_LINE_PATTERNS = (
    (re.compile(r"(每天|每日|一次)?(吃|服用|口服)\s*\d+\s*(mg|毫克|片|粒)", re.IGNORECASE), "出现具体服药剂量表述"),
    (re.compile(r"(直接|可以|可)(自行|自己)?(停药|停用|减量|减半)"), "出现自行停药/减量类表述"),
    (re.compile(r"(不用|不需要|没必要)(去|看)(医院|医生)"), "出现劝阻就医类表述"),
    (re.compile(r"(保证|肯定|百分之?百)(治好|治愈|根治)"), "出现绝对化疗效承诺"),
)

_PUNCT_RE = re.compile(r"[\s，。、；：？！,.;:?!\"'“”‘’（）()\[\]【】·\-—_/\\]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _normalize_for_match(text: str) -> str:
    """去空白与常见标点，用于子串匹配（避免标点差异导致漏检）。"""
    return _PUNCT_RE.sub("", text or "")


def speakable_char_count(text: str) -> int:
    """可读字数：CJK 按字计，连续 ASCII 词串按 1 字近似计。"""
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    ascii_words = len(re.findall(r"[A-Za-z0-9]+", text))
    return cjk + ascii_words


def validate_script_draft(
    draft: ShootingScriptDraft,
    *,
    forbidden_expr: Optional[list[str]] = None,
    prerequisites: Optional[list[str]] = None,
    duration_seconds: int = DEFAULT_DURATION_SECONDS,
    chars_per_second: float = SPEECH_CHARS_PER_SECOND,
) -> tuple[list[str], list[str]]:
    """对生稿草稿做硬校验。

    返回 (hard_violations, soft_warnings)：
    - hard：禁止表达命中 / 医学依据缺失 / 红线模式命中 → 拒绝落库
    - soft：时长字数越界 / 前提覆盖启发式未命中 → 允许落库并随响应提示
    """
    hard: list[str] = []
    soft: list[str] = []

    full_text = " ".join(part for part in (draft.hook, draft.body, draft.cta) if part)
    normalized = _normalize_for_match(full_text)

    # 1) 禁止表达（医生逐条填写的精准红线）
    for expr in forbidden_expr or []:
        expr_clean = (expr or "").strip()
        if not expr_clean:
            continue
        if expr_clean in full_text or _normalize_for_match(expr_clean) in normalized:
            hard.append(f"命中禁止表达：{expr_clean}")

    # 2) 医学依据必须留痕
    if not [r for r in (draft.refs or []) if (r or "").strip()]:
        hard.append("缺少医学依据（refs 为空）：核心结论须可溯源")

    # 3) 粗筛红线模式
    for pattern, label in _RED_LINE_PATTERNS:
        if pattern.search(full_text):
            hard.append(f"红线表述（{label}）")

    # 4) 时长-字数约束（soft：剪辑可再取舍）
    total_chars = speakable_char_count(full_text)
    lo = int(duration_seconds * SPEED_RANGE[0])
    hi = int(duration_seconds * SPEED_RANGE[1])
    target = int(duration_seconds * chars_per_second)
    if total_chars < lo or total_chars > hi:
        soft.append(
            f"字数{total_chars}超出建议区间[{lo},{hi}]（{duration_seconds}秒目标约{target}字）"
        )

    # 5) 前提覆盖（启发式，soft：子串匹配有误报/漏报风险，最终由医生审批兜底）
    for prereq in prerequisites or []:
        tokens = [t for t in _PUNCT_RE.split(prereq or "") if len(t) >= 2]
        if not tokens:
            continue
        if not any(_normalize_for_match(t) in normalized for t in tokens):
            soft.append(f"必要前提可能未覆盖：{prereq}")

    return hard, soft


# ---------------------------------------------------------------------------
# Prompt 构造
# ---------------------------------------------------------------------------

BRIEF_DRAFT_SYSTEM_PROMPT = (
    "你是医疗科普短视频的选题策划助手。输入是一条热点视频的口播转写稿，"
    "你的任务是为我们自己的医生拆解出选题卡草稿：目标人群、用户问题、核心结论、"
    "前提条件、建议钩子类型/时长/结构/CTA、内容要点。\n"
    "规则：\n"
    "1. 只提炼与归纳，不新增医学结论；core_conclusion 必须能在转写稿中找到依据。\n"
    "2. prerequisites 填结论成立的前提（如：患者已确诊、已开始服药），没有则留空。\n"
    "3. 禁止输出具体剂量、处方调整、绝对化疗效承诺。\n"
    "4. suggested_duration_seconds 从 30/45/60 中选择。\n"
    "5. 用简体中文回答。"
)


def build_brief_draft_prompt(source_text: str) -> str:
    return f"以下是热点视频的口播转写稿，请拆解为选题卡草稿：\n\n<transcript>\n{source_text}\n</transcript>"


SCRIPT_SYSTEM_PROMPT = (
    "你是医疗科普短视频的文稿撰稿人，为一位真实医生写口播拍摄稿。\n"
    "输入包含：选题卡（目标人群/用户问题/核心结论/前提/风险边界/禁止表达/时长/结构/CTA）、"
    "一条热点参考稿（仅供立意与结构参考）、医生的补充说明。\n"
    "硬性规则：\n"
    "1. 只写医生本人可以照读的口播原话（hook/body/cta），不要写舞台指示或旁白标注。\n"
    "2. 严禁复用参考稿的原句；只允许借鉴其立意、钩子类型与结构。\n"
    "3. 严禁出现：具体剂量、处方调整方案、绝对化疗效承诺；风险边界与禁止表达一条都不能碰。\n"
    "4. 核心结论必须与选题卡一致；必要前提需在正文中交代。\n"
    "5. refs 每条写明依据来源（如：指南名称+要点）；没有可靠依据的结论不要写。\n"
    "6. 总字数贴合建议时长（中文口播约 3.5 字/秒）。用简体中文。"
)


def build_script_prompt(
    *,
    brief: dict,
    core_conclusion: str,
    risk_boundary: str,
    forbidden_expr: list[str],
    prerequisites: list[str],
    duration_seconds: int,
    reference_transcript: str,
    doctor_notes: str = "",
) -> str:
    brief_payload = {
        "目标人群": brief.get("target_audience", ""),
        "内容类型": brief.get("content_type", ""),
        "用户问题": brief.get("user_question", ""),
        "核心结论": core_conclusion,
        "必要前提": prerequisites,
        "风险边界": risk_boundary,
        "禁止表达": forbidden_expr,
        "建议钩子类型": brief.get("suggested_hook_type", ""),
        "建议时长秒": duration_seconds,
        "建议结构": brief.get("suggested_structure", ""),
        "建议CTA": brief.get("suggested_cta", ""),
    }
    parts = [
        "选题卡：\n" + json.dumps(brief_payload, ensure_ascii=False, indent=2),
    ]
    if reference_transcript:
        parts.append(
            "热点参考稿（只学立意/结构/钩子类型，禁止复用原句）：\n<reference>\n"
            + reference_transcript[:4000]
            + "\n</reference>"
        )
    if doctor_notes:
        parts.append("医生补充说明（口播习惯/称呼/强调点）：\n" + doctor_notes[:1000])
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Agent 构造（复用 ai.py 的模型装配；未配置模型时显式报错）
# ---------------------------------------------------------------------------

_brief_agent: Optional[Agent] = None
_script_agent: Optional[Agent] = None
_agent_signature: Optional[tuple] = None


def _ensure_llm_ready() -> None:
    runtime_config = get_config()
    error = _get_missing_llm_key_error(runtime_config.llm, runtime_config)
    if error:
        # 与工作台原则一致：缺依赖显式报错，不静默降级
        raise RuntimeError(f"选题卡生成需要 LLM：{error}")


def _get_agents() -> tuple[Agent, Agent]:
    global _brief_agent, _script_agent, _agent_signature
    _ensure_llm_ready()  # 先 fail-fast：缺模型配置时显式报错，不再读其余属性
    runtime_config = get_config()
    signature = (
        runtime_config.llm,
        runtime_config.openai_api_key,
        runtime_config.google_api_key,
        runtime_config.anthropic_api_key,
        runtime_config.ollama_base_url,
        runtime_config.ollama_api_key,
    )
    if _brief_agent is None or _script_agent is None or _agent_signature != signature:
        _ensure_llm_ready()
        apply_settings_to_process_env(runtime_config.as_runtime_settings())
        model = _build_transcript_model(runtime_config)
        provider, _ = _split_llm_name(runtime_config.llm)
        retries = 2 if provider == "ollama" else 2
        _brief_agent = Agent(
            model=model,
            output_type=BriefDraft,
            system_prompt=BRIEF_DRAFT_SYSTEM_PROMPT,
            output_retries=retries,
        )
        _script_agent = Agent(
            model=model,
            output_type=ShootingScriptDraft,
            system_prompt=SCRIPT_SYSTEM_PROMPT,
            output_retries=retries,
        )
        _agent_signature = signature
    return _brief_agent, _script_agent


async def run_brief_draft(source_text: str) -> BriefDraft:
    """热点转写稿 → 选题卡草稿（不落库，由策划修改后保存）。"""
    brief_agent, _ = _get_agents()
    result = await brief_agent.run(build_brief_draft_prompt(source_text))
    return result.output


async def run_script_generation(
    *,
    brief: dict,
    core_conclusion: str,
    risk_boundary: str,
    forbidden_expr: list[str],
    prerequisites: list[str],
    duration_seconds: int,
    reference_transcript: str,
    doctor_notes: str = "",
) -> ShootingScriptDraft:
    """选题卡 → 医生原创拍摄稿草稿（落库前必须通过硬校验 + 医生审批）。"""
    _, script_agent = _get_agents()
    prompt = build_script_prompt(
        brief=brief,
        core_conclusion=core_conclusion,
        risk_boundary=risk_boundary,
        forbidden_expr=forbidden_expr,
        prerequisites=prerequisites,
        duration_seconds=duration_seconds,
        reference_transcript=reference_transcript,
        doctor_notes=doctor_notes,
    )
    result = await script_agent.run(prompt)
    return result.output
