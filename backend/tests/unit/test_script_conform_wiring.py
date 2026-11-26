"""对稿成片接线冒烟测试（2026-11-25，防真实跑视频时出 BUG）。

* route 层：POST /api/jobs/auto-edit 携带 config.script_mode → 透传进任务 config；
* 链路层：run_auto_edit_only 在 script_mode 时调用匹配阶段（诊断分支 + 正式全路径），
  非对稿模式绝不调用（老工作流零变化）。

全部打桩：不连库、不落真实 tasks/、不调模型。
"""

from types import SimpleNamespace

import pytest

from src.api.routes import jobs as jobs_routes
from src.studio_job_worker import runner


class _FakeRequest:
    def __init__(self, payload: dict):
        self._payload = payload

    async def json(self) -> dict:
        return self._payload


@pytest.mark.asyncio
async def test_auto_edit_route_passes_script_mode_into_job_config(monkeypatch):
    captured: dict = {}

    async def _capture_create(project_id, object_key, size, config, job_id=None):
        captured.update({"project_id": project_id, "object_key": object_key, "config": config,
                         "job_id": job_id})
        return "job-test-auto-1"

    def _fake_load(*args, **kwargs):
        return {"asr_segments": [], "transcript_id": "t1", "transcript_version": 1}

    monkeypatch.setattr(jobs_routes, "_create_public_job", _capture_create)
    monkeypatch.setattr("src.studio_transcript_store.load_corrected_transcript", _fake_load)

    response = await jobs_routes.submit_auto_edit_job(_FakeRequest({
        "object_key": "local:/tmp/x.mp4", "project_id": "p1",
        "transcript_id": "t1", "transcript_version": 1,
        "config": {"script_mode": True},
    }))

    assert response["job_id"] == "job-test-auto-1"
    assert response["job_kind"] == "auto_edit"
    assert captured["config"]["script_mode"] is True
    assert captured["config"]["job_kind"] == "auto_edit"


@pytest.mark.asyncio
async def test_auto_edit_route_defaults_script_mode_absent(monkeypatch):
    """不传 script_mode → config 无该键（老工作流零变化）。"""
    captured: dict = {}

    async def _capture_create(project_id, object_key, size, config, job_id=None):
        captured["config"] = config
        return "job-test-auto-2"

    def _fake_load(*args, **kwargs):
        return {"asr_segments": []}

    monkeypatch.setattr(jobs_routes, "_create_public_job", _capture_create)
    monkeypatch.setattr("src.studio_transcript_store.load_corrected_transcript", _fake_load)

    await jobs_routes.submit_auto_edit_job(_FakeRequest({
        "object_key": "local:/tmp/y.mp4", "project_id": "p1",
        "transcript_id": "t1", "transcript_version": 1,
    }))
    assert "script_mode" not in captured["config"]


def _patch_chain(monkeypatch, calls: dict):
    """把 run_auto_edit_only 的全部重阶段打桩，隔离出「hook 是否触发」这一件事。"""
    def _fake_load(*args, **kwargs):
        return {"asr_segments": [{"segment_id": 0, "start_ms": 0, "end_ms": 1000,
                                  "corrected_text": "测试句"}]}
    monkeypatch.setattr("src.studio_transcript_store.load_corrected_transcript", _fake_load)

    def _match(job, job_id, transcript):
        calls["match"] = calls.get("match", 0) + 1
        calls["match_transcript"] = transcript
        return {"script_linked": True}
    monkeypatch.setattr(runner, "run_script_match_stage", _match)
    monkeypatch.setattr(runner, "_knowledge_context_for", lambda job, job_id, transcript: {})
    monkeypatch.setattr(runner, "_analysis_cache", lambda job, job_id, transcript, kc: None)
    monkeypatch.setattr(runner, "_run_content_analysis_stage", lambda job, job_id, transcript: {})
    monkeypatch.setattr(runner, "_plan_cache", lambda job, job_id, transcript, analysis: None)
    monkeypatch.setattr(runner, "_run_clip_planning_stage",
                        lambda job, job_id, transcript, analysis: calls.setdefault("plan", 1) and {"slices": []})
    monkeypatch.setattr(runner, "_run_clip_refinement_stage", lambda job, job_id, transcript, plan: None)
    monkeypatch.setattr(runner, "_run_clip_render_stage",
                        lambda job, job_id, transcript, plan: calls.setdefault("render", 1))
    monkeypatch.setattr(runner, "_state", lambda *a, **k: calls.setdefault("states", []).append(a[1]))
    monkeypatch.setattr(runner, "_log", lambda *a, **k: None)


@pytest.mark.asyncio
async def test_auto_edit_chain_fires_match_when_script_mode(monkeypatch):
    calls: dict = {}
    _patch_chain(monkeypatch, calls)
    job = {"job_id": "job-chain-1", "project_id": "p1", "object_key": "local:/tmp/z.mp4",
           "config": {"script_mode": True}}
    runner.run_auto_edit_only(job)
    assert calls.get("match") == 1
    assert job["_script_match"] == {"script_linked": True}
    # 链路继续走完：规划与渲染阶段都被调用（匹配只前置，不中断）
    assert calls.get("plan") == 1 and calls.get("render") == 1  # 匹配只前置，链路走到渲染


@pytest.mark.asyncio
async def test_auto_edit_chain_skips_match_without_script_mode(monkeypatch):
    calls: dict = {}
    _patch_chain(monkeypatch, calls)

    def _fail(job, job_id, transcript):
        raise AssertionError("非对稿模式不应触发匹配阶段")
    monkeypatch.setattr(runner, "run_script_match_stage", _fail)

    job = {"job_id": "job-chain-2", "project_id": "p1", "object_key": "local:/tmp/w.mp4",
           "config": {}}
    runner.run_auto_edit_only(job)  # 老工作流：全程无匹配、正常走完
    assert "_script_match" not in job
