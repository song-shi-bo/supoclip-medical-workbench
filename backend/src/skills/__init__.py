"""Skill 适配器包：把散落在 prompt / 配置里的「固化技能」变成可被编排器调用的工具。

对外契约：
- 每个 ``run_*_skill(ctx, **params) -> SkillResult``；
- 规则一律来自 manifest（经 skill_registry 优先级解析），适配器内不写死；
- 未实现的技能显式抛 ``E_SKILL_NOT_IMPLEMENTED``，绝不伪实现、绝不静默降级；
- ``PIPELINE_ORDER`` 定义自动剪辑的依赖顺序，编排器必须按序调用。
"""

from __future__ import annotations

from .base import (
    SkillContext,
    SkillExecutionError,
    SkillResult,
    call_video_brain,
    input_schema,
    output_schema,
    rule_value,
    skill_definition,
    skill_version,
)
from .correction import run_transcript_correction_skill
from .cover import run_cover_generation_skill
from .highlights import run_highlight_selection_skill
from .planning import run_clip_planning_skill
from .qa import run_quality_assurance_skill
from .render import run_render_skill
from .speaker import run_speaker_diarization_skill
from .subtitle import run_subtitle_composition_skill
from .transcription import run_transcription_skill

# 自动剪辑依赖顺序（编排器必须按此顺序执行）
PIPELINE_ORDER: tuple[str, ...] = (
    "transcription",
    "transcript_correction",
    "speaker_diarization",
    "highlight_selection",
    "clip_planning",
    "subtitle_composition",
    "cover_generation",
    "render",
    "quality_assurance",
)

# 阶段名 → 适配器（编排器按 PIPELINE_ORDER 逐个取用）
ADAPTERS = {
    "transcription": run_transcription_skill,
    "transcript_correction": run_transcript_correction_skill,
    "speaker_diarization": run_speaker_diarization_skill,
    "highlight_selection": run_highlight_selection_skill,
    "clip_planning": run_clip_planning_skill,
    "subtitle_composition": run_subtitle_composition_skill,
    "cover_generation": run_cover_generation_skill,
    "render": run_render_skill,
    "quality_assurance": run_quality_assurance_skill,
}

__all__ = [
    "ADAPTERS",
    "PIPELINE_ORDER",
    "SkillContext",
    "SkillExecutionError",
    "SkillResult",
    "call_video_brain",
    "input_schema",
    "output_schema",
    "rule_value",
    "run_clip_planning_skill",
    "run_cover_generation_skill",
    "run_highlight_selection_skill",
    "run_quality_assurance_skill",
    "run_render_skill",
    "run_speaker_diarization_skill",
    "run_subtitle_composition_skill",
    "run_transcript_correction_skill",
    "run_transcription_skill",
    "skill_definition",
    "skill_version",
]
