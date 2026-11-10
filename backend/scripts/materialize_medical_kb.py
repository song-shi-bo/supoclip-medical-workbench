#!/usr/bin/env python3
"""物化医学指南库快照（闸门消费）+ 可选入库 PostgreSQL。

用法（backend 目录下）::

    python scripts/materialize_medical_kb.py                 # 校验 + 写 active_snapshot.json
    python scripts/materialize_medical_kb.py --db            # 同时入库 medical_guidelines 表

快照契约（见 src/medical_kb.py）：
- 闸门只读 ``medical_kb_seed/active_snapshot.json``（确定性本地文件，规划阶段不联网、不连库）；
- ``snapshot_sha256`` 是条目集的稳定指纹，进 plan 指纹——库变了，旧计划缓存自动失效；
- 修改种子条目后必须重跑本脚本，否则闸门感知不到变化。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = BACKEND_ROOT / "medical_kb_seed"
ACTIVE_SNAPSHOT = SEED_DIR / "active_snapshot.json"

ALLOWED_STATUS = {"approved", "disabled"}
ALLOWED_REVIEW = {"pending_review", "reviewed"}
REQUIRED_FIELDS = ("id", "entry_type", "statement", "source_name", "source_url")


def _canonical(entries: list[dict]) -> str:
    return json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def load_seed(path: Path) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    entries = raw.get("entries")
    if not isinstance(entries, list) or not entries:
        raise SystemExit(f"{path}: entries 为空或缺失")
    ids: set[str] = set()
    for entry in entries:
        for field in REQUIRED_FIELDS:
            if not str(entry.get(field) or "").strip():
                raise SystemExit(f"{path}: 条目缺必填字段 {field}: {entry.get('id')}")
        if entry["id"] in ids:
            raise SystemExit(f"{path}: 条目 id 重复: {entry['id']}")
        ids.add(entry["id"])
        if entry.get("status", "approved") not in ALLOWED_STATUS:
            raise SystemExit(f"{path}: 非法 status: {entry.get('status')} ({entry['id']})")
        if entry.get("review_status", "pending_review") not in ALLOWED_REVIEW:
            raise SystemExit(f"{path}: 非法 review_status: {entry.get('review_status')} ({entry['id']})")
    return raw


def materialize(seed_paths: list[Path]) -> dict:
    entries: list[dict] = []
    sources: list[str] = []
    seed_files: list[str] = []
    for path in seed_paths:
        raw = load_seed(path)
        meta_sources = raw.get("meta", {}).get("sources") or []
        sources.extend(url for url in meta_sources if url not in sources)
        seed_files.append(path.name)
        existing_ids = {e["id"] for e in entries}
        for entry in raw["entries"]:
            if entry["id"] in existing_ids:
                raise SystemExit(f"跨文件条目 id 重复: {entry['id']}")
            entries.append(entry)
    entries.sort(key=lambda e: e["id"])
    snapshot = {
        "meta": {
            "snapshot_sha256": hashlib.sha256(_canonical(entries).encode()).hexdigest(),
            "entry_count": len(entries),
            "seed_files": seed_files,
            "sources": sources,
            "materialized_note": "由 scripts/materialize_medical_kb.py 生成，勿手改；改种子后重跑。",
        },
        "entries": entries,
    }
    ACTIVE_SNAPSHOT.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return snapshot


def seed_db(snapshot: dict) -> int:
    """条目入库 medical_guidelines（content_hash 幂等，embedding 缺失静默降级）。"""
    try:
        sys.path.insert(0, str(BACKEND_ROOT))
        from src.database import get_engine  # noqa: PLC0415
        from sqlalchemy import text
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"--db 需要 backend 依赖（sqlalchemy/asyncpg）：{exc}")

    entries = snapshot["entries"]

    def content_hash(entry: dict) -> str:
        norm = json.dumps(
            {k: entry.get(k) for k in ("id", "statement", "source_url", "status",
                                       "review_status", "risk_boundary", "prerequisites")},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(norm.encode()).hexdigest()

    insert = text("""
        INSERT INTO medical_guidelines (id, topic_path, entry_type, statement, prerequisites,
                                        applicability, risk_boundary, audience_note, keywords,
                                        source_name, source_url, evidence_level, lang,
                                        status, review_status, content_hash)
        VALUES (:id, CAST(:topic_path AS text[]), :entry_type, :statement, :prerequisites,
                :applicability, :risk_boundary, :audience_note, CAST(:keywords AS text[]),
                :source_name, :source_url, :evidence_level, :lang,
                :status, :review_status, :content_hash)
        ON CONFLICT (id) DO NOTHING
    """)

    async def _run() -> int:
        engine = get_engine()
        inserted = 0
        async with engine.begin() as conn:
            for entry in entries:
                row = await conn.execute(insert, {
                    "id": entry["id"],
                    "topic_path": [str(p) for p in (entry.get("topic_path") or [])],
                    "entry_type": entry["entry_type"],
                    "statement": entry["statement"],
                    "prerequisites": str(entry.get("prerequisites") or ""),
                    "applicability": str(entry.get("applicability") or ""),
                    "risk_boundary": str(entry.get("risk_boundary") or ""),
                    "audience_note": str(entry.get("audience_note") or ""),
                    "keywords": [str(k) for k in (entry.get("keywords") or [])],
                    "source_name": entry["source_name"][:200],
                    "source_url": entry.get("source_url") or "",
                    "evidence_level": str(entry.get("evidence_level") or "")[:60],
                    "lang": entry.get("lang") or "zh",
                    "status": entry.get("status") or "approved",
                    "review_status": entry.get("review_status") or "pending_review",
                    "content_hash": content_hash(entry),
                })
                inserted += row.rowcount
        await engine.dispose()
        return inserted

    return asyncio.run(_run())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("seeds", nargs="*", default=[],
                        help="种子 JSON；缺省用 medical_kb_seed 下全部 20*-*seed.json")
    parser.add_argument("--db", action="store_true", help="同时写入 medical_guidelines 表")
    args = parser.parse_args()

    seed_paths = [Path(p) for p in args.seeds] or sorted(
        p for p in SEED_DIR.glob("*.json") if p.name != ACTIVE_SNAPSHOT.name)
    if not seed_paths:
        raise SystemExit(f"没有找到种子文件：{SEED_DIR}")
    snapshot = materialize(seed_paths)
    print(f"snapshot: {ACTIVE_SNAPSHOT}")
    print(f"entries:  {snapshot['meta']['entry_count']}")
    print(f"sha256:   {snapshot['meta']['snapshot_sha256']}")
    if args.db:
        inserted = seed_db(snapshot)
        print(f"db_inserted: {inserted}")


if __name__ == "__main__":
    main()
