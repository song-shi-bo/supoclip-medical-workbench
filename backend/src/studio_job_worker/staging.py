"""Studio worker：源片落地、guarded 上传、上传工具与恢复文件工具。"""

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
from .reporting import (  # noqa: F401
    _current_job,
    _current_job_id,
    _http,
    _log,
    _state,
)


# ---------------------------------------------------------------------------
# 各阶段实现
# ---------------------------------------------------------------------------

def materialise_source(canonical, target):
    from src.capacity_guard import reserve_capacity
    canonical, target = Path(canonical), Path(target)
    if target.exists():
        return target
    # 先试零副本硬链接，确实需要复制时再登记完整新增峰值。
    with reserve_capacity("source_link", 64 * 1024, path=target.parent):
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(canonical, target)
            return target
        except FileExistsError:
            return target
        except OSError:
            pass
    with reserve_capacity("source_copy", canonical.stat().st_size + 64 * 1024, path=target.parent):
        return _source_materialise(canonical, target)


def _guarded_upload_file(path, object_key):
    from src import r2
    from src.capacity_guard import reserve_capacity, estimate_publish_peak
    with reserve_capacity("publish", estimate_publish_peak([path]), path=r2.local_path_for(object_key)):
        return r2.upload_file(path, object_key)


def _local_object_path(object_key: str) -> Path | None:
    """本地对象模式下，object_key 指向本机一个已存在的文件时返回该路径。

    ``local:`` 前缀是对象存储的本地回退驱动（未配置 R2），此时「下载」本质
    只是同盘搬运——历史实现走 ``copy_file``，于是同一份 3GB 源片在
    ``studio-uploads/`` 之外又多出一份完整物理副本。返回真实路径后由
    :func:`src.source_store.materialise_source` 走硬链接，零额外占用。

    R2 模式（真·网络下载）和本地对象文件缺失时返回 ``None``，调用方退回
    既有下载路径，保持失败语义不变。
    """

    from src import r2

    if not object_key.startswith(r2.LOCAL_SCHEME):
        return None
    candidate = r2.local_path_for(object_key)
    return candidate if candidate.is_file() else None


def stage_downloading(object_key: str, job_id: str) -> Path:
    from src import r2

    _state(job_id, "downloading", "下载源视频", 0.05)
    target = Path(os.getenv("TEMP_DIR", str(_base_dir() / "temp"))) / "studio-jobs" / job_id / "source.mp4"
    canonical = _local_object_path(object_key)
    if canonical is not None:
        # 本地对象就是源片本身：硬链接进 job 目录，链路仍只有一份物理数据。
        materialise_source(canonical, target)
    else:
        # R2 模式必须真正落盘一份——这是整条链路里唯一必要的源片写入。
        r2.download_to_file(object_key, target)
    return target


def _job_owner_subject(project_id: str) -> str:
    """ACL 只读查询项目归属 subject（云端转写上传音频时的溯源标识）。"""
    try:
        import sqlite3
        acl = os.environ.get("STUDIO_ACCESS_ACL", "/domain/.studio-resources/access/access.sqlite3")
        c = sqlite3.connect(f"file:{acl}?mode=ro", uri=True)
        try:
            row = c.execute("SELECT owner FROM objects WHERE project_id=? LIMIT 1", (project_id,)).fetchone()
            return str(row[0]) if row and row[0] else "unknown"
        finally:
            c.close()
    except Exception:  # noqa: BLE001 — 溯源标识失败不影响转写
        return "unknown"


def _sha256_file(path: Path) -> str:
    """流式计算文件 SHA-256（发布登记用；1MB 分块，长视频安全）。"""
    import hashlib

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()




def _transcribe_recovery(job,source):
    """转写阶段完成点持久保存，回传失败恢复不再执行已成功ASR/纠错。"""
    import hashlib
    config={k:v for k,v in (job.get("config") or {}).items() if not k.startswith("_")}
    stat=source.stat()
    identity={"object_key":job["object_key"],"source":[stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns],
        "config":config,"worker_sha256":_sha256_file(Path(__file__))}
    path=Path(os.getenv("STUDIO_TASKS_ROOT",str(_base_dir()/"tasks")))/"job-recovery"/job["job_id"]/"transcribe.json"
    data={}
    if path.is_file():
        try: data=json.loads(path.read_text(encoding="utf-8"))
        except (ValueError,OSError): data={}
    if data.get("identity")!=identity: data={}
    return path,data,identity


def _save_recovery(path,data,identity):
    import uuid
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+"."+uuid.uuid4().hex+".tmp")
    try:
        temporary.write_text(json.dumps({**data,"identity":identity},ensure_ascii=False),encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)
