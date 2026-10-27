"""Studio 已纠错转写存取：TRANSCRIBE_ONLY 与 AUTO_EDIT 两类任务的复用契约。

纯同步文件存储（禁止 import DB / async）：工作台把「转写+纠错」的产物按版本
落盘，AUTO_EDIT 任务启动时按 object_key / transcript_id / transcript_version
取回已纠错转写，绝不重跑转写与纠错。

布局（STUDIO_TASKS_ROOT 缺省 backend/tasks）::

    tasks/transcripts/<source_id>/v1.json
    - source_id = sha256(object_key).hexdigest()[:16]
    - 版本 n 从 1 递增；写入走同目录 .part 临时文件 + os.replace 原子改名，
      中断不会留下半截 JSON，也不会互相覆盖。

payload 形状（schema="studio.corrected_transcript/1"）见
:func:`save_corrected_transcript`。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent

SCHEMA = "studio.corrected_transcript/1"

_VERSION_RE = re.compile(r"^v(\d+)\.json$")


def tasks_root() -> Path:
    return Path(os.getenv("STUDIO_TASKS_ROOT", str(BASE_DIR / "tasks")))


def transcripts_root() -> Path:
    return tasks_root() / "transcripts"


def source_id_for(object_key: str) -> str:
    """object_key → 稳定 16 位目录名（同名源片永远落在同一目录）。"""
    return hashlib.sha256(str(object_key).encode("utf-8")).hexdigest()[:16]


def transcript_id_for(object_key: str) -> str:
    return f"tr-{source_id_for(object_key)}"


def _version_path(source_id: str, version: int) -> Path:
    return transcripts_root() / source_id / f"v{int(version)}.json"


def _latest_version(source_id: str) -> int | None:
    directory = transcripts_root() / source_id
    if not directory.is_dir():
        return None
    versions = [
        int(match.group(1))
        for entry in directory.iterdir()
        if (match := _VERSION_RE.match(entry.name))
    ]
    return max(versions) if versions else None


def _source_still_exists(object_key: str) -> bool:
    """加载前的源片存在性校验。

    * 本地模式（object_key 带 ``local:`` 前缀）：源文件已被清理 → False；
    * R2 模式：不做网络探测（存在性由 R2 侧保证），直接放行；
    * 校验过程抛任何异常 → 按「无法验证」放行（不因校验器故障拒绝合法转写）。
    """
    try:
        from . import r2

        if not str(object_key).startswith(r2.LOCAL_SCHEME):
            return True
        return r2.local_path_for(str(object_key)).is_file()
    except Exception:  # noqa: BLE001 — 无法验证一律放行
        return True


def _save_corrected_transcript_unlocked(
    object_key: str,
    project_id: str,
    *,
    asr_segments: list[dict[str, Any]],
    raw_segments: list[dict[str, Any]],
    correction_audit: list[dict[str, Any]],
    correction_summary: dict[str, Any],
    correction_provider: str,
    language: str,
    words: list[dict[str, Any]],
) -> dict[str, Any]:
    """落盘一个新版本并返回 ``{"transcript_id", "transcript_version"}``。

    版本号按目录里已有 ``v{n}.json`` 的最大值 +1 递增（从 1 开始）；写完才
    ``os.replace`` 原子改名，读方要么看到完整旧版、要么看到完整新版。
    """
    source_id = source_id_for(object_key)
    directory = transcripts_root() / source_id
    directory.mkdir(parents=True, exist_ok=True)
    latest = _latest_version(source_id)
    version = (latest or 0) + 1
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "transcript_id": transcript_id_for(object_key),
        "transcript_version": version,
        "source_object_key": object_key,
        "source_project_id": project_id,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "correction_provider": correction_provider,
        "correction_summary": correction_summary or {},
        "language": language,
        "words": words,
        "asr_segments": asr_segments,
        "raw_segments": raw_segments,
        "correction_audit": correction_audit,
    }
    target = _version_path(source_id, version)
    temporary = target.with_name(f"{target.name}.{uuid.uuid4().hex}.part")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return {"transcript_id": payload["transcript_id"], "transcript_version": version}


def save_corrected_transcript(
    object_key: str, project_id: str, *, asr_segments: list[dict[str, Any]],
    raw_segments: list[dict[str, Any]], correction_audit: list[dict[str, Any]],
    correction_summary: dict[str, Any], correction_provider: str,
    language: str, words: list[dict[str, Any]],
) -> dict[str, Any]:
    """所有自动纠错与人工修订共用版本锁，避免跨进程覆盖同一 vN。"""
    from .studio_runtime import process_lock
    directory = transcripts_root() / source_id_for(object_key)
    directory.mkdir(parents=True, exist_ok=True)
    with process_lock(directory / ".version.lock"):
        return _save_corrected_transcript_unlocked(object_key, project_id,
            asr_segments=asr_segments, raw_segments=raw_segments,
            correction_audit=correction_audit, correction_summary=correction_summary,
            correction_provider=correction_provider, language=language, words=words)


def load_corrected_transcript(
    object_key: str,
    *,
    transcript_id: str | None = None,
    transcript_version: int | None = None,
) -> dict[str, Any] | None:
    """读取已纠错转写；缺省取最新版本。

    以下情形一律返回 ``None``（调用方按「请先完成转写与纠错」处理）：
    没有任何版本文件 / JSON 损坏 / ``schema`` 不是当前版本 /
    ``transcript_id`` 与调用方给值或与 object_key 推导值不匹配 /
    本地模式且源片文件已缺失。
    """
    source_id = source_id_for(object_key)
    if transcript_version is not None:
        version = int(transcript_version)
    else:
        latest = _latest_version(source_id)
        if latest is None:
            return None
        version = latest
    path = _version_path(source_id, version)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    expected_transcript_id = transcript_id_for(object_key)
    if payload.get("schema") != SCHEMA:
        return None
    if payload.get("transcript_id") != expected_transcript_id:
        return None
    if transcript_id is not None and payload.get("transcript_id") != transcript_id:
        return None
    if not _source_still_exists(object_key):
        return None
    return payload


def find_latest_by_project(project_id: str) -> dict[str, Any] | None:
    """按项目 id 找最新可用的纠错转写凭据（重新剪辑入口用）。

    扫描转写存储全部版本，取 ``source_project_id`` 匹配且源文件仍存在的
    最新版本；找不到返回 ``None``。只返回凭据三元组，不带转写正文。
    """
    if not transcripts_root().is_dir():
        return None
    best: tuple[str, dict[str, Any]] | None = None
    for version_file in sorted(transcripts_root().glob("*/v*.json")):
        try:
            payload = json.loads(version_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
            continue
        if payload.get("source_project_id") != project_id:
            continue
        object_key = str(payload.get("source_object_key") or "")
        transcript_id = str(payload.get("transcript_id") or "")
        version = int(payload.get("transcript_version") or 0)
        if not object_key or not transcript_id or version < 1:
            continue
        if not _source_still_exists(object_key):
            continue
        created = str(payload.get("created_at_utc") or "")
        if best is None or created >= best[0]:
            best = (created, {
                "object_key": object_key,
                "transcript_id": transcript_id,
                "transcript_version": version,
            })
    return best[1] if best else None


def find_latest_by_object_key(object_key: str) -> dict[str, Any] | None:
    """按 object_key 取该物理源的最新可用版本凭据（同源跨项目复用）。"""
    directory = transcripts_root() / source_id_for(object_key)
    if not directory.is_dir():
        return None
    best: tuple[int, dict[str, Any]] | None = None
    for version_file in directory.glob("v*.json"):
        match = _VERSION_RE.match(version_file.name)
        if not match:
            continue
        try:
            payload = json.loads(version_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
            continue
        if not _source_still_exists(str(payload.get("source_object_key") or object_key)):
            continue
        version = int(match.group(1))
        if best is None or version > best[0]:
            best = (version, {
                "object_key": str(payload.get("source_object_key") or object_key),
                "transcript_id": str(payload.get("transcript_id") or ""),
                "transcript_version": version,
            })
    return best[1] if best else None
