"""speaker_labeling 单元测试：窗口解析、保守回退、指纹失效、overlay 存取与非破坏合并。

不调真实模型：``_model_ready`` / ``_LABEL_LLM_FN`` 打桩。
"""
from __future__ import annotations

import json

import pytest

from src import speaker_labeling as sl


def _segs(n: int = 6) -> list[dict]:
    roles = ["医生", "患者", "家属"]
    return [
        {"segment_id": f"seg_{i+1:04d}",
         "start_ms": i * 1000, "end_ms": i * 1000 + 900,
         "text": f"第{i+1}句话", "corrected_text": f"第{i+1}句话",
         "speaker": "未标注", "fallback_role": roles[i % 3]}
        for i in range(n)
    ]


def _ok_response(segs: list[dict], speaker_of=None) -> str:
    labels = [{"segment_id": s["segment_id"],
               "speaker": (speaker_of or (lambda s: s["fallback_role"]))(s),
               "reason": "测试"} for s in segs]
    return json.dumps({"labels": labels}, ensure_ascii=False)


@pytest.fixture(autouse=True)
def _ready(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sl, "_model_ready", lambda: (True, "fixture-model", ""))


# ---------------------------------------------------------------- 主流程
def test_label_transcript_happy_path(monkeypatch: pytest.MonkeyPatch):
    segs = _segs()
    monkeypatch.setattr(sl, "_LABEL_LLM_FN", lambda prompt: _ok_response(segs))
    record = sl.label_transcript(segs, transcript_id="tr-x", transcript_version=2)
    assert record["schema"] == sl.SPEAKER_SCHEMA_VERSION
    assert record["prompt_version"] == "speaker-label-prompt/2026-11-06-a1"
    assert record["llm_error"] is None
    assert record["labels"] == {s["segment_id"]: s["fallback_role"] for s in segs}
    assert record["stats"] == {"医生": 2, "患者": 2, "家属": 2, "未知": 0}
    assert record["windows"]["total"] == 1 and record["windows"]["failed"] == 0
    assert record["fingerprint"] == sl.transcript_fingerprint(segs)


def test_label_transcript_windows_with_context_tail(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sl, "WINDOW_SEGMENTS", 3)
    seen: list[str] = []

    def fake(prompt: str) -> str:
        seen.append(prompt)
        ids = [line.split("\t")[0] for line in prompt.splitlines() if line.startswith("seg_")]
        # 需要标注的段：每窗最后 3 段（上下文尾巴也被列出但不应重复出错）
        target = ids[-3:]
        return json.dumps({"labels": [
            {"segment_id": i, "speaker": "医生", "reason": "r"} for i in target]},
            ensure_ascii=False)

    segs = _segs(9)
    monkeypatch.setattr(sl, "_LABEL_LLM_FN", fake)
    record = sl.label_transcript(segs)
    assert record["windows"]["total"] == 3
    assert len(seen) == 3
    # 第二窗的提示里带上第一窗尾部 4 段中的 3 段（窗口=3 时尾部=整窗）
    assert any("seg_0003" in seen[1] for _ in [0])
    assert set(record["labels"]) == {s["segment_id"] for s in segs}
    assert record["stats"]["未知"] == 0


def test_label_transcript_invalid_json_falls_back_unknown(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sl, "_LABEL_LLM_FN", lambda prompt: "不是JSON")
    record = sl.label_transcript(_segs())
    assert record["labels"] == {}
    assert record["stats"]["未知"] == 6
    assert record["llm_error"] == "不是JSON" or record["windows"]["warnings"]


def test_label_transcript_drops_unknown_ids_and_bad_labels(monkeypatch: pytest.MonkeyPatch):
    raw = json.dumps({"labels": [
        {"segment_id": "seg_0001", "speaker": "医生", "reason": ""},
        {"segment_id": "seg_9999", "speaker": "患者", "reason": ""},   # 集合外 → 丢弃
        {"segment_id": "seg_0002", "speaker": "主持人", "reason": ""},  # 非法标签 → 未知
    ]}, ensure_ascii=False)
    monkeypatch.setattr(sl, "_LABEL_LLM_FN", lambda prompt: raw)
    record = sl.label_transcript(_segs())
    assert record["labels"] == {"seg_0001": "医生"}
    assert record["stats"]["未知"] == 5
    assert record["windows"]["warnings"], "缺段必须记录警告"


def test_label_transcript_llm_unconfigured(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sl, "_model_ready", lambda: (False, "", "LLM 未配置"))
    record = sl.label_transcript(_segs())
    assert record["llm_error"] == "LLM 未配置"
    assert record["labels"] == {}
    assert record["stats"]["未知"] == 6


def test_label_transcript_empty_segments():
    record = sl.label_transcript([])
    assert record["segment_count"] == 0
    assert record["llm_error"]


# ---------------------------------------------------------------- 存取与合并
def test_save_load_roundtrip(tmp_path):
    segs = _segs()
    record = sl.label_transcript(segs, transcript_id="tr-x", transcript_version=1)
    record["labels"] = {s["segment_id"]: s["fallback_role"] for s in segs}
    path = sl.save_labels(tmp_path, "abc123", record)
    assert path.name == "speaker_labels.json"
    loaded = sl.load_labels(tmp_path, "abc123")
    assert loaded is not None and loaded["fingerprint"] == record["fingerprint"]
    assert sl.load_labels(tmp_path, "nope") is None


def test_load_labels_rejects_wrong_schema(tmp_path):
    path = sl.labels_path(tmp_path, "abc123")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema": "other/9"}), encoding="utf-8")
    assert sl.load_labels(tmp_path, "abc123") is None


def test_merge_only_fills_unlabeled_and_known(tmp_path):
    segs = _segs()
    segs[0]["speaker"] = "医生"  # 已有可信标签 → 不覆盖
    record = {"fingerprint": sl.transcript_fingerprint(segs),
              "labels": {"seg_0001": "患者",  # 与已有冲突 → 跳过
                         "seg_0002": "家属",
                         "seg_0003": "未知"}}  # 未知 → 不覆盖
    result = sl.merge_speaker_labels(segs, record)
    assert result["merged"] == 1 and result["skipped_existing"] == 1
    assert segs[0]["speaker"] == "医生"
    assert segs[1]["speaker"] == "家属"
    assert segs[2]["speaker"] == "未标注"


def test_merge_rejects_fingerprint_mismatch():
    segs = _segs()
    record = {"fingerprint": "deadbeef", "labels": {s["segment_id"]: "医生" for s in segs}}
    result = sl.merge_speaker_labels(segs, record)
    assert result["merged"] == 0
    assert "指纹不匹配" in result["reason"]
    assert all(s["speaker"] == "未标注" for s in segs)


def test_merge_without_record():
    result = sl.merge_speaker_labels(_segs(), None)
    assert result["merged"] == 0 and result["reason"] == "无 overlay"


def test_label_transcript_resumes_from_prior_overlay(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sl, "WINDOW_SEGMENTS", 3)
    segs = _segs(9)
    prior = {"fingerprint": sl.transcript_fingerprint(segs),
             "labels": {f"seg_{i:04d}": "医生" for i in range(1, 4)},  # 窗口1 已标好
             "model": "old-model"}
    calls: list[str] = []

    def fake(prompt: str) -> str:
        calls.append(prompt)
        ids = [line.split("\t")[0] for line in prompt.splitlines() if line.startswith("seg_")]
        return json.dumps({"labels": [
            {"segment_id": i, "speaker": "患者", "reason": ""} for i in ids[-3:]]},
            ensure_ascii=False)

    monkeypatch.setattr(sl, "_LABEL_LLM_FN", fake)
    record = sl.label_transcript(segs, resume_from=prior)
    assert len(calls) == 2  # 只调窗口2、3；窗口1 复用
    assert record["windows"]["resumed"] == 1
    assert all(record["labels"][f"seg_{i:04d}"] == "医生" for i in range(1, 4))
    assert all(record["labels"][f"seg_{i:04d}"] == "患者" for i in range(4, 10))
    assert record["stats"]["未知"] == 0


def test_resume_ignores_fingerprint_mismatch(monkeypatch: pytest.MonkeyPatch):
    segs = _segs(3)
    prior = {"fingerprint": "stale", "labels": {"seg_0001": "医生"}}
    calls: list[str] = []

    def fake(prompt: str) -> str:
        calls.append(prompt)
        return _ok_response(segs)

    monkeypatch.setattr(sl, "_LABEL_LLM_FN", fake)
    record = sl.label_transcript(segs, resume_from=prior)
    assert len(calls) == 1  # 指纹不符 → 全部重标
    assert record["labels"]["seg_0001"] == segs[0]["fallback_role"]


def test_partial_failure_keeps_prior_labels(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sl, "WINDOW_SEGMENTS", 3)
    segs = _segs(6)
    prior = {"fingerprint": sl.transcript_fingerprint(segs),
             "labels": {f"seg_{i:04d}": "医生" for i in range(1, 4)}}

    def fake(prompt: str) -> str:
        raise RuntimeError("429 限流")

    monkeypatch.setattr(sl, "_LABEL_LLM_FN", fake)
    record = sl.label_transcript(segs, resume_from=prior)
    assert record["llm_error"] is None  # 有续跑成果，不算整体失败
    assert record["stats"]["医生"] == 3 and record["stats"]["未知"] == 3
    assert record["windows"]["failed"] == 1


def test_summary_for_payload_shape():
    record = {"transcript_id": "tr-x", "transcript_version": 2, "stats": {"医生": 1},
              "llm_error": None}
    summary = sl.summary_for_payload(record, {"merged": 3, "skipped_existing": 0, "reason": ""})
    assert summary["transcript_id"] == "tr-x"
    assert summary["merge"]["merged"] == 3
    assert sl.summary_for_payload(None, {}) == {}
