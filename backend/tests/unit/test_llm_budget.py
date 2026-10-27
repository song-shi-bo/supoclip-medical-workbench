"""llm_budget（模型 token 台账与估算）单元测试：估算 / 台账 / usage 提取。

预算闸门已于 2026-10-15 删除（用户拍板）：额度不足由各模型阶段自动回退兜底，
本模块只保留估算与台账的观测契约测试。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from src import llm_budget


@pytest.fixture()
def ledger(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(llm_budget, "_ledger_root", lambda: tmp_path / "llm_usage")
    return tmp_path / "llm_usage"


def test_estimate_tokens_conservative_for_cjk_and_ascii():
    cjk = llm_budget.estimate_tokens("高血压按时服药")          # 6 个汉字
    assert cjk >= 6                                             # ≥1 token/字上界保守
    ascii_est = llm_budget.estimate_tokens("abcdefgh")          # ASCII 更便宜
    assert ascii_est < cjk
    assert llm_budget.estimate_tokens("") == 0


def test_estimate_reserve_includes_max_tokens_and_overhead():
    est = llm_budget.estimate_reserve("只含少量文本", 4096)
    assert est >= 4096 + llm_budget.PER_CALL_OVERHEAD_TOKENS


def test_ledger_records_reserved_and_usage(ledger):
    llm_budget.record_call("transcript_correction", job_id="j1", est_reserved=8000,
                           usage={"prompt_tokens": 4000, "completion_tokens": 1000,
                                  "reasoning_tokens": 200}, model="openai:test-model")
    llm_budget.record_call("content_analysis", job_id="j1", est_reserved=9000)
    used_tokens, used_requests = llm_budget.today_totals()
    assert used_tokens == 17000 and used_requests == 2
    path = ledger / f"{datetime.now(UTC).strftime('%Y-%m-%d')}.json"
    data = json.loads(path.read_text())
    assert data["entries"][0]["kind"] == "transcript_correction"
    assert data["entries"][0]["usage"]["reasoning_tokens"] == 200


def test_ledger_is_per_utc_date(ledger):
    old = datetime(2026, 10, 14, 23, 59, tzinfo=UTC)
    llm_budget.record_call("clip_planning", est_reserved=5000, now=old)
    assert llm_budget.today_totals() == (0, 0)                  # 新的一天从未用
    assert llm_budget.today_totals(old) == (5000, 1)


def test_record_call_failure_never_raises(tmp_path, monkeypatch: pytest.MonkeyPatch):
    def boom():
        raise OSError("disk full")
    monkeypatch.setattr(llm_budget, "_ledger_root", boom)
    llm_budget.record_call("transcript_correction", est_reserved=1)  # 不应抛出


def test_extract_usage_defensive_shapes():
    class _Usage:
        request_tokens = 100
        response_tokens = 50
        details = {"reasoning_tokens": 20}

    class _Result:
        def usage(self):
            return _Usage()

    assert llm_budget.extract_usage(_Result()) == {
        "prompt_tokens": 100, "completion_tokens": 50, "reasoning_tokens": 20}
    assert llm_budget.extract_usage(object()) == {
        "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}
    assert llm_budget.extract_usage(None) == {
        "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}


def test_token_limit_error_detection():
    assert llm_budget.is_token_limit_error(RuntimeError(
        "UnexpectedModelBehavior: Model token limit (provider default) exceeded"))
    assert not llm_budget.is_token_limit_error(RuntimeError("connection reset"))
