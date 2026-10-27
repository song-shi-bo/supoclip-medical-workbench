"""双任务 API 契约：/api/jobs/transcribe 与 /api/jobs/auto-edit。

不落库：create_job 打桩；转写有效性走 monkeypatch 的 store 桩；
422 文案与 config 注入是前后端并行开发的冻结契约，必须逐字守护。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.api.routes import jobs as jobs_routes
from src import studio_transcript_store as store


class FakeRequest:
    def __init__(self, payload: dict):
        self._payload = payload

    async def json(self) -> dict:
        return self._payload


def test_transcribe_defaults_to_transcribe_only_and_injects_job_kind(monkeypatch):
    captured: dict = {}

    async def fake_create_job(project_id, object_key, size, config, *, job_id=None):
        captured.update(project_id=project_id, object_key=object_key, config=config)
        return "job-1"

    monkeypatch.setattr(jobs_routes, "create_job", fake_create_job)
    result = asyncio.run(jobs_routes.submit_transcribe_job(FakeRequest(
        {"object_key": "local:/a.mp4", "project_id": "timeline_t"})))
    assert result == {"job_id": "job-1", "status": "queued", "job_kind": "transcribe_only"}
    assert captured["config"]["job_kind"] == "transcribe_only"


def test_transcribe_rejects_unknown_mode():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(jobs_routes.submit_transcribe_job(FakeRequest(
            {"object_key": "local:/a.mp4", "project_id": "timeline_t", "mode": "full"})))
    assert exc.value.status_code == 422
    assert "transcribe_only" in str(exc.value.detail)


def test_auto_edit_rejects_without_valid_transcript(monkeypatch):
    monkeypatch.setattr(store, "load_corrected_transcript", lambda *a, **k: None)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(jobs_routes.submit_auto_edit_job(FakeRequest(
            {"object_key": "local:/a.mp4", "project_id": "timeline_t",
             "transcript_id": "tr-x", "transcript_version": 1})))
    assert exc.value.status_code == 422
    assert exc.value.detail == "请先完成转写与纠错。"


def test_auto_edit_creates_job_with_transcript_reference(monkeypatch):
    monkeypatch.setattr(store, "load_corrected_transcript",
                        lambda *a, **k: {"schema": store.SCHEMA, "transcript_version": 3})
    captured: dict = {}

    async def fake_create_job(project_id, object_key, size, config, *, job_id=None):
        captured.update(project_id=project_id, object_key=object_key, config=config)
        return "job-2"

    monkeypatch.setattr(jobs_routes, "create_job", fake_create_job)
    result = asyncio.run(jobs_routes.submit_auto_edit_job(FakeRequest(
        {"object_key": "local:/a.mp4", "project_id": "timeline_t",
         "transcript_id": "tr-x", "transcript_version": 3})))
    assert result == {"job_id": "job-2", "status": "queued", "job_kind": "auto_edit"}
    assert captured["config"]["job_kind"] == "auto_edit"
    assert captured["config"]["transcript_id"] == "tr-x"
    assert captured["config"]["transcript_version"] == 3


def test_internal_state_whitelist_accepts_split_statuses():
    from src.api.routes.jobs import JobStateRequest

    for status in ("extracting_audio", "transcript_ready", "retrieving_rules",
                   "extracting_highlights", "planning"):
        body = JobStateRequest(status=status)
        assert body.status == status
