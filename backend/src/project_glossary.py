"""项目术语表：ASR 热词 + LLM 纠错参照（2026-10-28，用户需求的术语定制机制）。

存储：``{STUDIO_TASKS_ROOT}/projects/<project_id>/glossary.json``（随项目台账同目录）；
全局词表（可选）：``{STUDIO_TASKS_ROOT}/glossary.json``，与项目词表合并去重后使用。

格式：``{"terms": ["词1", "词2", …], "updated_at": "ISO8601"}``。
安全边界：词数与词长有上限；任何故障不抛出到投递/转写主链路（返回空表）。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

MAX_TERMS = 300
MAX_TERM_LEN = 60


def _tasks_root() -> Path:
    from .studio_project_store import TASKS_ROOT

    return Path(TASKS_ROOT)


def _safe_project_id(project_id: str) -> str:
    pid = str(project_id or "").strip()
    if not pid or "/" in pid or "\\" in pid or ".." in pid or pid.startswith("."):
        raise ValueError("invalid project_id")
    return pid


def glossary_path(project_id: str) -> Path:
    return _tasks_root() / "projects" / _safe_project_id(project_id) / "glossary.json"


def global_glossary_path() -> Path:
    return _tasks_root() / "glossary.json"


def _read_terms(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    raw = data.get("terms") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]


def load_glossary(project_id: str) -> list[str]:
    """项目词表 + 全局词表合并去重（保持顺序，项目词表优先）。任何故障返回空表。"""
    try:
        terms = _read_terms(glossary_path(project_id))
        seen = set(terms)
        for t in _read_terms(global_glossary_path()):
            if t not in seen:
                terms.append(t)
                seen.add(t)
        return terms[:MAX_TERMS]
    except Exception:  # noqa: BLE001 — 词表故障绝不影响主链路
        return []


def save_glossary(project_id: str, terms: list[str]) -> list[str]:
    """保存项目词表（校验+去重+截断），返回规范化后的词表。"""
    pid = _safe_project_id(project_id)
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in terms or []:
        t = str(raw).strip()
        if not t or len(t) > MAX_TERM_LEN or t in seen:
            continue
        seen.add(t)
        cleaned.append(t)
        if len(cleaned) >= MAX_TERMS:
            break
    path = glossary_path(pid)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"terms": cleaned,
               "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return cleaned


def hotwords_string(project_id: str, *, max_chars: int = 800) -> Optional[str]:
    """术语表 → faster-whisper ``hotwords`` 参数（逗号分隔）；无词表返回 None。"""
    terms = load_glossary(project_id)
    if not terms:
        return None
    out: list[str] = []
    used = 0
    for t in terms:
        if used + len(t) + 1 > max_chars:
            break
        out.append(t)
        used += len(t) + 1
    return "，".join(out) if out else None
