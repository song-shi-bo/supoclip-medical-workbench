"""纠错批次并行化单测：并发执行 / 结果保序 / 单批回退不中断批次 / 术语表注入。

模型调用层（_run_patch_call）整体 monkeypatch——不触网、不触真实 LLM。
"""
import threading
import time

import pytest

from src import transcript_correction_llm as tcl

SEGS = [{"start": float(i), "end": float(i) + 1.0, "text": f"第{i}段文本"} for i in range(4)]


@pytest.fixture()
def patched_env(monkeypatch):
    monkeypatch.setattr(tcl, "_model_ready", lambda: (True, "test-model", ""))
    monkeypatch.setattr(tcl, "_choose_mode", lambda prepared, glossary_terms=None: "batch")
    monkeypatch.setattr(tcl, "_batch_bounds", lambda total: [(0, 2), (2, 4)])
    monkeypatch.setenv("CONTENT_CORRECTION_CONCURRENCY", "2")


def _mk_outcome(start, batch, changed_first=False, fail=False, delay=0.0):
    time.sleep(delay)
    if fail:
        return {"items": None, "error": "模拟失败", "token_limit": False, "usage": {}}
    items = []
    if changed_first:
        items.append(tcl._PatchItem(index=start, corrected_text=batch[0]["text"] + "（已纠）",
                                    reason="测试", uncertain=False))
    return {"items": items, "error": None, "token_limit": False, "usage": {}}


def test_parallel_batches_run_concurrently_and_in_order(monkeypatch, patched_env):
    """两批各 0.4s：并发应 <0.7s 完成；结果按批次序号原位应用。"""
    calls = []
    lock = threading.Lock()

    def fake_call(model_name, batch, start, prepared, batched_mode, progress, label, job_id,
                  glossary_terms=None):
        with lock:
            calls.append((start, threading.current_thread().name, time.monotonic()))
        return _mk_outcome(start, batch, changed_first=(start == 0), delay=0.4)

    monkeypatch.setattr(tcl, "_run_patch_call", fake_call)
    started = time.monotonic()
    result = tcl.correct_segments_with_model(
        [dict(s) for s in SEGS], glossary_terms=["术语A"])
    elapsed = time.monotonic() - started

    assert elapsed < 0.7, f"未并发执行（耗时 {elapsed:.2f}s）"
    assert len({c[1] for c in calls}) == 2, "应在不同线程执行"
    assert result["correction_summary"]["llm_batches"] == 2
    assert result["correction_summary"]["fallback_batches"] == 0
    segs = result["asr_segments"]
    assert segs[0]["text"].endswith("（已纠）")
    assert [s["text"][:2] for s in segs] == ["第0", "第1", "第2", "第3"]


def test_single_batch_failure_falls_back_without_breaking(monkeypatch, patched_env):
    """批 2 失败 → 该批走保守规整，批 1 正常；engine 如实带 +local-fallback。"""

    def fake_call(model_name, batch, start, prepared, batched_mode, progress, label, job_id,
                  glossary_terms=None):
        return _mk_outcome(start, batch, changed_first=(start == 0), fail=(start == 2))

    monkeypatch.setattr(tcl, "_run_patch_call", fake_call)
    result = tcl.correct_segments_with_model([dict(s) for s in SEGS])

    summary = result["correction_summary"]
    assert summary["llm_batches"] == 1 and summary["fallback_batches"] == 1
    assert "local-fallback" in summary["engine"]
    segs = result["asr_segments"]
    assert segs[0]["text"].endswith("（已纠）")
    assert segs[2]["text"] == "第2段文本"


def test_serial_mode_when_concurrency_1(monkeypatch, patched_env):
    """并发=1 时保持串行行为（回归保护）。"""
    monkeypatch.setenv("CONTENT_CORRECTION_CONCURRENCY", "1")

    def fake_call(model_name, batch, start, prepared, batched_mode, progress, label, job_id,
                  glossary_terms=None):
        return _mk_outcome(start, batch, changed_first=(start == 0))

    monkeypatch.setattr(tcl, "_run_patch_call", fake_call)
    result = tcl.correct_segments_with_model([dict(s) for s in SEGS])
    assert result["correction_summary"]["llm_batches"] == 2
    assert result["asr_segments"][0]["text"].endswith("（已纠）")


def test_glossary_terms_reach_prompt_builder():
    """术语表参数注入 _build_user_prompt：有词表出现术语段，无词表逐字节一致。"""
    prepared = [{"text": "xx"}, {"text": "yy"}]
    with_terms = tcl._build_user_prompt(prepared, 0, prepared, batched_mode=False,
                                        glossary_terms=["氨氯地平", "汞柱"])
    without_terms = tcl._build_user_prompt(prepared, 0, prepared, batched_mode=False,
                                           glossary_terms=None)
    assert "项目术语表" in with_terms and "氨氯地平" in with_terms and "汞柱" in with_terms
    assert "项目术语表" not in without_terms
    assert without_terms == tcl._build_user_prompt(prepared, 0, prepared, batched_mode=False)
