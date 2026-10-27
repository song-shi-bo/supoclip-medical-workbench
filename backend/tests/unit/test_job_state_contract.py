"""`/internal/jobs/{job_id}/state` 状态契约测试。

回归 2026-10-16 P0：worker 新发射状态 `uploading_reedit_artifacts`
（studio_job_worker.py「重新剪辑并替换旧切片」Step 3 发布契约）不在
state 端点白名单 → 422「未知任务状态」，reedit 进度上报全挂。

两层防护：
- 行为契约：合法状态 200、租约失效 409、非法状态 422（先于租约校验）、
  执行器凭据错误 401；
- 集合差异回归：扫描 studio_job_worker.py 里 `_state(` 的字面状态 +
  已知动态终态，断言整体 ⊆ jobs_routes.ALLOWED_JOB_STATUSES，
  防未来 worker 新增状态再漏配。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import jobs as jobs_routes

# worker 里不经 _state() 字面量发射、由返回值/异常路径动态决定的终态
DYNAMIC_TERMINAL_STATUSES = {
    "transcript_ready",
    "completed",
    "completed_with_warnings",
    "partial_succeeded",
    "technical_failed",
    "completed_with_cleanup_warning",
    "failed",
}

WORKER_SECRET = "test-worker-secret"
STATE_URL = "/api/internal/jobs/{job_id}/state"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """最小 app 只挂 jobs router；配好执行器共享密钥。"""
    monkeypatch.setattr(jobs_routes, "_WORKER_SECRET", WORKER_SECRET)
    app = FastAPI()
    app.include_router(jobs_routes.router)
    return TestClient(app)


def _post_state(client: TestClient, status: str, secret: str = WORKER_SECRET):
    return client.post(
        STATE_URL.format(job_id="192a7b28a47c41059fb147651593d480"),
        json={"status": status, "stage": "st", "progress": 0.8},
        headers={"X-Worker-Secret": secret, "X-Worker-Id": "worker-test"},
    )


# ---------------------------------------------------------------------------
# a) 集合差异回归：worker 发射的状态不允许再漏配
# ---------------------------------------------------------------------------

def test_worker_emitted_statuses_subset_of_whitelist():
    source = (Path(__file__).resolve().parents[2] / "src" / "studio_job_worker.py").read_text(
        encoding="utf-8")
    emitted = set(re.findall(r'_state\([^,]+,\s*"([a-z_]+)"', source))
    assert emitted, "扫描不到任何 _state(...) 字面状态——正则或源码路径失效"
    known = emitted | DYNAMIC_TERMINAL_STATUSES
    missing = known - jobs_routes.ALLOWED_JOB_STATUSES
    assert not missing, f"worker 会发射但 state 端点白名单缺失的状态: {sorted(missing)}"


# ---------------------------------------------------------------------------
# b) 契约行为：新状态可上报
# ---------------------------------------------------------------------------

def test_uploading_reedit_artifacts_accepted(monkeypatch: pytest.MonkeyPatch, client: TestClient):
    seen: dict = {}

    async def fake_update_progress(job_id, status, stage, progress, error, worker_id="", lease_token="", metadata=None):
        seen.update(job_id=job_id, status=status, worker_id=worker_id)
        return True

    monkeypatch.setattr(jobs_routes, "update_progress", fake_update_progress)
    resp = _post_state(client, "uploading_reedit_artifacts")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True}
    assert seen["status"] == "uploading_reedit_artifacts"
    assert seen["worker_id"] == "worker-test"


def test_all_whitelisted_statuses_accepted(monkeypatch: pytest.MonkeyPatch, client: TestClient):
    """白名单内任意状态都应通过校验（逐一打一遍端点，防白名单与端点脱钩）。"""

    async def fake_update_progress(*args, **kwargs):
        return True

    monkeypatch.setattr(jobs_routes, "update_progress", fake_update_progress)
    for status in sorted(jobs_routes.ALLOWED_JOB_STATUSES):
        resp = _post_state(client, status)
        assert resp.status_code == 200, f"status={status!r} 被拒: {resp.text}"


# ---------------------------------------------------------------------------
# c) 校验仍在：409 / 422 先于租约校验 / 401
# ---------------------------------------------------------------------------

def test_stale_lease_returns_409(monkeypatch: pytest.MonkeyPatch, client: TestClient):
    async def fake_update_progress(*args, **kwargs):
        return False

    monkeypatch.setattr(jobs_routes, "update_progress", fake_update_progress)
    resp = _post_state(client, "uploading_reedit_artifacts")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "任务租约失效或不属于当前执行器"


def test_unknown_status_422_precedes_lease_check(monkeypatch: pytest.MonkeyPatch,
                                                 client: TestClient):
    async def explode(*args, **kwargs):
        raise AssertionError("非法状态必须在校验层拒绝，不得触达租约校验")

    monkeypatch.setattr(jobs_routes, "update_progress", explode)
    resp = _post_state(client, "definitely_not_a_state")
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == "未知任务状态"


def test_bad_worker_secret_rejected(client: TestClient):
    resp = _post_state(client, "uploading_reedit_artifacts", secret="wrong-secret")
    assert resp.status_code == 401, resp.text


def test_missing_worker_secret_configuration(monkeypatch: pytest.MonkeyPatch):
    """服务端未配置 WORKER_SHARED_SECRET → 503（执行器通道关闭）。"""
    monkeypatch.setattr(jobs_routes, "_WORKER_SECRET", "")
    app = FastAPI()
    app.include_router(jobs_routes.router)
    client = TestClient(app)
    resp = client.post(STATE_URL.format(job_id="j"), json={"status": "uploading"})
    assert resp.status_code == 503, resp.text
