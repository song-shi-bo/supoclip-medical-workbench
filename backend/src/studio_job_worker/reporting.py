"""Studio worker：HTTP 上报与进度状态（_log/_http/_current_job()/_state）。"""

import asyncio
import inspect
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from dotenv import load_dotenv  # noqa: E402
from src.source_store import materialise_source as _source_materialise  # noqa: E402
from src.studio_runtime import process_lock, terminate_with_parent  # noqa: E402


from .config import (  # noqa: F401
    _base_dir,
    BACKEND_INTERNAL_URL,
    JOB_TIMEOUT_SECONDS,
    POLL_SECONDS,
    STUDIO_TASKS_ROOT,
    VIDEO_BRAIN_HOME,
    VIDEO_BRAIN_URL,
    WORKER_ID,
    WORKER_QUEUES,
    WORKER_ROLE_TAG,
    WORKER_SHARED_SECRET,
)


def _log(message: str) -> None:
    print(f"[studio-worker {WORKER_ID} {time.strftime('%H:%M:%S')}] {message}", flush=True)


def _http(method: str, url: str, payload: dict | None = None, timeout: float = 60) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json", "X-Worker-Id": WORKER_ID,
                 "X-Worker-Secret": WORKER_SHARED_SECRET,
                 "X-Worker-Lease": str(_current_job().get("lease_token") or "")},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode() or "{}")

# ---------------------------------------------------------------------------
# 状态上报
# ---------------------------------------------------------------------------

_CURRENT_JOB = {"id": None, "lease_token": None, "run_id": None, "metadata": {}}


def _current_job_id() -> str:
    return _current_job()["id"] or ""


def _state(job_id: str, status: str, stage: str, progress: float, error: str | None = None) -> None:
    try:
        _http("POST", f"{BACKEND_INTERNAL_URL}/internal/jobs/{job_id}/state",
              {"status": status, "stage": stage, "progress": progress, "error": error,
               **(_current_job().get("metadata") or {})})
    except Exception as exc:
        raise RuntimeError(f"无法提交任务状态: {type(exc).__name__}") from exc



def _current_job() -> dict:
    """_current_job() 的调用时解析：兼容测试对**包命名空间**的整体替换（阶段5·点2）。"""
    import sys
    pkg_name = __name__.rsplit(".", 1)[0]
    return getattr(sys.modules[pkg_name], "_CURRENT_JOB", _CURRENT_JOB)
