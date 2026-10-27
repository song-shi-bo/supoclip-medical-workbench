"""AUTO_EDIT 完整切片规划（clip_planning，第四步）契约测试。

不调真实模型：``_model_ready`` / ``_run_plan_agent`` 打桩；worker 层把
editing/rendering/源片搬运设为守卫桩（被调用即失败，证明不渲染）。
核心契约：
* 输入只有纠错转写分段 + 分析产物；时间戳/连续区间/去重全部程序决定；
* 柔性时长只产 warning，无 reject、不阻断；
* 模型失败最多自动重试一次，仍失败 → 本地回退并保存最优计划。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from src import clip_planning as cp
from src import content_analysis as ca
from src import llm_budget
from src import studio_job_worker as worker
from src import studio_transcript_store as store
from src.api.routes import jobs as jobs_routes


@pytest.fixture(autouse=True)
def _isolated_llm_ledger(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """规划成功路径会写 llm_budget 台账；测试内重定向到临时目录。"""
    monkeypatch.setattr(llm_budget, "_ledger_root", lambda: tmp_path / "llm_usage")


def seg(i: int, text: str, s_ms: int, e_ms: int) -> dict:
    return {"segment_id": f"seg_{i:04d}", "start_ms": s_ms, "end_ms": e_ms, "text": text}


def _segments() -> list[dict]:
    """12 段 ≈ 320s：开场寒暄 + 三块完整内容 + 结尾引流。"""
    rows = [
        (0, "大家好，今天我们聊一聊高血压的常见误区。", 0, 5000),
        (1, "高血压是最常见的慢性病之一。", 6500, 20000),
        (2, "很多患者认为血压高了才需要吃药。", 21000, 40000),
        (3, "其实确诊高血压之后就应该按医嘱规范治疗。", 41000, 65000),
        (4, "血压长期升高会伤害心脑肾等靶器官。", 66000, 90000),
        (5, "所以降压治疗的目标是保护器官，而不只是降数字。", 91000, 115000),
        (6, "第二个误区是血压正常了就自行停药。", 130000, 150000),
        (7, "擅自停药可能导致血压反跳，甚至加重病情。", 151000, 175000),
        (8, "调整用药一定要听医生的。", 176000, 195000),
        (9, "第三，保健品不能代替正规的药物治疗。", 196000, 220000),
        (10, "任何保健品都没有确切的降压证据。", 221000, 245000),
        (11, "关注我，了解更多的健康知识。", 260000, 275000),
    ]
    return [seg(i, t, s, e) for i, t, s, e in rows]


def _analysis() -> dict:
    return {
        "keywords": ["高血压", "误区", "停药", "保健品", "血压"],
        "golden_quotes": [{
            "text": "擅自停药可能导致血压反跳，甚至加重病情。",
            "segment_ids": ["seg_0007"], "start_ms": 151000, "end_ms": 175000, "reason": "警示核心",
        }],
        "topic_units": [{"title": "高血压误区", "segment_ids": [f"seg_{i:04d}" for i in range(1, 11)],
                         "start_ms": 6500, "end_ms": 245000, "reason": ""}],
        "candidate_intervals": [
            {"start_ms": 6500, "end_ms": 115000, "topic_title": "误区一：规范治疗", "reason": ""},
            {"start_ms": 130000, "end_ms": 195000, "topic_title": "误区二：擅自停药", "reason": ""},
            {"start_ms": 196000, "end_ms": 245000, "topic_title": "误区三：保健品", "reason": ""},
        ],
    }


def _clip_out(**overrides):
    base = {
        "topic": "误区一：确诊就要规范治疗",
        "start_segment_id": "seg_0001", "end_segment_id": "seg_0005",
        "opening_context": "从慢性病定义讲起，不看前文也能懂",
        "core_points": ["确诊即治疗", "保护靶器官"],
        "closing_thought": "结尾落在完整结论之后",
        "evidence_segment_ids": ["seg_0003"],
        "cut_in_reason": "自然边界", "cut_out_reason": "整句结束且有停顿",
    }
    base.update(overrides)
    return cp._PlanClipOut(**base)


def test_llm_plan_validated_deduped_and_sorted(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cp, "_model_ready", lambda: (True, "openai:test-model", ""))
    monkeypatch.setattr(cp, "_run_plan_agent", lambda model, prompt: cp._PlanOut(clips=[
        _clip_out(),                                                   # 有效：108.5s
        _clip_out(topic="坏引用", start_segment_id="seg_9999"),         # 引用不存在 → 舍弃
        _clip_out(topic="高度重复", start_segment_id="seg_0002"),       # 与前者重叠>50% → 舍弃
    ], discarded=[cp._DiscardedOut(segment_ids=["seg_0000"], type="寒暄", reason="开场")]))
    record = cp.plan_clips(_segments(), _analysis(), transcript_version=1,
                           job_id="job-p", project_id="timeline_t")

    assert record["plan_source"] == "llm" and record["llm_error"] is None
    assert record["llm_calls"] == 1 and record["transcript_version"] == 1
    assert len(record["clips"]) == 1
    clip = record["clips"][0]
    assert clip["start_segment_id"] == "seg_0001" and clip["end_segment_id"] == "seg_0005"
    assert clip["start_ms"] == 6500 and clip["end_ms"] == 115000          # 时间戳由分段重算
    assert clip["duration_ms"] == 108500
    assert clip["clip_id"].startswith("clip-")
    assert any("引用分段不存在" in note for note in record["notes"])
    assert any("高度重复" in note for note in record["notes"])
    assert record["discarded"][0]["type"] == "寒暄"


def test_continuity_middle_segments_belong_to_clip(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cp, "_model_ready", lambda: (True, "openai:test-model", ""))
    monkeypatch.setattr(cp, "_run_plan_agent", lambda model, prompt: cp._PlanOut(
        clips=[_clip_out(start_segment_id="seg_0001", end_segment_id="seg_0005")], discarded=[]))
    record = cp.plan_clips(_segments(), _analysis(), transcript_version=1)
    clip = record["clips"][0]
    # 连续区间：start/end 之间的分段（2/3/4）全部归属该切片，时长=首尾差
    assert clip["duration_ms"] == 115000 - 6500
    assert clip["start_ms"] < clip["end_ms"]


def test_soft_duration_only_warns_never_rejects(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cp, "_model_ready", lambda: (True, "openai:test-model", ""))

    def run_case(rows, topic):
        segments = [seg(i, t, s, e) for i, (t, s, e) in enumerate(rows)]
        analysis = {"keywords": [], "golden_quotes": [], "topic_units": [], "candidate_intervals": []}
        monkeypatch.setattr(cp, "_run_plan_agent", lambda model, prompt: cp._PlanOut(
            clips=[_clip_out(topic=topic, start_segment_id="seg_0000",
                             end_segment_id=segments[-1]["segment_id"], evidence_segment_ids=[])], discarded=[]))
        return cp.plan_clips(segments, analysis, transcript_version=1)["clips"][0]

    # 偏短 44s（<90s）：保留 + warning
    short = run_case([("内容甲。", 0, 20000), ("内容乙。", 21000, 44000)], "偏短44s")
    assert short["completeness"] == "warning"
    assert any("90" in w for w in short["warnings"])
    # 可接受 108s（90–210s）：正常接受，无时长偏离 warning
    ok = run_case([(f"第{i}句完整表达。", i * 20000, (i + 1) * 20000 - 1500) for i in range(6)], "可接受108s")
    assert ok["duration_ms"] == 118500
    assert not any("偏离" in w for w in ok["warnings"])
    # 超长 240s（>210s）：保留 + warning（不截断、不拒绝）
    long_one = run_case([(f"第{i}句完整表达。", i * 20000, (i + 1) * 20000 - 1500) for i in range(13)], "超长240s")
    assert any("偏离" in w for w in long_one["warnings"])
    assert long_one["completeness"] == "warning"


def test_retry_smaller_windows_then_llm_succeeds(monkeypatch: pytest.MonkeyPatch):
    calls = {"n": 0}

    def flaky(model, prompt):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("网关瞬时错误")
        ids = [line.split("|")[0] for line in prompt.splitlines() if "|" in line and line.startswith("seg_")]
        return cp._PlanOut(clips=[_clip_out(start_segment_id=ids[0], end_segment_id=ids[-1],
                                           evidence_segment_ids=[])], discarded=[])

    monkeypatch.setattr(cp, "_model_ready", lambda: (True, "openai:test-model", ""))
    monkeypatch.setattr(cp, "_run_plan_agent", flaky)
    monkeypatch.setattr(cp.time, "sleep", lambda s: None)
    record = cp.plan_clips(_segments(), _analysis(), transcript_version=1)
    assert record["plan_source"] == "llm" and record["llm_calls"] == 3


def test_retry_exhausted_falls_back_with_best_effort(monkeypatch: pytest.MonkeyPatch):
    def always_fail(model, prompt):
        raise RuntimeError("配额不足")

    monkeypatch.setattr(cp, "_model_ready", lambda: (True, "openai:test-model", ""))
    monkeypatch.setattr(cp, "_run_plan_agent", always_fail)
    monkeypatch.setattr(cp.time, "sleep", lambda s: None)
    record = cp.plan_clips(_segments(), _analysis(), transcript_version=1)
    assert record["plan_source"] == "heuristic_fallback"
    assert record["llm_requested"] is True and 1 < record["llm_calls"] <= record["callback_budget"]
    assert "配额不足" in (record["llm_error"] or "")
    assert record["clips"], "回退也应保存当前最优计划（不阻断任务）"


def test_fallback_uses_analysis_trims_greeting_and_marks_discards(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cp, "_model_ready", lambda: (False, "", "LLM 未配置"))
    record = cp.plan_clips(_segments(), _analysis(), transcript_version=1)
    assert record["plan_source"] == "heuristic_fallback"
    assert record["llm_requested"] is False and record["llm_error"] == "LLM 未配置"
    assert record["clips"]
    for clip in record["clips"]:
        assert clip["start_segment_id"] != "seg_0000"      # 寒暄被裁掉
    all_discarded = [sid for d in record["discarded"] for sid in d["segment_ids"]]
    assert "seg_0000" in all_discarded                      # 舍弃记录含寒暄段


def test_clip_id_stable_for_same_range(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cp, "_model_ready", lambda: (False, "", "LLM 未配置"))
    first = cp.plan_clips(_segments(), _analysis(), transcript_version=1)
    second = cp.plan_clips(_segments(), _analysis(), transcript_version=1)
    ids_first = [c["clip_id"] for c in first["clips"]]
    ids_second = [c["clip_id"] for c in second["clips"]]
    assert ids_first and ids_first == ids_second


def test_empty_transcript_short_circuits():
    record = cp.plan_clips([], _analysis(), transcript_version=1)
    assert record["plan_source"] == "heuristic_fallback"
    assert record["llm_error"] == "纠错转写为空" and record["clips"] == []


# ---------------------------------------------------------------------------
# 第五轮修正：时长不淘汰 / 资源保护上限 / 恢复
# ---------------------------------------------------------------------------

def test_short_clip_25s_kept_with_short_duration_warning(monkeypatch: pytest.MonkeyPatch):
    """25 秒但主题完整的候选必须进入最终计划，带 short_duration warning。"""
    rows = [
        ("高血压确诊后就要规范用药。", 0, 12000),
        ("目标不是降数字而是保护器官。", 13000, 25000),
    ]
    segments = [seg(i, t, s, e) for i, (t, s, e) in enumerate(rows)]
    monkeypatch.setattr(cp, "_model_ready", lambda: (True, "openai:test-model", ""))
    monkeypatch.setattr(cp, "_run_plan_agent", lambda model, prompt: cp._PlanOut(
        clips=[_clip_out(topic="规范用药一句话", start_segment_id="seg_0000", end_segment_id="seg_0001",
                         opening_context="自含主题句", closing_thought="完整结论后结束", evidence_segment_ids=[])],
        discarded=[]))
    record = cp.plan_clips(segments, _analysis(), transcript_version=1)
    assert len(record["clips"]) == 1, "25s 主题完整的候选不允许被时长淘汰"
    clip = record["clips"][0]
    assert clip["completeness"] == "warning"
    assert any("short_duration" in w for w in clip["warnings"])
    assert not any("无法独立成立" in n and "25" in n for n in record["notes"])


def test_semantic_discard_45s_reason_must_not_be_duration(monkeypatch: pytest.MonkeyPatch):
    """45 秒但内容无法独立理解的候选：因语义原因进 discarded，理由不得是时长。"""
    monkeypatch.setattr(cp, "_model_ready", lambda: (True, "openai:test-model", ""))
    monkeypatch.setattr(cp, "_run_plan_agent", lambda model, prompt: cp._PlanOut(
        clips=[_clip_out()],
        discarded=[cp._DiscardedOut(segment_ids=["seg_0009", "seg_0010"], type="无法独立成立",
                                    reason="承接上文的零散补充，单独看不知道在讲什么")]))
    record = cp.plan_clips(_segments(), _analysis(), transcript_version=1)
    entry = next(d for d in record["discarded"] if d["type"] == "无法独立成立")
    assert "seg_0009" in entry["segment_ids"]
    assert "时长" not in entry["reason"]                    # 舍弃理由是语义，不是时长
    assert len(entry["reason"]) >= 5


def test_max_clips_is_resource_cap_not_target(monkeypatch: pytest.MonkeyPatch):
    """超过上限按综合得分保留最优，并在计划级 warnings 记录；上限可配置。"""
    rows = [(f"第{i}块完整内容，讲清一个独立观点。", i * 40000, (i + 1) * 40000 - 1500) for i in range(3)]
    segments = [seg(i, t, s, e) for i, (t, s, e) in enumerate(rows)]
    monkeypatch.setenv("CONTENT_PLAN_MAX_CLIPS", "2")
    monkeypatch.setattr(cp, "_model_ready", lambda: (True, "openai:test-model", ""))
    monkeypatch.setattr(cp, "_run_plan_agent", lambda model, prompt: cp._PlanOut(clips=[
        _clip_out(topic=f"主题{c}", start_segment_id=f"seg_{c:04d}", end_segment_id=f"seg_{c:04d}", evidence_segment_ids=[])
        for c in range(3)], discarded=[]))
    record = cp.plan_clips(segments, _analysis(), transcript_version=1)
    assert len(record["clips"]) == 2                       # 资源保护上限生效
    assert any("资源保护上限" in w for w in record["warnings"])
    assert any("资源保护上限" in note for note in record["notes"])


def test_worker_resume_reuses_persisted_analysis_without_model_call(
        monkeypatch: pytest.MonkeyPatch, tmp_path):
    """模拟 worker 重启：已保存 analysis artifact → 恢复继续规划，模型分析调用 0 次。"""
    monkeypatch.setenv("STUDIO_TASKS_ROOT", str(tmp_path / "tasks"))
    saved_analysis = {"plan_source": "llm", "llm_error": None, "llm_calls": 1,
                      "llm_duration_ms": 900, "model": "openai:test-model",
                      "rule_snapshot_id": "2026-10-13.10",
                      "keywords": ["高血压"], "golden_quotes": [], "topic_units": [],
                      "candidate_intervals": [], "transcript_version": 3}
    ca.save_analysis_record("autoedit_res01", dict(saved_analysis))  # 先真实落盘（模拟上次运行）

    def _guard(name):
        def _fail(*args, **kwargs):
            raise AssertionError(f"恢复路径禁止重新分析/渲染：{name}")
        return _fail

    monkeypatch.setattr(ca, "analyze_content", _guard("analyze_content"))
    monkeypatch.setattr(ca, "save_analysis_record", _guard("save_analysis_record"))

    monkeypatch.setattr(worker, "stage_editing", _guard("stage_editing"))
    monkeypatch.setattr(worker, "stage_rendering", _guard("stage_rendering"))
    monkeypatch.setattr(worker, "_materialise_source_for_job", _guard("_materialise_source_for_job"))
    monkeypatch.setattr("src.studio_transcript_store.load_corrected_transcript",
                        lambda key, transcript_id=None, transcript_version=None: {
                            "asr_segments": _segments(), "raw_segments": [],
                            "transcript_id": "tr-x", "transcript_version": 3})
    plan_calls: dict = {}

    def fake_plan(segments, analysis, **kwargs):
        plan_calls["analysis"] = analysis
        plan_calls["rule_snapshot_id"] = kwargs.get("rule_snapshot_id")
        return {"plan_source": "heuristic_fallback", "llm_error": None, "clips": [],
                "discarded": [], "notes": []}

    monkeypatch.setattr(cp, "plan_clips", fake_plan)
    monkeypatch.setattr(cp, "save_plan_record",
                        lambda job_id, record: {"path": "/tmp/clip_plan.json", "size_bytes": 10})
    monkeypatch.setattr(worker, "_http", lambda *a, **k: {})
    monkeypatch.setattr("src.r2.upload_file", lambda path, key: {"object_key": key, "size": 10})
    states: list[str] = []
    monkeypatch.setattr(worker, "_state",
                        lambda job_id, status, stage, progress, error=None: states.append(status))

    job = {"job_id": "autoedit_res01", "object_key": "local:/v.mp4", "project_id": "timeline_t",
           "config": {"job_kind": "auto_edit", "stop_after": "plan"}}
    worker.run_auto_edit_only(job)

    assert states[0] == "extracting_highlights"            # 恢复提示（复用已保存分析）
    assert states[-1] == "plan_ready"
    reused = plan_calls["analysis"]
    assert reused["plan_source"] == "llm" and reused["llm_calls"] == 1
    assert reused.get("_resumed") is True
    assert plan_calls["rule_snapshot_id"] is not None      # 规则版本被记录进规划
    assert reused.get("analysis_artifact_path")            # 分析工件路径随记录传递


def test_saved_plan_file_shape(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("STUDIO_TASKS_ROOT", str(tmp_path / "tasks"))
    record = cp._record(plan_source="llm", llm_requested=True, llm_error=None,
                        transcript_version=2, clips=[{
                            "clip_id": "clip-x", "topic": "t", "start_segment_id": "seg_0000",
                            "end_segment_id": "seg_0001", "start_ms": 0, "end_ms": 1,
                            "duration_ms": 1, "opening_context": "", "core_points": [],
                            "closing_thought": "", "evidence_segment_ids": [],
                            "cut_in_reason": "", "cut_out_reason": "",
                            "completeness": "pass", "warnings": []}],
                        discarded=[], notes=[])
    saved = cp.save_plan_record("autoedit_plan01", record)
    data = json.loads((tmp_path / "tasks" / "plans" / "autoedit_plan01" / "clip_plan.json").read_text())
    for key in ("clip_id", "topic", "start_segment_id", "end_segment_id", "start_ms", "end_ms",
                "duration_ms", "opening_context", "core_points", "closing_thought",
                "evidence_segment_ids", "cut_in_reason", "cut_out_reason",
                "completeness", "warnings"):
        assert key in data["clips"][0]
    assert saved["size_bytes"] > 0


# ---------------------------------------------------------------------------
# API 与 worker 集成
# ---------------------------------------------------------------------------

class FakeRequest:
    def __init__(self, payload: dict):
        self._payload = payload

    async def json(self) -> dict:
        return self._payload


def test_auto_edit_formal_default_has_no_stop_after(monkeypatch):
    """正式「开始自动剪辑」不带 stop_after：分析→规划→渲染全自动（需求 1）。"""
    captured: dict = {}

    async def fake_create_job(project_id, object_key, size, config, *, job_id=None):
        captured["config"] = config
        return "job-2"

    monkeypatch.setattr(jobs_routes, "create_job", fake_create_job)
    monkeypatch.setattr(store, "load_corrected_transcript", lambda *a, **k: {"ok": True})
    asyncio.run(jobs_routes.submit_auto_edit_job(FakeRequest(
        {"object_key": "local:/a.mp4", "project_id": "timeline_t",
         "transcript_id": "tr-x", "transcript_version": 1})))
    assert "stop_after" not in captured["config"]       # 自动继续渲染，无诊断停点
    assert captured["config"]["job_kind"] == "auto_edit"


def test_auto_edit_analysis_only_maps_to_stop_after_analysis(monkeypatch):
    captured: dict = {}

    async def fake_create_job(project_id, object_key, size, config, *, job_id=None):
        captured["config"] = config
        return "job-3"

    monkeypatch.setattr(jobs_routes, "create_job", fake_create_job)
    monkeypatch.setattr(store, "load_corrected_transcript", lambda *a, **k: {"ok": True})
    asyncio.run(jobs_routes.submit_auto_edit_job(FakeRequest(
        {"object_key": "local:/a.mp4", "project_id": "timeline_t",
         "transcript_id": "tr-x", "transcript_version": 1, "config": {"analysis_only": True}})))
    assert captured["config"]["stop_after"] == "analysis"
    assert "analysis_only" not in captured["config"]


def test_worker_plan_stage_saves_and_stops_before_render(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("STUDIO_TASKS_ROOT", str(tmp_path / "tasks"))

    def _guard(name):
        def _fail(*args, **kwargs):
            raise AssertionError(f"禁止调用 {name}（第四步不渲染/不搬源片）")
        return _fail

    monkeypatch.setattr(worker, "stage_editing", _guard("stage_editing"))
    monkeypatch.setattr(worker, "stage_rendering", _guard("stage_rendering"))
    monkeypatch.setattr(worker, "_materialise_source_for_job", _guard("_materialise_source_for_job"))
    monkeypatch.setattr(worker, "_upload_auto_edit_artifacts", _guard("_upload_auto_edit_artifacts"))
    monkeypatch.setattr("src.studio_transcript_store.load_corrected_transcript",
                        lambda key, transcript_id=None, transcript_version=None: {
                            "asr_segments": _segments(), "raw_segments": [],
                            "transcript_id": "tr-x", "transcript_version": 9})
    analysis_stub = {"plan_source": "llm", "keywords": ["高血压"], "golden_quotes": [],
                     "topic_units": [], "candidate_intervals": []}
    monkeypatch.setattr(worker, "_run_content_analysis_stage",
                        lambda job, jid, transcript: analysis_stub)
    plan_calls: dict = {}

    def fake_plan(segments, analysis, **kwargs):
        plan_calls["transcript_version"] = kwargs.get("transcript_version")
        plan_calls["analysis"] = analysis
        return {"plan_source": "llm", "llm_error": None, "clips": [{
            "clip_id": "clip-x", "topic": "t", "start_segment_id": "seg_0000",
            "end_segment_id": "seg_0001", "start_ms": 0, "end_ms": 1, "duration_ms": 1,
            "opening_context": "", "core_points": [], "closing_thought": "",
            "evidence_segment_ids": [], "cut_in_reason": "", "cut_out_reason": "",
            "completeness": "pass", "warnings": []}],
            "discarded": [], "notes": []}

    monkeypatch.setattr(cp, "plan_clips", fake_plan)
    monkeypatch.setattr(cp, "save_plan_record",
                        lambda job_id, record: {"path": "/tmp/clip_plan.json", "size_bytes": 10})
    posts: list[dict] = []
    monkeypatch.setattr(worker, "_http", lambda method, url, payload=None, timeout=60: posts.append(payload) or {})
    monkeypatch.setattr("src.r2.upload_file", lambda path, key: {"object_key": key, "size": 10})
    states: list[tuple[str, str]] = []
    monkeypatch.setattr(worker, "_state",
                        lambda job_id, status, stage, progress, error=None: states.append((status, stage)))

    job = {"job_id": "autoedit_plan02", "object_key": "local:/v.mp4", "project_id": "timeline_t",
           "config": {"job_kind": "auto_edit", "stop_after": "plan"}}
    worker.run_auto_edit_only(job)

    statuses = [status for status, _ in states]
    assert statuses[0] == "planning"          # 分析阶段已打桩，第一条状态来自规划阶段
    assert statuses[-1] == "plan_ready"
    assert "downloading" not in statuses and "editing" not in statuses and "rendering" not in statuses
    assert plan_calls["transcript_version"] == 9            # 读取指定转写版本
    assert plan_calls["analysis"] is analysis_stub          # 规划输入=分析产物（内存传递）
    assert posts and posts[0]["kind"] == "clip_plan"
