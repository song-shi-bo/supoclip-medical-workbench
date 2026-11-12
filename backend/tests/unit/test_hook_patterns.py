"""钩子模式库（hook_patterns）契约测试：打分、排序、指纹、逐字执法、自动刷新。

核心前提（用户 2026-11-12 定稿）：钩子必须逐字来自 29 分钟转写原话——
模式库只是筛选标尺（结构归类打分排序），模型只准选、不准写；
执法：hook_first_line 去标点归一后必须命中转写，否则替换为切片内逐字候选。
自动刷新（用户接受全自动）：文稿入库后检测新增素材 → 计数更新 + LLM 新结构
提议（LLM 失败不影响计数）→ 版本 +1 → 旧计划缓存自动失效。
"""

from __future__ import annotations

import json

import pytest

from src import clip_planning as cp
from src import hook_pattern_refresh as refresh
from src import hook_patterns as hp
from src import llm_budget


# ---------------------------------------------------------------------------
# 结构打分
# ---------------------------------------------------------------------------

def test_score_sentence_classifies_patterns() -> None:
    scored = dict(hp.score_sentence("打针是大概多长时间打一次？"))
    assert "P4" in scored or "P3" in scored          # 提问直击
    scored = dict(hp.score_sentence("每三个月一次，记住3个复查要点"))
    assert "P3" in scored                            # 数字预告
    scored = dict(hp.score_sentence("很多人以为晚期肺癌是绝症，其实它正在变成慢性病"))
    assert "P2" in scored                            # 认知反差
    scored = dict(hp.score_sentence("错过最佳救治时间，风险非常高"))
    assert "P1" in scored                            # 案例警示/后果
    scored = dict(hp.score_sentence("大于六十岁的高血压患者要注意了"))
    assert "P6" in scored                            # 人群点名


def test_rank_candidates_filters_and_orders() -> None:
    texts = [
        "嗯嗯。",                                     # 过短 → 过滤
        "这是一句非常非常非常长的转写句子" * 8,        # 过长 → 过滤
        "第一，这个痣不对称，两边形状不一致。",
        "直径超过6毫米，最近还在进展变化。",
    ]
    ranked = hp.rank_candidates(texts)
    assert all(c["text"] != "嗯嗯。" for c in ranked)
    assert len(ranked) <= 8
    assert ranked == sorted(ranked, key=lambda c: c["score"], reverse=True)


# ---------------------------------------------------------------------------
# 身份与指纹
# ---------------------------------------------------------------------------

def test_identity_deterministic_and_changes_with_library(tmp_path) -> None:
    source = hp.default_library_path()
    raw = json.loads(source.read_text(encoding="utf-8"))
    raw["patterns"][0]["structure"] += "（改动）"
    target = tmp_path / "changed.json"
    target.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    a = hp.identity(source)
    b = hp.identity(target)
    assert a["sha256"] and b["sha256"] and a["sha256"] != b["sha256"]
    assert a["enabled"] is True and b["enabled"] is True


def test_library_missing_disables_identity(tmp_path) -> None:
    assert hp.identity(tmp_path / "nope.json")["enabled"] is False


# ---------------------------------------------------------------------------
# 逐字执法（plan_clips 内）
# ---------------------------------------------------------------------------

def _segments() -> list[dict]:
    rows = [
        (0, "咱们先说说这个黑色素瘤的复查安排。", 0, 6000),
        (1, "这种情况建议尽快到皮肤科就诊。", 6500, 13000),
        (2, "直径超过6毫米，最近还在进展变化。", 13500, 20000),
        (3, "行了行了，说点别的。", 20500, 26000),
    ]
    return [{"segment_id": f"seg_{i:04d}", "start_ms": s, "end_ms": e, "text": t}
            for i, t, s, e in rows]


def _analysis() -> dict:
    return {"keywords": [], "golden_quotes": [], "topic_units": [
        {"title": "痣的ABCDE就医信号", "segment_ids": [s["segment_id"] for s in _segments()],
         "hook_quote": "", "patient_question": "", "mechanism": "具体疑问"}],
        "candidate_intervals": []}


def _slice_plan(hook: str) -> cp._SlicePlanOut:
    return cp._SlicePlanOut.model_validate({
        "video_summary": "", "slice_count": 1,
        "slices": [{"id": 1, "time_range": ["00:00:01", "00:00:26"],
                    "theme": "痣的ABCDE就医信号", "hook_first_line": hook,
                    "cover_text": "", "content_summary": "", "completeness_note": "",
                    "risk_flags": []}],
        "discarded_segments": [],
    })


def _run_plan(monkeypatch: pytest.MonkeyPatch, hook: str) -> dict:
    monkeypatch.setattr(cp, "_model_ready", lambda: (True, "openai:test-model", ""))
    monkeypatch.setattr(cp, "_run_plan_agent",
                        lambda model, prompt: _slice_plan(hook))
    return cp.plan_clips(_segments(), _analysis(), job_id="job-hook", project_id="proj-hook")


def test_non_verbatim_hook_replaced_with_transcript_candidate(
        monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(llm_budget, "_ledger_root", lambda: tmp_path / "ledger")
    record = _run_plan(monkeypatch, "关注点赞不迷路我们下期见呀")
    clip = record["clips"][0]
    assert clip["hook_first_line"] != "关注点赞不迷路我们下期见呀"
    replaced = [n for n in record["notes"] if "钩子逐字执法" in n]
    assert replaced, "非逐字钩子必须被执法替换并留痕"


def test_verbatim_hook_from_transcript_is_kept(monkeypatch: pytest.MonkeyPatch,
                                               tmp_path) -> None:
    monkeypatch.setattr(llm_budget, "_ledger_root", lambda: tmp_path / "ledger")
    monkeypatch.setattr(cp, "_model_ready", lambda: (True, "openai:test-model", ""))

    def fake_agent(model, prompt):
        plan = _slice_plan("这种情况建议尽快到皮肤科就诊。")   # 转写里的逐字原句
        return plan

    monkeypatch.setattr(cp, "_run_plan_agent", fake_agent)
    record = cp.plan_clips(_segments(), _analysis(), job_id="job-hook-verbatim",
                           project_id="proj-hook-verbatim")
    assert record["hook_patterns"]["verified_verbatim"] == 1
    assert record["hook_patterns"]["replaced_non_verbatim"] == 0


# ---------------------------------------------------------------------------
# 自动刷新（素材驱动）
# ---------------------------------------------------------------------------

def _write_library(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = hp.default_library_path()
    raw = json.loads(source.read_text(encoding="utf-8"))
    target = tmp_path / "hook_patterns.json"
    target.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("HOOK_PATTERNS_PATH", str(target))
    hp._cache.clear()


def test_refresh_no_new_material_is_noop(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_library(tmp_path, monkeypatch)
    monkeypatch.setattr(refresh, "_new_scripts_since", lambda since: [])
    summary = refresh.refresh_if_new_material()
    assert summary["updated"] is False
    assert summary["mode"] == "no_new_material"


def test_refresh_updates_counts_and_bumps_version(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_library(tmp_path, monkeypatch)
    monkeypatch.setattr(refresh, "PROPOSAL_THRESHOLD", 1)  # 2条新增 ≥ 阈值 → 触发提议
    monkeypatch.setattr(refresh, "_new_scripts_since", lambda since: [
        {"id": "s1", "title": "t", "body": "为什么年年体检一发现癌症就是中晚期？",
         "created_at": "2026-11-12T10:00:00"},
        {"id": "s2", "title": "t2", "body": "这是定期复查的安排。",
         "created_at": "2026-11-12T10:01:00"},
    ])
    monkeypatch.setattr(refresh, "_llm_propose",
                        lambda openings, existing: [
                            {"structure": "症状清单后给一个安心结论", "mechanisms": ["身份共鸣"]}])
    summary = refresh.refresh_if_new_material()
    assert summary["updated"] is True
    assert summary["proposals_added"] == 1
    assert summary["new_version"].endswith("v2")
    library = json.loads(hp.default_library_path().read_text(encoding="utf-8"))
    assert library["version"] == summary["new_version"]
    assert any("症状清单" in str(p.get("structure")) for p in library["patterns"])
    archive_dir = tmp_path / "archive"
    assert archive_dir.is_dir() and list(archive_dir.iterdir())


def test_refresh_llm_failure_still_updates_counts(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_library(tmp_path, monkeypatch)
    monkeypatch.setattr(refresh, "PROPOSAL_THRESHOLD", 1)  # 1条新增 ≥ 阈值 → 触发提议
    monkeypatch.setattr(refresh, "_new_scripts_since", lambda since: [
        {"id": "s1", "title": "t", "body": "为什么年年体检一发现癌症就是中晚期？",
         "created_at": "2026-11-12T10:00:00"}])
    def _boom(openings, existing):
        raise RuntimeError("llm down")
    monkeypatch.setattr(refresh, "_llm_propose", _boom)
    summary = refresh.refresh_if_new_material()
    assert summary["updated"] is True
    assert summary["counts_touched"] >= 1
    assert any("LLM 提议失败" in n for n in summary["notes"])


def test_refresh_respects_state_watermark(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_library(tmp_path, monkeypatch)
    state_file = tmp_path / ".sync_state.json"
    state_file.write_text(json.dumps(
        {"last_scripts_created_at": "2026-11-12T23:59:59"}), encoding="utf-8")
    monkeypatch.setattr(refresh, "_new_scripts_since", lambda since: [])
    summary = refresh.refresh_if_new_material()
    assert summary["mode"] == "no_new_material"
