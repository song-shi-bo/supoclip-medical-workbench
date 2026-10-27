"""DSH 模型全文纠错（transcript_correction_llm，patch 协议）契约测试。

不调真实模型：``_model_ready`` / ``_run_agent`` 打桩。
核心契约：
* 时间戳（start/end/start_ms/end_ms）只来自原始分段，模型无权改动；
* 输入不携带时间戳；模型只返回「发生修改的段」patch，未修改段保持原文；
* raw_text / corrected_text / 修改原因 / uncertain 落段与 audit；
* 分段数量永不改变；单批失败只回退该批并留痕；
* fallback 的 engine 如实为 local-conservative-rules，绝不冒充 dsh-llm；
* 预留估算与真实 usage / 台账记录（llm_budget）。
"""

from __future__ import annotations

import json

import pytest

from src import llm_budget
from src import studio_job_worker as worker
from src import transcript_correction_llm as tcm


@pytest.fixture(autouse=True)
def _isolated_llm_ledger(tmp_path: object, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(llm_budget, "_ledger_root", lambda: tmp_path / "llm_usage")  # type: ignore[attr-defined]


def _segs(n: int, text: str = "患 者有 高血ya病史."):
    return [{"start": float(i), "end": float(i) + 1.5, "text": f"{text}{i}"} for i in range(n)]


def _batch(changes: list[tcm._PatchItem]) -> tcm._PatchBatch:
    return tcm._PatchBatch(changes=changes)


def _patch(index: int, corrected_text: str, reason: str = "同音字修正",
           uncertain: bool = False) -> tcm._PatchItem:
    return tcm._PatchItem(index=index, corrected_text=corrected_text,
                          reason=reason, uncertain=uncertain)


def _prompt_segment_lines(prompt: str) -> list[tuple[int, str]]:
    """解析「【待校对分段】」区里的 `序号|文本` 行（测试桩用它按输入造 patch）。"""
    section = prompt.split("【待校对分段", 1)[1]
    pairs: list[tuple[int, str]] = []
    for line in section.strip().splitlines():
        head, sep, text = line.partition("|")
        if sep and head.strip().isdigit():
            pairs.append((int(head.strip()), text))
    return pairs


def _patch_everything(model: str, prompt: str, max_tokens: int) -> tcm._PatchBatch:
    return _batch([_patch(index, f"修正{text}") for index, text in _prompt_segment_lines(prompt)])


def test_model_unavailable_falls_back_and_enriches(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(tcm, "_model_ready", lambda: (False, "", "LLM 未配置"))
    raw = _segs(3)
    result = tcm.correct_segments_with_model(raw)

    summary = result["correction_summary"]
    assert summary["engine"] == "local-conservative-rules"
    assert summary["fallback_reason"] == "LLM 未配置"
    assert len(result["asr_segments"]) == 3
    for index, seg in enumerate(result["asr_segments"]):
        assert seg["segment_id"] == f"seg_{index:04d}"
        assert seg["start_ms"] == index * 1000
        assert seg["end_ms"] == index * 1000 + 1500
        assert seg["raw_text"] == raw[index]["text"]
        assert seg["text"].strip()  # 非空，video-brain 契约


def test_llm_patches_applied_and_timestamps_frozen(monkeypatch: pytest.MonkeyPatch):
    raw = [
        {"start": 1.0, "end": 2.5, "text": "患者有高血ya病史"},
        {"start": 2.5, "end": 4.0, "text": "平时血压偏高"},
    ]
    monkeypatch.setattr(tcm, "_model_ready", lambda: (True, "openai:test-model", ""))
    # 只 patch 第 0 段；第 1 段无修改——patch 协议下模型不返回它
    monkeypatch.setattr(
        tcm, "_run_agent",
        lambda model, prompt, max_tokens: _batch(
            [_patch(0, "患者有高血压病史", reason="血ya→高血压：同音字")]),
    )
    result = tcm.correct_segments_with_model(raw)

    summary = result["correction_summary"]
    assert summary["engine"] == "dsh-llm:openai:test-model"
    assert summary["llm_changed"] == 1
    assert summary["mode"] == "single"
    first, second = result["asr_segments"]
    assert first["text"] == "患者有高血压病史"
    assert first["raw_text"] == "患者有高血ya病史"
    assert first["correction_reason"] == "血ya→高血压：同音字"
    # 未被 patch 的段保持原文（no_change）
    assert second["text"] == "平时血压偏高"
    assert second["correction_status"] == "no_change"
    # 时间戳冻结：与原始分段逐值一致，毫秒字段同步生成
    assert (first["start"], first["end"], first["start_ms"], first["end_ms"]) == (1.0, 2.5, 1000, 2500)
    assert (second["start"], second["end"], second["start_ms"], second["end_ms"]) == (2.5, 4.0, 2500, 4000)
    audit = result["correction_audit"]
    assert audit[0]["corrected_text"] == "患者有高血压病史"
    assert audit[0]["reason"] == "血ya→高血压：同音字"
    assert audit[0]["engine"].startswith("dsh-llm:")
    assert audit[1]["status"] == "no_change"


def test_input_carries_no_timestamps(monkeypatch: pytest.MonkeyPatch):
    prompts: list[str] = []
    monkeypatch.setattr(tcm, "_model_ready", lambda: (True, "openai:test-model", ""))

    def spy(model, prompt, max_tokens):
        prompts.append(prompt)
        return _batch([])

    monkeypatch.setattr(tcm, "_run_agent", spy)
    tcm.correct_segments_with_model([{"start": 12.345, "end": 15.6, "text": "原文"}])
    assert "12.345" not in prompts[0] and "15.6" not in prompts[0]
    assert prompts[0].startswith("0|") or "\n0|原文" in prompts[0]


def test_uncertain_patch_keeps_raw_text_and_flags_pending(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(tcm, "_model_ready", lambda: (True, "openai:test-model", ""))
    monkeypatch.setattr(
        tcm, "_run_agent",
        lambda model, prompt, max_tokens: _batch([_patch(0, "", reason="口音难辨", uncertain=True)]),
    )
    result = tcm.correct_segments_with_model([{"start": 0.0, "end": 1.0, "text": "原文如此"}])
    seg = result["asr_segments"][0]
    assert seg["text"] == "原文如此"          # uncertain：保留原文
    assert seg["uncertain"] is True
    assert seg["correction_status"] == "pending_review"
    assert result["correction_summary"]["issues_pending_review"] == 1


def test_blank_patch_text_keeps_raw(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(tcm, "_model_ready", lambda: (True, "openai:test-model", ""))
    monkeypatch.setattr(
        tcm, "_run_agent",
        lambda model, prompt, max_tokens: _batch([_patch(0, "   ", reason="")]),
    )
    result = tcm.correct_segments_with_model([{"start": 0.0, "end": 1.0, "text": "原始句"}])
    seg = result["asr_segments"][0]
    assert seg["text"] == "原始句"
    assert seg["uncertain"] is True


def test_small_transcript_single_compact_call(monkeypatch: pytest.MonkeyPatch):
    prompts: list[str] = []

    def fake_agent(model, prompt, max_tokens):
        prompts.append(prompt)
        return _batch([_patch(i, f"甲{i}") for i, _ in _prompt_segment_lines(prompt)])

    monkeypatch.setattr(tcm, "_model_ready", lambda: (True, "openai:test-model", ""))
    monkeypatch.setattr(tcm, "_run_agent", fake_agent)
    result = tcm.correct_segments_with_model(_segs(3))
    assert len(prompts) == 1
    assert "全篇摘要" not in prompts[0]       # 整篇模式：无摘要/重叠上下文
    assert "前文重叠" not in prompts[0]
    assert len(result["asr_segments"]) == 3
    assert [seg["text"] for seg in result["asr_segments"]] == ["甲0", "甲1", "甲2"]


def test_long_transcript_batched_with_summary_and_overlap(monkeypatch: pytest.MonkeyPatch):
    prompts: list[str] = []
    monkeypatch.setattr(tcm, "SINGLE_CALL_LIMIT", 100)  # 强制走分批模式（patch 协议下默认阈值 800）

    def fake_agent(model, prompt, max_tokens):
        prompts.append(prompt)
        return _patch_everything(model, prompt, max_tokens)

    monkeypatch.setattr(tcm, "_model_ready", lambda: (True, "openai:test-model", ""))
    monkeypatch.setattr(tcm, "_run_agent", fake_agent)
    raw = _segs(250)
    result = tcm.correct_segments_with_model(raw)

    # 250 段 → 批大小 100 → 3 批（100/100/50）
    assert len(prompts) == 3
    assert "全篇摘要" in prompts[1] and "前文重叠" in prompts[1]
    # 第二批携带的全局序号从 100 起（重叠段只作上下文，不重复校对）
    second_batch_indices = [index for index, _ in _prompt_segment_lines(prompts[1])]
    assert second_batch_indices[0] == 100 and len(second_batch_indices) == 100
    summary = result["correction_summary"]
    assert summary["llm_batches"] == 3
    assert summary["fallback_batches"] == 0
    assert len(result["asr_segments"]) == 250  # 数量守恒


def test_failed_batch_falls_back_only_that_batch(monkeypatch: pytest.MonkeyPatch):
    calls = {"n": 0}
    monkeypatch.setattr(tcm, "SINGLE_CALL_LIMIT", 100)  # 强制分批（默认整篇阈值 800）

    def fake_agent(model, prompt, max_tokens):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("模型超时")
        return _patch_everything(model, prompt, max_tokens)

    monkeypatch.setattr(tcm, "_model_ready", lambda: (True, "openai:test-model", ""))
    monkeypatch.setattr(tcm, "_run_agent", fake_agent)
    result = tcm.correct_segments_with_model(_segs(250))

    summary = result["correction_summary"]
    assert summary["llm_batches"] == 2
    assert summary["fallback_batches"] == 1
    assert "+local-fallback" in summary["engine"]
    assert summary["fallback_reasons"] and "模型超时" in summary["fallback_reasons"][0]
    assert len(result["asr_segments"]) == 250
    # 带引擎字段的 audit 条目应来自模型批（保守规整的既有 audit 形状不带 engine）
    engines = {entry["engine"] for entry in result["correction_audit"] if "engine" in entry}
    assert engines and all(e.startswith("dsh-llm:") for e in engines)


def test_out_of_range_patch_ignored(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(tcm, "_model_ready", lambda: (True, "openai:test-model", ""))
    monkeypatch.setattr(
        tcm, "_run_agent",
        lambda model, prompt, max_tokens: _batch(
            [_patch(0, "修正"), _patch(99, "越界"), _patch(0, "重复序号")]),
    )
    result = tcm.correct_segments_with_model(_segs(3))
    summary = result["correction_summary"]
    assert summary["invalid_patches"] == 2
    assert summary["llm_changed"] == 1
    assert result["asr_segments"][1]["correction_status"] == "no_change"


def test_token_limit_single_call_downgrades_to_batches(monkeypatch: pytest.MonkeyPatch):
    def fake_agent(model, prompt, max_tokens):
        if "全篇摘要" not in prompt:
            # 整篇调用：无论 4096 还是 6144 档都报输出超上限
            raise RuntimeError("UnexpectedModelBehavior: Model token limit (provider default) exceeded")
        return _patch_everything(model, prompt, max_tokens)

    monkeypatch.setattr(tcm, "_model_ready", lambda: (True, "openai:test-model", ""))
    monkeypatch.setattr(tcm, "_run_agent", fake_agent)
    result = tcm.correct_segments_with_model(_segs(250))

    summary = result["correction_summary"]
    assert summary["llm_batches"] == 3
    assert summary["fallback_batches"] == 0
    assert summary["engine"] == "dsh-llm:openai:test-model"  # 分批重跑成功，仍是模型结果
    assert "整篇输出超上限" in (summary.get("single_call_downgrade") or "")
    assert len(result["asr_segments"]) == 250


def test_single_call_all_batches_fail_keeps_engine_honest(monkeypatch: pytest.MonkeyPatch):
    def fake_agent(model, prompt, max_tokens):
        raise RuntimeError("连接被重置")

    monkeypatch.setattr(tcm, "_model_ready", lambda: (True, "openai:test-model", ""))
    monkeypatch.setattr(tcm, "_run_agent", fake_agent)
    result = tcm.correct_segments_with_model(_segs(4))
    summary = result["correction_summary"]
    assert summary["engine"] == "local-conservative-rules"  # 不允许把 fallback 冒充 dsh-llm
    assert len(result["asr_segments"]) == 4


def test_progress_callback_reports_batches(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(tcm, "SINGLE_CALL_LIMIT", 100)  # 强制分批（默认整篇阈值 800）
    monkeypatch.setattr(tcm, "_model_ready", lambda: (True, "openai:test-model", ""))
    monkeypatch.setattr(tcm, "_run_agent", _patch_everything)
    seen: list[str] = []
    tcm.correct_segments_with_model(
        _segs(250), progress=lambda message: seen.append(message))
    assert seen and seen[0] == "模型全文纠错（批次 1/3）" and seen[-1] == "模型全文纠错（批次 3/3）"


def test_usage_recorded_to_summary_and_ledger(monkeypatch: pytest.MonkeyPatch, tmp_path: object):
    class _Usage:
        request_tokens = 1234
        response_tokens = 567
        details = {"reasoning_tokens": 89}

    class _Result:
        output = None

        def usage(self):
            return _Usage()

    def fake_agent(model, prompt, max_tokens):
        # 模拟 _run_agent 内部的 usage 捕获：直接改模块级 _last_usage 不可行，
        # 这里借助真实 extract_usage 走一遍 RunResult 形状。
        tcm._last_usage.update(llm_budget.extract_usage(_Result()))
        return _batch([_patch(0, "修正")])

    monkeypatch.setattr(tcm, "_model_ready", lambda: (True, "openai:test-model", ""))
    monkeypatch.setattr(tcm, "_run_agent", fake_agent)
    monkeypatch.setattr(llm_budget, "_ledger_root", lambda: tmp_path / "llm_usage")  # type: ignore[attr-defined]
    result = tcm.correct_segments_with_model(_segs(2), job_id="job-ledger")
    summary = result["correction_summary"]
    assert summary["llm_prompt_tokens"] == 1234
    assert summary["llm_completion_tokens"] == 567
    assert summary["llm_reasoning_tokens"] == 89
    assert summary["llm_reserved_tokens_estimate"] > 0
    ledger = json.loads((tmp_path / "llm_usage").glob("*.json").__next__().read_text())  # type: ignore[attr-defined]
    assert ledger["requests"] == 1 and ledger["entries"][0]["job_id"] == "job-ledger"
    assert ledger["entries"][0]["kind"] == "transcript_correction"


def test_stage_correcting_routes_through_model_path(monkeypatch: pytest.MonkeyPatch):
    received: dict = {}

    def fake_correct(raw_segments, progress=None, job_id=None):
        received["segments"] = raw_segments
        received["progress"] = progress
        received["job_id"] = job_id
        return {"asr_segments": [], "correction_audit": [], "correction_summary": {}}

    monkeypatch.setattr("src.transcript_correction_llm.correct_segments_with_model", fake_correct)
    monkeypatch.setattr(worker, "_state", lambda *a, **k: None)
    worker.stage_correcting(
        [{"start": 1.2567, "end": 2.5, "text": "  文本 "}], "job-x")
    seg = received["segments"][0]
    assert seg["start"] == 1.257 and seg["text"] == "文本"
    assert callable(received["progress"])
    assert received["job_id"] == "job-x"
