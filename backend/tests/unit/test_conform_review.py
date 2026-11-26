"""conform_review（对稿成片 P2a：LLM extra 分级）单元测试。

纯单元：不连库、不调真模型。覆盖：
* 无 extra 段 → 不调用 LLM（无需分级）；
* LLM 未配置 → 回退 local-rule（不抛出、不冒充）；
* LLM 裁决执法：未知 segment_id 忽略并计数、非法分类钳到 offtopic；
* 预算台账：预留+实际各记一条。
"""

import json
from types import SimpleNamespace

from src import conform_review as cr
from src.conform_review import ReviewOut, ExtraVerdict

EXTRAS = [
    {"segment_id": 7, "start_ms": 3200, "end_ms": 6000, "text": "顺便说一句下周有义诊。"},
    {"segment_id": 8, "start_ms": 6200, "end_ms": 7000, "text": "这个药在同一时间吃效果最好。"},
]
MATCHED = [
    {"index": 0, "status": "matched", "text": "免疫治疗出现副作用，先别急着停药。"},
    {"index": 1, "status": "matched", "text": "定期复查很重要。"},
]


def _fake_config():
    from types import SimpleNamespace
    return SimpleNamespace(llm="dsh:test-model", openai_api_key=None,
                           google_api_key=None, anthropic_api_key=None,
                           ollama_base_url=None, ollama_api_key=None,
                           as_runtime_settings=lambda: {})


def test_no_extra_segments_skips_llm(monkeypatch):
    monkeypatch.setattr(cr, "_get_agent",
                        lambda: (_ for _ in ()).throw(AssertionError("无 extra 段不应建 Agent")))
    result = cr.review_extra_segments({"extra_segments": [], "sentences": MATCHED},
                                      job_id="j0")
    assert result["engine"] == "local-rule"
    assert any("无需分级" in w for w in result["warnings"])
    assert result["verdicts"] == []


def test_fallback_when_llm_unconfigured(monkeypatch):
    monkeypatch.setattr(cr, "get_config", _fake_config)

    def _no_llm():
        raise RuntimeError("对稿分级需要 LLM：未配置")
    monkeypatch.setattr(cr, "_ensure_llm_ready", _no_llm)
    result = cr.review_extra_segments({"extra_segments": EXTRAS, "sentences": MATCHED},
                                      job_id="j1")
    assert result["engine"] == "local-rule"
    assert result["verdicts"] == []
    assert any("LLM 分级不可用" in w for w in result["warnings"])


def test_verdicts_enforced(monkeypatch):
    monkeypatch.setattr(cr, "get_config", _fake_config)
    monkeypatch.setattr(cr, "_ensure_llm_ready", lambda: None)

    class _FakeAgent:
        def run_sync(self, prompt):
            assert "文稿外多说段" in prompt and "文稿已拍句子" in prompt
            return SimpleNamespace(output=ReviewOut(verdicts=[
                ExtraVerdict(segment_id="7", classification="risk",
                             coherent_removal=True, reason="推广活动，合规风险"),
                ExtraVerdict(segment_id="8", classification="golden",
                             coherent_removal=False, reason="服药时间提醒，医学有价值"),
                ExtraVerdict(segment_id="999", classification="risk"),  # 未知 id → 忽略
                ExtraVerdict(segment_id="7", classification="hype"),    # 非法分类 → 钳 offtopic
            ]))
    monkeypatch.setattr(cr, "_get_agent", lambda: _FakeAgent())

    budget_calls = []
    import src.llm_budget as budget
    monkeypatch.setattr(budget, "record_call",
                        lambda kind, **kw: budget_calls.append((kind, kw.get("note"))))
    monkeypatch.setattr(budget, "extract_usage", lambda result: {"total_tokens": 100})

    result = cr.review_extra_segments({"extra_segments": EXTRAS, "sentences": MATCHED},
                                      job_id="j2")
    assert result["engine"] == "llm:dsh:test-model"
    verdicts = {v["segment_id"]: v for v in result["verdicts"]}
    # 未知 id 999 被忽略；重复 id 7 后到者覆盖（非法分类钳 offtopic）
    assert set(verdicts) == {"7", "8"}
    assert verdicts["8"]["classification"] == "golden"
    assert verdicts["8"]["coherent_removal"] is False
    assert any("忽略" in w for w in result["warnings"])
    assert ("conform_review", "P2a 预留") in budget_calls
    assert ("conform_review", "P2a 实际") in budget_calls
