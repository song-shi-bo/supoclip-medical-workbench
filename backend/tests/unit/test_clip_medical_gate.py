"""医学指南库（第三库）与切片医学闸门契约测试（2026-11-10 阶段一）。

核心契约：
* 快照加载与指纹：快照缺失 → 闸门禁用；内容变化 → snapshot_sha256 变化（进 plan 指纹）；
* 匹配：医学对话（ABCDE 信号）命中指南条目（向量或词法路径），家常聊天判 none；
* 领域激活：全片无命中 → annotate_only 只标注不剔除（防误伤库外领域视频）；
* 硬剔除：enforce 模式下医学密度低于阈值且主题块级别 none 的切片被程序移入
  discarded（type=非医学闲聊），宁缺毋滥；
* 提示词A不动：拼装层只新增【医学主题匹配】背景行（对转写的程序标注，不注入
  条目正文）；系统提示词逐字节冻结断言在 test_clip_refinement.py。
"""

from __future__ import annotations

import pytest

from src import clip_medical_gate as gate
from src import clip_planning as cp
from src import clip_purify as pu
from src import llm_budget
from src import medical_kb as mkb


@pytest.fixture(autouse=True)
def _isolated_llm_ledger(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """走 plan_clips 的用例会写 llm_budget 台账；重定向到临时目录。"""
    monkeypatch.setattr(llm_budget, "_ledger_root", lambda: tmp_path / "llm_usage")


def seg(i: int, text: str, s_ms: int, e_ms: int) -> dict:
    return {"segment_id": f"seg_{i:04d}", "start_ms": s_ms, "end_ms": e_ms, "text": text}


MEDICAL_TEXTS = [
    "今天我们聊聊痣的ABCDE信号。",
    "第一，这个痣不对称，两边形状不一致。",
    "第二，边缘不规则，还有颜色不均。",
    "直径超过6毫米，最近还在进展变化。",
    "这种情况下建议尽快到皮肤科就诊。",
    "医生会评估是否需要活检。",
]
CHAT_TEXTS = [
    "行了行了，说点别的。",
    "下个月咱们一起去钓鱼吧。",
    "你上回借走的鱼竿还没还我。",
    "我孙子最近学游泳，可积极了。",
    "回头把你那渔具也捎过来。",
    "到时候咱们早点出发。",
]


def _segments() -> list[dict]:
    rows = [(i, MEDICAL_TEXTS[i], i * 7000, i * 7000 + 6500) for i in range(6)]
    rows += [(i + 6, CHAT_TEXTS[i], 42000 + i * 7000, 42000 + i * 7000 + 6500)
             for i in range(6)]
    return [seg(i, t, s, e) for i, t, s, e in rows]


def _analysis() -> dict:
    return {
        "keywords": ["痣", "黑色素瘤", "钓鱼"],
        "golden_quotes": [],
        "topic_units": [
            {"title": "痣的ABCDE就医信号", "segment_ids": [f"seg_{i:04d}" for i in range(6)],
             "info_seconds": 42},
            {"title": "周末钓鱼和家常闲聊",
             "segment_ids": [f"seg_{i:04d}" for i in range(6, 12)], "info_seconds": 42},
        ],
        "candidate_intervals": [],
    }


def _chat_only_analysis() -> dict:
    data = _analysis()
    data["topic_units"] = [data["topic_units"][1]]
    return data


def _slice_plan() -> cp._SlicePlanOut:
    return cp._SlicePlanOut.model_validate({
        "video_summary": "皮肤科医生讲解痣的ABCDE信号，中间穿插家常聊天",
        "slice_count": 2,
        "slices": [
            {"id": 1, "time_range": ["00:00:01", "00:00:41"], "theme": "痣的ABCDE就医信号",
             "hook_first_line": "这个痣不对称要当心", "cover_text": "ABCDE信号",
             "content_summary": "讲解痣不对称、边缘不规则、颜色不均、直径超6毫米与进展五个信号。",
             "completeness_note": "五个信号在切片内讲完", "risk_flags": []},
            {"id": 2, "time_range": ["00:00:42", "00:01:21"], "theme": "周末钓鱼和家常闲聊",
             "hook_first_line": "下个月一起去钓鱼", "cover_text": "去钓鱼",
             "content_summary": "商量下个月一起去钓鱼，聊孙子和渔具。",
             "completeness_note": "一段家常闲聊", "risk_flags": []},
        ],
        "discarded_segments": [],
    })


# ---------------------------------------------------------------------------
# 快照与指纹
# ---------------------------------------------------------------------------

def test_snapshot_loads_with_meta_sha() -> None:
    identity = mkb.snapshot_identity()
    assert identity["enabled"] is True
    assert identity["entry_count"] >= 40
    assert len(identity["snapshot_sha256"]) == 64
    import json
    from pathlib import Path
    raw = json.loads(mkb.default_snapshot_path().read_text(encoding="utf-8"))
    assert identity["snapshot_sha256"] == raw["meta"]["snapshot_sha256"]


def test_snapshot_hash_changes_with_content(tmp_path) -> None:
    import json
    source = mkb.default_snapshot_path()
    raw = json.loads(source.read_text(encoding="utf-8"))
    raw["entries"][0]["statement"] = raw["entries"][0]["statement"] + "（改动）"
    target = tmp_path / "changed_snapshot.json"
    target.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    before = mkb.snapshot_identity(source)
    after = mkb.snapshot_identity(target)
    assert after["enabled"] is True
    assert after["snapshot_sha256"] != before["snapshot_sha256"]


def test_snapshot_missing_disables_gate(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = tmp_path / "nope.json"
    monkeypatch.setattr(mkb, "default_snapshot_path", lambda: missing)
    assert mkb.snapshot_identity()["enabled"] is False
    profile = gate.run_gate(_segments(), _analysis())
    assert profile["enabled"] is False
    assert profile["mode"] == "disabled"


# ---------------------------------------------------------------------------
# 匹配：医学 vs 家常
# ---------------------------------------------------------------------------

def test_medical_text_matches_and_chat_text_does_not() -> None:
    state = mkb.load_snapshot()
    assert state is not None
    medical = mkb.match_text(
        "这个痣不对称，边缘不规则，颜色不均，直径超过6毫米，还在进展变化", state)
    assert medical["level"] in ("strong", "weak")
    assert medical["score"] >= mkb.WEAK_THRESHOLD
    chat = mkb.match_text("下个月咱们一起去钓鱼吧，你上回借走的鱼竿还没还我。", state)
    assert chat["level"] == "none"
    assert chat["score"] < mkb.WEAK_THRESHOLD


# ---------------------------------------------------------------------------
# 闸门：领域激活与硬剔除
# ---------------------------------------------------------------------------

def test_run_gate_enforce_mode_with_annotations() -> None:
    profile = gate.run_gate(_segments(), _analysis())
    assert profile["enabled"] is True
    assert profile["mode"] == "enforce"
    levels = {ann["title"]: ann["level"] for ann in profile["unit_annotations"]}
    assert levels["痣的ABCDE就医信号"] in ("strong", "weak")
    assert levels["周末钓鱼和家常闲聊"] == "none"
    assert profile["medical_segment_ids"]


def test_run_gate_annotate_only_when_out_of_domain() -> None:
    profile = gate.run_gate(_segments(), _chat_only_analysis())
    assert profile["enabled"] is True
    assert profile["mode"] == "annotate_only"
    assert any("仅标注不剔除" in note for note in profile["notes"])


def test_enforce_clips_moves_chat_clip_in_enforce_mode() -> None:
    profile = gate.run_gate(_segments(), _analysis())
    index = cp._SegIndex(_segments())
    clips = [
        {"clip_id": "clip_med", "topic": "痣的ABCDE就医信号",
         "start_segment_id": "seg_0000", "end_segment_id": "seg_0005"},
        {"clip_id": "clip_chat", "topic": "周末钓鱼和家常闲聊",
         "start_segment_id": "seg_0006", "end_segment_id": "seg_0011"},
    ]
    kept, moved, decisions = gate.enforce_clips(profile, clips, index, [])
    assert [c["clip_id"] for c in kept] == ["clip_med"]
    assert len(moved) == 1
    assert moved[0]["type"] == gate.DISCARD_TYPE
    assert moved[0]["segment_ids"] == [f"seg_{i:04d}" for i in range(6, 12)]
    by_id = {d["clip_id"]: d for d in decisions}
    assert by_id["clip_med"]["decision"] == "kept"
    assert by_id["clip_chat"]["decision"] == "discarded_non_medical"
    assert kept[0]["medical_gate"]["decision"] == "kept"


def test_enforce_clips_annotate_only_never_moves() -> None:
    profile = gate.run_gate(_segments(), _chat_only_analysis())
    assert profile["mode"] == "annotate_only"
    index = cp._SegIndex(_segments())
    clips = [{"clip_id": "clip_chat", "topic": "周末钓鱼和家常闲聊",
              "start_segment_id": "seg_0006", "end_segment_id": "seg_0011"}]
    kept, moved, decisions = gate.enforce_clips(profile, clips, index, [])
    assert moved == []
    assert [c["clip_id"] for c in kept] == ["clip_chat"]
    assert decisions[0]["would_discard"] is True


def test_context_lines_do_not_leak_entry_statement() -> None:
    profile = gate.run_gate(_segments(), _analysis())
    lines = gate.context_lines(profile)
    assert lines
    assert lines[0].startswith("【医学主题匹配")
    joined = "\n".join(lines)
    import json
    snapshot = json.loads(mkb.default_snapshot_path().read_text(encoding="utf-8"))
    for entry in snapshot["entries"][:8]:
        assert entry["statement"][:20] not in joined  # 条目正文不进提示词


# ---------------------------------------------------------------------------
# 与 plan_clips 的集成：标注透传 + 硬剔除 + 指纹身份
# ---------------------------------------------------------------------------

def _patch_plan_llm(monkeypatch: pytest.MonkeyPatch, prompts: list[str]) -> None:
    monkeypatch.setattr(cp, "_model_ready", lambda: (True, "openai:test-model", ""))
    captured = prompts

    def _agent(model, prompt):
        captured.append(prompt)
        return _slice_plan()

    monkeypatch.setattr(cp, "_run_plan_agent", _agent)


def test_plan_clips_attaches_gate_and_discards_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts: list[str] = []
    _patch_plan_llm(monkeypatch, prompts)
    record = cp.plan_clips(_segments(), _analysis(), job_id="job-gate-test",
                           project_id="proj-gate-test")
    # 拼装层透传：提示词里有医学标注背景行，但没有条目正文
    assert prompts and "【医学主题匹配" in prompts[0]
    # 硬剔除：聊家常切片被移入 discarded
    assert record["medical_gate"]["mode"] == "enforce"
    assert record["medical_gate"]["moved_count"] == 1
    assert any(d["type"] == gate.DISCARD_TYPE for d in record["discarded"])
    assert all(c["topic"] != "周末钓鱼和家常闲聊" for c in record["clips"])
    kept = [c for c in record["clips"] if c["topic"] == "痣的ABCDE就医信号"]
    assert kept and kept[0]["medical_gate"]["decision"] == "kept"


def test_plan_fingerprint_changes_with_gate_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    segments, analysis = _segments(), _analysis()
    monkeypatch.setattr(cp, "_model_ready", lambda: (True, "openai:test-model", ""))
    identity_a = mkb.snapshot_identity()
    identity_b = dict(identity_a, snapshot_sha256="0" * 64)
    provenance_a = cp.build_current_plan_provenance(
        segments, analysis, model="openai:test-model", medical_gate_identity=identity_a)
    provenance_b = cp.build_current_plan_provenance(
        segments, analysis, model="openai:test-model", medical_gate_identity=identity_b)
    assert provenance_a["input_fingerprint"] != provenance_b["input_fingerprint"]
    assert provenance_a["rules_snapshot"]["medical_gate"]["snapshot_sha256"] == \
        identity_a["snapshot_sha256"]


def test_gate_failure_degrades_without_blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args, **_kwargs):
        raise RuntimeError("embed exploded")

    monkeypatch.setattr(mkb, "load_snapshot", _boom)
    profile = gate.run_gate(_segments(), _analysis())
    assert profile["enabled"] is False
    assert profile["mode"] == "disabled"
    # 规划链路继续可用：闸门禁用时切片不受影响
    _patch_plan_llm(monkeypatch, [])
    record = cp.plan_clips(_segments(), _analysis(), job_id="job-gate-fallback",
                           project_id="proj-gate-fallback")
    assert record["medical_gate"]["mode"] == "disabled"
    assert record["clips"], "闸门禁用时规划不应为空"


# ---------------------------------------------------------------------------
# worker 契约（2026-11-11 E_CACHE_CONTEXT_CHANGED 回归钉）：
# worker 预计算 gate_profile + 富化分析喂 expected；plan_clips 传同一 profile——
# 两边 provenance 必须逐字节相等。worker 必须用「gate 跑完后的富化分析」构建
# expected，否则标注注入后 prompt sha 不同，契约拦截（真实事故 5e863396）。
# ---------------------------------------------------------------------------

def test_worker_contract_expected_matches_plan_record(monkeypatch: pytest.MonkeyPatch,
                                                      tmp_path) -> None:
    prompts: list[str] = []
    _patch_plan_llm(monkeypatch, prompts)
    segments, analysis = _segments(), _analysis()
    gate_profile = gate.run_gate(segments, analysis)
    assert gate_profile["enabled"] is True
    enriched = dict(analysis)
    enriched["medical_annotations"] = gate_profile["unit_annotations"]
    expected = cp.build_current_plan_provenance(
        segments, enriched, model="openai:test-model",
        medical_gate_identity=gate_profile["identity"],
        purify_identity=pu.identity(None))
    record = cp.plan_clips(segments, analysis, gate_profile=gate_profile)
    assert record["source_provenance"] == expected
