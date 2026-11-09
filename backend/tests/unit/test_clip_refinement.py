"""两步切片规划·第二步精修（clip_refinement）契约测试。

核心契约（2026-11-05 用户拍板，docs/two-step-planning-design.md）：
* 防混：提示词A（clip_planning）与提示词B（clip_refinement）各自与
  docs/prompts/ 归档原文逐字节一致；两阶段 system prompt 互斥断言，
  任何交叉直接测试失败；
* 输出 schema 分离：第一步只准输出区间 JSON（_SlicePlanOut）；
  第二步输出四段 markdown 文本，由解析器切块、cue 与分段时间轴对齐；
* 兜底：精修失败按切片粒度回退原始纠错转写 + warning（source=fallback
  如实标注，绝不冒充），不阻断任务；
* 恢复：同指纹同 generation 的精修工件直接复用，不重复调用模型。

不调真实模型：``_model_ready`` / ``_REFINE_LLM_FN`` / pydantic_ai.Agent 打桩。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import clip_planning as cp
from src import clip_refinement as cr
from src import llm_budget
from src import studio_job_worker as worker

_DOCS = Path(__file__).resolve().parents[3] / "docs" / "prompts"


def _prompt_body(filename: str) -> str:
    """归档提示词正文（`---` 分隔线之后，剥首尾空白——即用户原文）。"""
    raw = (_DOCS / filename).read_text(encoding="utf-8")
    return raw.split("\n---\n", 1)[1].strip()


@pytest.fixture(autouse=True)
def _isolated_llm_ledger(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(llm_budget, "_ledger_root", lambda: tmp_path / "llm_usage")


# =====================================================================
# 防混机制（硬要求）：归档原文逐字节一致 + 两阶段互斥
# =====================================================================

def test_prompt_a_matches_archived_file_verbatim():
    assert cp._PLAN_SYSTEM_PROMPT.strip() == _prompt_body("step1-slice-planning.md")


def test_prompt_b_matches_archived_file_verbatim():
    assert cr.PROMPT_REFINE_VERBATIM.strip() == _prompt_body("step2-slice-refinement.md")


def test_prompts_are_distinct_and_versioned():
    """两份提示词互不相同、各自带版本号与 sha256（进 provenance）。"""
    assert cp._PLAN_SYSTEM_PROMPT != cr.PROMPT_REFINE_VERBATIM
    assert cp.PLAN_PROMPT_VERSION != cr.REFINE_PROMPT_VERSION
    assert len(cr.prompt_sha256()) == 64
    import hashlib
    assert cr.prompt_sha256() == hashlib.sha256(
        cr.PROMPT_REFINE_VERBATIM.encode("utf-8")).hexdigest()


def test_planning_agent_system_prompt_is_prompt_a(monkeypatch: pytest.MonkeyPatch):
    """规划阶段发出的 system prompt 必须逐字等于提示词A（stub 捕获断言）。"""
    import pydantic_ai
    captured: dict = {}
    output = cp._SlicePlanOut(video_summary="s", slice_count=1,
                              slices=[cp._SliceOut(id=1, time_range=["00:00:00", "00:01:00"],
                                                   theme="t")],
                              discarded_segments=[])
    result = SimpleNamespace(output=output, usage=lambda: None)

    class Agent:
        def __class_getitem__(cls, _):
            return cls

        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def run(self, *args, **kwargs):
            return result

    monkeypatch.setitem(sys.modules, "src.ai", SimpleNamespace(
        _build_transcript_model=lambda c: None, apply_settings_to_process_env=lambda c: None))
    monkeypatch.setitem(sys.modules, "src.config", SimpleNamespace(
        get_config=lambda: SimpleNamespace(as_runtime_settings=lambda: {})))
    monkeypatch.setattr(pydantic_ai, "Agent", Agent)
    cp._run_plan_agent("fixture", "user-prompt")
    assert captured["system_prompt"] == cp._PLAN_SYSTEM_PROMPT
    assert captured["output_type"] is cp._SlicePlanOut


def test_refinement_agent_system_prompt_is_prompt_b(monkeypatch: pytest.MonkeyPatch):
    """精修阶段发出的 system prompt 必须逐字等于提示词B（stub 捕获断言）。"""
    import pydantic_ai
    captured: dict = {}
    result = SimpleNamespace(output="## 1. 精修 SRT\n\n1\n00:00:00,000 --> 00:00:01,000\n文本",
                             usage=lambda: None)

    class Agent:
        def __class_getitem__(cls, _):
            return cls

        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def run(self, *args, **kwargs):
            return result

    monkeypatch.setattr(cr, "_model_ready", lambda: (True, "fixture", ""))
    monkeypatch.setitem(sys.modules, "src.ai", SimpleNamespace(
        _build_transcript_model=lambda c: None, apply_settings_to_process_env=lambda c: None))
    monkeypatch.setitem(sys.modules, "src.config", SimpleNamespace(
        get_config=lambda: SimpleNamespace(as_runtime_settings=lambda: {})))
    monkeypatch.setattr(pydantic_ai, "Agent", Agent)
    cr._run_refine_agent("user-prompt")
    assert captured["system_prompt"] == cr.PROMPT_REFINE_VERBATIM
    assert captured["output_type"] is str
    assert "clip_refinement" not in str(captured.get("system_prompt", ""))


def test_output_schema_separation():
    """schema 分离：第一步只准区间 JSON；旧单步 _PlanOut 已退役。"""
    assert not hasattr(cp, "_PlanOut"), "旧单步输出 schema 必须退役删除"
    validated = cp._SlicePlanOut.model_validate({
        "video_summary": "s", "slice_count": 1,
        "slices": [{"id": 1, "time_range": ["00:00:00", "00:01:00"], "theme": "t"}],
        "discarded_segments": []})
    assert validated.slices[0].theme == "t"
    # 第一步 schema 没有旧字段（clips/topic/start_segment_id）；旧形态喂进来
    # 只会被忽略，不会产出切片。
    assert "clips" not in cp._SlicePlanOut.model_fields
    old_shaped = cp._SlicePlanOut.model_validate({"clips": [{"topic": "t"}]})
    assert old_shaped.slices == []


# =====================================================================
# 提示词B输出解析：四段切块 + SRT cue 解析 + segment 对齐
# =====================================================================

def _refined_output() -> str:
    return """## 1. 精修 SRT
1
00:00:06,000 --> 00:00:12,000
（医生）确诊高血压就要规范治疗
2
00:00:12,000 --> 00:00:19,500
目标是保护器官不是降数字

## 2. 待核对清单
- [00:00:12] 保护器官 —— 听不清，未猜测补全

## 3. 合规处理清单
- [00:00:06] 「确诊就得马上吃药」→「确诊就要规范治疗」（依据四-3 需遵医嘱）

## 4. 结尾文字卡建议
- 具体用药请面诊遵医嘱
- 治疗方案个体差异大请遵医嘱
"""


def test_parse_refined_output_four_sections():
    parsed = cr.parse_refined_output(_refined_output())
    assert parsed is not None
    assert len(parsed["cues"]) == 2
    assert parsed["cues"][0]["speaker"] == "医生"
    assert parsed["cues"][0]["text"] == "确诊高血压就要规范治疗"
    assert parsed["cues"][0]["end_ms"] == 12000
    assert len(parsed["checklist_pending"]) == 1
    assert len(parsed["checklist_compliance"]) == 1
    assert len(parsed["ending_cards"]) == 2
    assert set(parsed["sections_found"]) == {"srt", "pending", "compliance", "ending"}


def test_parse_refined_output_without_srt_section_is_none():
    assert cr.parse_refined_output("## 2. 待核对清单\n- 无 SRT 主体") is None
    assert cr.parse_refined_output("") is None


def test_align_cues_to_segments_and_long_cue_warning():
    window = [
        {"segment_id": "seg_0001", "start_ms": 6500, "end_ms": 12000, "corrected_text": "原文一"},
        {"segment_id": "seg_0002", "start_ms": 12000, "end_ms": 19500, "corrected_text": "原文二"},
    ]
    parsed = cr.parse_refined_output(_refined_output())
    warnings: list[str] = []
    segments = cr.align_cues_to_segments(parsed["cues"], window, 6500, 19500, warnings)
    assert [(s["segment_id"], s["refined_text"], s["speaker"]) for s in segments] == [
        ("seg_0001", "确诊高血压就要规范治疗", "医生"),
        ("seg_0002", "目标是保护器官不是降数字", ""),
    ]
    assert warnings == []


def test_align_shifts_clip_relative_timecodes():
    window = [{"segment_id": "seg_0001", "start_ms": 6500, "end_ms": 12000,
               "corrected_text": "原文一"}]
    cues = [{"index": 1, "start_ms": 0, "end_ms": 5500, "text": "相对时间码", "speaker": "",
             "char_count": 5}]
    warnings: list[str] = []
    segments = cr.align_cues_to_segments(cues, window, 6500, 12000, warnings)
    assert segments and segments[0]["segment_id"] == "seg_0001"
    assert any("相对时间" in w for w in warnings)


def test_align_records_out_of_window_and_overlong_cues():
    window = [{"segment_id": "seg_0001", "start_ms": 6500, "end_ms": 12000,
               "corrected_text": "原文一"}]
    cues = [
        {"index": 1, "start_ms": 99999000, "end_ms": 99999001, "text": "窗外", "speaker": "",
         "char_count": 2},
        {"index": 2, "start_ms": 6500, "end_ms": 12000,
         "text": "这一条远远超过了提示词B规定的单句十五个字上限", "speaker": "医生", "char_count": 22},
    ]
    warnings: list[str] = []
    segments = cr.align_cues_to_segments(cues, window, 6500, 12000, warnings)
    assert [s["refined_text"] for s in segments] == ["这一条远远超过了提示词B规定的单句十五个字上限"]
    assert any("窗口外" in w for w in warnings)
    assert any("15 字" in w for w in warnings)


# =====================================================================
# 只删不改执法（2026-11-08 用户规则，b2）：判断进提示词、执法进代码
# =====================================================================

def test_delete_only_allows_sentence_deletion_and_filler_freedom():
    """整句删除合法；语气词删除或保留都合法（两侧同表清理）。"""
    warnings: list[str] = []
    aligned = [
        {"segment_id": "seg_0001", "start_ms": 6500, "end_ms": 12000,
         "refined_text": "确诊高血压就要规范治疗", "speaker": "医生"},      # 语气词已删
        {"segment_id": "seg_0002", "start_ms": 12000, "end_ms": 19500,
         "refined_text": "那个目标是保护器官，不是降数字", "speaker": ""},   # 语气词保留
    ]
    segments, reverted = cr.enforce_delete_only(aligned, _segments(), warnings)
    assert reverted == [] and warnings == []
    assert [s["refined_text"] for s in segments] == [
        "确诊高血压就要规范治疗", "那个目标是保护器官，不是降数字"]


def test_delete_only_allows_whole_sentence_deletion():
    """前一整句被删 → 剩余句仍是顺序保持子序列，合法。"""
    warnings: list[str] = []
    aligned = [{"segment_id": "seg_0002", "start_ms": 12000, "end_ms": 19500,
                "refined_text": "目标是保护器官不是降数字。", "speaker": ""}]
    _, reverted = cr.enforce_delete_only(aligned, _segments(), warnings)
    assert reverted == [] and warnings == []


def test_delete_only_reverts_rewritten_segment_and_keeps_others():
    """改词润色的段整段回退原纠错转写并留痕；合规段不受牵连。"""
    warnings: list[str] = []
    aligned = [
        {"segment_id": "seg_0001", "start_ms": 6500, "end_ms": 12000,
         "refined_text": "确诊高血压就要规范治疗", "speaker": "医生"},
        {"segment_id": "seg_0002", "start_ms": 12000, "end_ms": 19500,
         "refined_text": "目标是保护器官而并非单纯降低数字", "speaker": "家属"},  # 润色改词
    ]
    segments, reverted = cr.enforce_delete_only(aligned, _segments(), warnings)
    assert reverted == ["seg_0002"]
    assert segments[0]["refined_text"] == "确诊高血压就要规范治疗"
    assert segments[1]["refined_text"] == "那个目标是保护器官，不是降数字。"   # 回退原文
    assert segments[1]["speaker"] == ""                                    # 回退段清空说话人
    assert any("只删不改校验" in w and "seg_0002" in w for w in warnings)


def test_delete_only_rejects_new_content_and_reorder():
    """语序颠倒 = 顺序保持破坏 → 违规回退。"""
    warnings: list[str] = []
    aligned = [{"segment_id": "seg_0001", "start_ms": 6500, "end_ms": 12000,
                "refined_text": "目标是保护器官不是降数字。确诊高血压就要规范治疗。",
                "speaker": ""}]
    _, reverted = cr.enforce_delete_only(aligned, _segments(), warnings)
    assert reverted == ["seg_0001"]
    assert any("只删不改校验" in w for w in warnings)


def _rewritten_output() -> str:
    """seg_0001 润色违规、seg_0002 合规的精修输出（部分违规夹具）。"""
    return """## 1. 精修 SRT
1
00:00:06,000 --> 00:00:12,000
（医生）确诊了高血压就必须马上开始规范化治疗
2
00:00:12,000 --> 00:00:19,500
目标是保护器官不是降数字

## 2. 待核对清单
- 无
## 3. 合规处理清单
- 无
## 4. 结尾文字卡建议
- 具体用药请面诊遵医嘱
"""


def test_refine_clips_partial_violation_reverts_single_segment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cr, "_model_ready", lambda: (True, "fixture-model", ""))
    monkeypatch.setattr(cr, "_REFINE_LLM_FN", lambda prompt: _rewritten_output())
    record = cr.refine_clips(_plan_record(), _segments(), job_id="job-do1")
    entry = record["clips"]["clip-abc1234567"]
    assert entry["source"] == "llm"
    assert entry["reverted_segments"] == ["seg_0001"]
    assert entry["segments"][0]["refined_text"] == "啊，确诊高血压就要规范治疗。"
    assert entry["segments"][1]["refined_text"] == "目标是保护器官不是降数字"
    assert any("只删不改校验" in w for w in entry["warnings"])


def test_refine_clips_full_violation_falls_back(monkeypatch: pytest.MonkeyPatch):
    """全部分段违规 → 整片按兜底契约回退（reason=delete_only_violation）。"""
    monkeypatch.setattr(cr, "_model_ready", lambda: (True, "fixture-model", ""))
    fully_rewritten = _rewritten_output().replace(
        "目标是保护器官不是降数字", "目标是保器官而非降数字")
    monkeypatch.setattr(cr, "_REFINE_LLM_FN", lambda prompt: fully_rewritten)
    record = cr.refine_clips(_plan_record(), _segments(), job_id="job-do2")
    entry = record["clips"]["clip-abc1234567"]
    assert entry["source"] == "fallback"
    assert entry["fallback_reason"] == "delete_only_violation"
    assert any("整片回退" in w for w in entry["warnings"])
    assert record["fallback_count"] == 1


# =====================================================================
# refine_clips：llm / 失败回退 / 全量不可用
# =====================================================================

def _plan_record() -> dict:
    return {"plan_source": "llm", "clips": [{
        "clip_id": "clip-abc1234567", "topic": "误区一：规范治疗",
        "hook_first_line": "高血压确诊就要规范治疗",
        "start_ms": 6500, "end_ms": 19500, "duration_ms": 13000,
        "start_segment_id": "seg_0001", "end_segment_id": "seg_0002",
    }]}


def _segments() -> list[dict]:
    # 夹具即「只删不改」活的契约示例：原文带语气词，精修侧（_refined_output）
    # 只发生语气词删除——可全部通过 enforce_delete_only 校验。
    return [
        {"segment_id": "seg_0001", "start_ms": 6500, "end_ms": 12000,
         "corrected_text": "啊，确诊高血压就要规范治疗。"},
        {"segment_id": "seg_0002", "start_ms": 12000, "end_ms": 19500,
         "corrected_text": "那个目标是保护器官，不是降数字。"},
    ]


def test_refine_clips_llm_path(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cr, "_model_ready", lambda: (True, "fixture-model", ""))
    calls: list[str] = []

    def stub(user_prompt: str) -> str:
        calls.append(user_prompt)
        return _refined_output()

    monkeypatch.setattr(cr, "_REFINE_LLM_FN", stub)
    record = cr.refine_clips(_plan_record(), _segments(), job_id="job-r")
    assert record["fallback_count"] == 0 and record["llm_calls"] == 1
    entry = record["clips"]["clip-abc1234567"]
    assert entry["source"] == "llm"
    assert entry["model"] == "fixture-model"
    assert entry["segments"][0]["refined_text"] == "确诊高血压就要规范治疗"
    assert entry["theme"] == "误区一：规范治疗"
    assert entry["hook_first_line"] == "高血压确诊就要规范治疗"
    # 用户提示带规划信息 + 原始 SRT（提示词B的输入契约）
    assert "主题：误区一：规范治疗" in calls[0]
    assert "钩子首句：高血压确诊就要规范治疗" in calls[0]
    assert "00:00:06,500 --> 00:00:12,000" in calls[0]
    assert "00:00:12,000 --> 00:00:19,500" in calls[0]


def test_refine_clips_fallback_on_llm_error(monkeypatch: pytest.MonkeyPatch):
    """精修失败按切片粒度回退原始纠错转写 + warning，绝不冒充。"""
    monkeypatch.setattr(cr, "_model_ready", lambda: (True, "fixture-model", ""))

    def boom(user_prompt: str) -> str:
        raise RuntimeError("网关超时")

    monkeypatch.setattr(cr, "_REFINE_LLM_FN", boom)
    record = cr.refine_clips(_plan_record(), _segments(), job_id="job-r")
    entry = record["clips"]["clip-abc1234567"]
    assert entry["source"] == "fallback"
    assert entry["fallback_reason"] == "refine_failed"
    assert [s["refined_text"] for s in entry["segments"]] == [
        "啊，确诊高血压就要规范治疗。", "那个目标是保护器官，不是降数字。"]
    assert any("回退原始纠错转写" in w for w in entry["warnings"])
    assert any("1/1 条切片精修回退" in w for w in record["warnings"])


def test_refine_clips_all_fallback_when_llm_unavailable(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cr, "_model_ready", lambda: (False, "", "LLM 未配置"))
    record = cr.refine_clips(_plan_record(), _segments(), job_id="job-r")
    entry = record["clips"]["clip-abc1234567"]
    assert entry["source"] == "fallback"
    assert record["llm_requested"] is False and record["llm_error"] == "LLM 未配置"
    assert [s["refined_text"] for s in entry["segments"]] == [
        "啊，确诊高血压就要规范治疗。", "那个目标是保护器官，不是降数字。"]


def test_refinement_save_load_roundtrip_and_fingerprint(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("STUDIO_TASKS_ROOT", str(tmp_path / "tasks"))
    monkeypatch.setattr(cr, "_model_ready", lambda: (False, "", "LLM 未配置"))
    record = cr.refine_clips(_plan_record(), _segments(), job_id="job-s")
    saved = cr.save_refinement_record("job-s", record)
    assert (Path(saved["path"]).is_file())
    loaded = cr.load_refinement_record("job-s", expected_fingerprint=record["plan_fingerprint"])
    assert loaded is not None and loaded["clips"]["clip-abc1234567"]["source"] == "fallback"
    assert cr.load_refinement_record("job-s", expected_fingerprint="mismatch") is None
    assert cr.load_refinement_record("job-missing") is None


# =====================================================================
# worker 接线：refining 阶段状态 + 工件上传 + plan_ready 后移 + 恢复复用
# =====================================================================

def _wire_worker(monkeypatch: pytest.MonkeyPatch, tmp_path, llm_calls: dict):
    monkeypatch.setenv("STUDIO_TASKS_ROOT", str(tmp_path / "tasks"))
    monkeypatch.setattr(cr, "_model_ready", lambda: (True, "fixture-model", ""))

    def stub(user_prompt: str) -> str:
        llm_calls["n"] += 1
        return _refined_output()

    monkeypatch.setattr(cr, "_REFINE_LLM_FN", stub)
    plan_file = tmp_path / "clip_plan.json"
    plan_file.write_text("{}", encoding="utf-8")
    posts: list[dict] = []
    monkeypatch.setattr(worker, "_http",
                        lambda method, url, payload=None, timeout=60: posts.append(payload) or {})
    monkeypatch.setattr("src.r2.upload_file",
                        lambda path, key: {"object_key": key, "size": Path(path).stat().st_size})
    states: list[tuple[str, str]] = []
    monkeypatch.setattr(worker, "_state",
                        lambda job_id, status, stage, progress, error=None:
                        states.append((status, stage)))
    return posts, states


def test_worker_refinement_stage_runs_and_uploads(tmp_path, monkeypatch: pytest.MonkeyPatch):
    llm_calls = {"n": 0}
    posts, states = _wire_worker(monkeypatch, tmp_path, llm_calls)
    record = worker._run_clip_refinement_stage({"job_id": "autoedit_ref01"}, "autoedit_ref01",
                                               {"asr_segments": _segments()}, _plan_record())
    statuses = [status for status, _ in states]
    assert statuses[0] == "refining" and statuses[-1] == "refining"
    artifact = Path(record["refinement_artifact_path"])
    assert artifact.name == "clip_refinement.json" and artifact.is_file()
    kinds = [post.get("kind") for post in posts if isinstance(post, dict)]
    assert "clip_refinement" in kinds
    entry = record["clips"]["clip-abc1234567"]
    assert entry["source"] == "llm" and record["plan_generation"] == 0
    assert llm_calls["n"] == 1


def test_worker_refinement_stage_resume_reuses_without_model_call(
        tmp_path, monkeypatch: pytest.MonkeyPatch):
    llm_calls = {"n": 0}
    _wire_worker(monkeypatch, tmp_path, llm_calls)
    first = worker._run_clip_refinement_stage({"job_id": "autoedit_ref02"}, "autoedit_ref02",
                                              {"asr_segments": _segments()}, _plan_record())
    assert llm_calls["n"] == 1
    stages: list[str] = []
    monkeypatch.setattr(worker, "_state",
                        lambda job_id, status, stage, progress, error=None: stages.append(stage))
    second = worker._run_clip_refinement_stage({"job_id": "autoedit_ref02"}, "autoedit_ref02",
                                               {"asr_segments": _segments()}, _plan_record())
    assert llm_calls["n"] == 1                     # 恢复复用，不重复调用模型
    assert second["refinement_artifact_path"] == first["refinement_artifact_path"]
    assert any("恢复复用" in s for s in stages)


# =====================================================================
# 文案导出（精修版 markdown）
# =====================================================================

def test_copy_markdown_llm_and_fallback(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("STUDIO_TASKS_ROOT", str(tmp_path / "tasks"))
    monkeypatch.setattr(cr, "_model_ready", lambda: (True, "fixture-model", ""))
    monkeypatch.setattr(cr, "_REFINE_LLM_FN", lambda prompt: _refined_output())
    record = cr.refine_clips(_plan_record(), _segments(), job_id="job-md")
    markdown = cr.build_copy_markdown("job-md", "clip-abc1234567", _plan_record(), record)
    assert "# clip-abc1234567 文案 · 误区一：规范治疗" in markdown
    assert "模型精修版" in markdown
    assert "[00:06–00:12] （医生）确诊高血压就要规范治疗" in markdown
    assert "待核对清单" in markdown and "结尾文字卡建议" in markdown
    # fallback：如实标注，绝不冒充精修文本
    fallback_record = {"clips": {"clip-abc1234567": {
        "segments": [{"segment_id": "seg_0001", "refined_text": "原文一", "speaker": ""}],
        "source": "fallback", "theme": "误区一", "hook_first_line": "", "warnings": []}}}
    fb = cr.build_copy_markdown("job-md", "clip-abc1234567", _plan_record(), fallback_record)
    assert "精修版未生成（fallback，原始纠错转写）" in fb and "原文一" in fb


def test_refined_copy_route_serves_markdown(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """导出端点：读精修产物 → markdown；缺失 → 404。"""
    from src.api.routes import media as media_routes
    monkeypatch.setenv("STUDIO_TASKS_ROOT", str(tmp_path / "tasks"))
    monkeypatch.setattr(cr, "_model_ready", lambda: (True, "fixture-model", ""))
    monkeypatch.setattr(cr, "_REFINE_LLM_FN", lambda prompt: _refined_output())
    record = cr.refine_clips(_plan_record(), _segments(), job_id="job-api")
    cr.save_refinement_record("job-api", record)
    cp.save_plan_record("job-api", _plan_record())

    async def main():
        response = await media_routes.studio_clip_refined_copy("job-api", "clip-abc1234567")
        return response

    response = asyncio.run(main())
    assert response.status_code == 200
    assert "text/markdown" in response.media_type
    assert "确诊高血压就要规范治疗" in response.body.decode("utf-8")

    with pytest.raises(Exception) as excinfo:
        asyncio.run(media_routes.studio_clip_refined_copy("job-none", "clip-x"))
    assert "404" in str(getattr(excinfo.value, "status_code", "")) or \
        getattr(excinfo.value, "status_code", None) == 404


# =====================================================================
# 右侧精修面板（结构化 JSON，实施清单第 6 项消费侧）
# =====================================================================

def test_transcript_payload_relative_timecodes(monkeypatch: pytest.MonkeyPatch):
    """面板行 = cue 四列语义，时间码换算成切片相对毫秒（模板口径 00:00–00:02）。"""
    monkeypatch.setattr(cr, "_model_ready", lambda: (True, "fixture-model", ""))
    monkeypatch.setattr(cr, "_REFINE_LLM_FN", lambda prompt: _refined_output())
    record = cr.refine_clips(_plan_record(), _segments(), job_id="job-panel")
    payload = cr.build_transcript_payload("job-panel", "clip-abc1234567",
                                          _plan_record(), record)
    assert payload["source"] == "llm"
    assert payload["theme"] == "误区一：规范治疗"
    assert payload["checklist_pending"] and payload["ending_cards"]
    rows = payload["rows"]
    assert len(rows) == 2
    assert rows[0]["speaker"] == "医生"
    assert rows[0]["text"] == "确诊高血压就要规范治疗"
    # cue 绝对 6000–12000 / 12000–19500，切片起点 6500：
    # 相对 0–5500（负值钳 0）/ 5500–13000。
    assert rows[0]["start_ms"] == 0 and rows[0]["end_ms"] == 5500
    assert rows[1]["start_ms"] == 5500 and rows[1]["end_ms"] == 13000


def test_transcript_payload_fallback_null_timecodes():
    """fallback 行没有时间码字段 → start/end 为 None（如实，不编造）。"""
    fallback_record = {"prompt_version": "refine-test", "clips": {"clip-abc1234567": {
        "segments": [{"segment_id": "seg_0001", "refined_text": "原文一", "speaker": ""}],
        "source": "fallback", "fallback_reason": "llm_unavailable",
        "warnings": ["w1"], "prompt_version": "refine-test"}}}
    payload = cr.build_transcript_payload("job-p", "clip-abc1234567",
                                          _plan_record(), fallback_record)
    assert payload["source"] == "fallback"
    assert payload["fallback_reason"] == "llm_unavailable"
    assert payload["rows"][0]["start_ms"] is None
    assert payload["rows"][0]["end_ms"] is None
    assert payload["rows"][0]["text"] == "原文一"
    assert payload["warnings"] == ["w1"]


def test_transcript_payload_missing_plan_no_fake_timecodes(monkeypatch: pytest.MonkeyPatch):
    """计划缺失 → 不按 0 偏移冒充相对时间：行时间码 None + warning。"""
    monkeypatch.setattr(cr, "_model_ready", lambda: (True, "fixture-model", ""))
    monkeypatch.setattr(cr, "_REFINE_LLM_FN", lambda prompt: _refined_output())
    record = cr.refine_clips(_plan_record(), _segments(), job_id="job-noplan")
    payload = cr.build_transcript_payload("job-noplan", "clip-abc1234567", None, record)
    assert payload["source"] == "llm"
    assert all(row["start_ms"] is None and row["end_ms"] is None for row in payload["rows"])
    assert any("缺少切片计划" in w for w in payload["warnings"])


def test_refined_transcript_route_serves_json(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """面板端点：读精修产物 → 结构化 JSON；缺失任务 / 未知切片 → 404。"""
    from src.api.routes import media as media_routes
    monkeypatch.setenv("STUDIO_TASKS_ROOT", str(tmp_path / "tasks"))
    monkeypatch.setattr(cr, "_model_ready", lambda: (True, "fixture-model", ""))
    monkeypatch.setattr(cr, "_REFINE_LLM_FN", lambda prompt: _refined_output())
    record = cr.refine_clips(_plan_record(), _segments(), job_id="job-panel-api")
    cr.save_refinement_record("job-panel-api", record)
    cp.save_plan_record("job-panel-api", _plan_record())

    payload = asyncio.run(
        media_routes.studio_clip_refined_transcript("job-panel-api", "clip-abc1234567"))
    assert payload["source"] == "llm"
    assert payload["rows"][0]["speaker"] == "医生"
    assert payload["rows"][0]["start_ms"] == 0

    for task_id, clip_id in [("job-none", "clip-x"),
                             ("job-panel-api", "clip-doesnotexist")]:
        with pytest.raises(Exception) as excinfo:
            asyncio.run(media_routes.studio_clip_refined_transcript(task_id, clip_id))
        assert getattr(excinfo.value, "status_code", None) == 404 or \
            "404" in str(getattr(excinfo.value, "status_code", ""))
