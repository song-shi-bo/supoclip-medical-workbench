"""Studio worker：环境常量、BASE_DIR 与 .env 装载（阶段5·点2 拆分）。"""

from __future__ import annotations

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

BASE_DIR = Path(__file__).resolve().parents[2]  # src/studio_job_worker/config.py → backend（拆包后多一层）
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv  # noqa: E402
from src.source_store import materialise_source as _source_materialise  # noqa: E402
from src.studio_runtime import process_lock, terminate_with_parent  # noqa: E402

load_dotenv(BASE_DIR / ".env")

WORKER_ID = os.getenv("STUDIO_WORKER_ID") or f"worker-{uuid.uuid4().hex[:8]}"
# 阶段5·点3：本 worker 角色只认领的资源队列（逗号分隔；空 = 全队列）。
# 例：STUDIO_WORKER_QUEUES=asr 启动转写专用 worker，与渲染 worker 并行不互抢。
WORKER_QUEUES = tuple(q.strip() for q in os.getenv("STUDIO_WORKER_QUEUES", "").split(",") if q.strip())
WORKER_ROLE_TAG = "-" + "-".join(WORKER_QUEUES) if WORKER_QUEUES else ""

BACKEND_INTERNAL_URL = os.getenv("BACKEND_INTERNAL_URL", "http://127.0.0.1:8000/api").rstrip("/")
WORKER_SHARED_SECRET = os.getenv("WORKER_SHARED_SECRET", "")
POLL_SECONDS = float(os.getenv("WORKER_POLL_SECONDS", "5"))
JOB_TIMEOUT_SECONDS = float(os.getenv("WORKER_JOB_TIMEOUT_SECONDS", "3600"))
VIDEO_BRAIN_URL = os.getenv("VIDEO_BRAIN_URL", "http://127.0.0.1:8940").rstrip("/")
VIDEO_BRAIN_HOME = Path(os.getenv("VIDEO_BRAIN_HOME", "/domain/video-brain"))
STUDIO_TASKS_ROOT = Path(os.getenv("STUDIO_TASKS_ROOT", str(BASE_DIR / "tasks")))


def _base_dir() -> Path:
    """BASE_DIR 的调用时解析：兼容测试对**包命名空间**的 monkeypatch（阶段5·点2）。"""
    import sys
    pkg_name = __name__.rsplit(".", 1)[0]
    return getattr(sys.modules[pkg_name], "BASE_DIR", BASE_DIR)
