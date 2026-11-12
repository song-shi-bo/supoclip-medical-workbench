"""钩子模式库自动刷新（2026-11-12，用户接受全自动模式）。

触发：文稿库（scripts）入库提交成功后，后台线程调用
:func:`refresh_if_new_material`——绝不阻塞入库主流程。

行为：
1. 检测新增转写条目（按 created_at 水位，状态存 hook_patterns/.sync_state.json）；
2. 确定性计数更新：新转写开头按现有模式结构打分，material_count 累积（证据权重）；
3. 新结构提议：新增条目累计 ≥ PROPOSAL_THRESHOLD 时，发起一次小额 LLM 批量分析
   （走 llm_budget 记账），提议无法归入现有模式的新开头结构；
4. 合并与版本：任何变更 → 模式库版本号 +1 → 旧版本 JSON 存档 hook_patterns/archive/
   → 库文件 sha256 变化 → 旧计划缓存自动失效。

纪律（与库文件 discipline 同源）：
- 提炼提示词硬约束「输出中禁止出现素材原句」，模式条目只有结构描述 + 来源引用；
- 只保留医学患教适用的结构；模式总数超上限（MAX_PATTERNS）只更新计数不增条目；
- LLM 分析失败 → 计数更新仍然生效、提议跳过（如实留痕），绝不阻塞入库。
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any

from . import hook_patterns, llm_budget
from .hook_patterns import HOOK_PATTERNS_VERSION_KEY

STATE_FILENAME = ".sync_state.json"
ARCHIVE_DIRNAME = "archive"
MAX_PATTERNS = int(__import__("os").getenv("HOOK_PATTERNS_MAX", "15"))
PROPOSAL_THRESHOLD = int(__import__("os").getenv("HOOK_PATTERNS_PROPOSAL_THRESHOLD", "5"))


def _library_path() -> Path:
    return hook_patterns.default_library_path()


def _state_path() -> Path:
    return _library_path().parent / STATE_FILENAME


def _load_state() -> dict[str, Any]:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict[str, Any]) -> None:
    try:
        _state_path().write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _load_raw_library() -> dict[str, Any] | None:
    path = _library_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save_library(raw: dict[str, Any]) -> None:
    """版本 +1 + 旧版存档 + 原子写库文件（加载器按 mtime 自动重载）。"""
    import uuid
    path = _library_path()
    old_version = str(raw.get(HOOK_PATTERNS_VERSION_KEY) or "hook-patterns/v0")
    archive_dir = path.parent / ARCHIVE_DIRNAME
    archive_dir.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        (archive_dir / f"{old_version.replace('/', '_')}.json").write_text(
            path.read_text(encoding="utf-8"), encoding="utf-8")
    try:
        n = int(old_version.rsplit("v", 1)[-1])
        raw[HOOK_PATTERNS_VERSION_KEY] = f"hook-patterns/v{n + 1}"
    except ValueError:
        raw[HOOK_PATTERNS_VERSION_KEY] = "hook-patterns/v1"
    raw.setdefault("changelog", []).append({
        "version": raw[HOOK_PATTERNS_VERSION_KEY],
        "date": time.strftime("%Y-%m-%d"),
        "note": "自动刷新：素材计数更新/新模式提议合并",
    })
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.part")
    temporary.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    import os
    os.replace(temporary, path)


def _new_scripts_since(since: str | None) -> list[dict[str, Any]]:
    """查询水位之后新增的转写条目（id/title/body/created_at）。"""
    from .database import get_engine
    from sqlalchemy import text

    async def _run() -> list[dict[str, Any]]:
        engine = get_engine()
        try:
            async with engine.connect() as conn:
                if since:
                    rows = (await conn.execute(text(
                        "SELECT id, title, body, created_at FROM scripts "
                        "WHERE created_at > :since ORDER BY created_at"),
                        {"since": since})).mappings().all()
                else:
                    rows = (await conn.execute(text(
                        "SELECT id, title, body, created_at FROM scripts "
                        "ORDER BY created_at"))).mappings().all()
                return [dict(r) for r in rows]
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def _merge_counts(raw: dict[str, Any], new_openings: list[str]) -> int:
    """确定性计数更新：新开头按现有模式打分，material_count 累积。返回更新数。"""
    patterns = raw.get("patterns") or []
    by_id = {p.get("id"): p for p in patterns if isinstance(p, dict)}
    touched = 0
    for opening in new_openings:
        for pid, score in hook_patterns.score_sentence(opening):
            pattern = by_id.get(pid)
            if pattern and score >= 0.5:
                pattern["material_count"] = int(pattern.get("material_count") or 0) + 1
                touched += 1
    return touched


def _llm_propose(new_openings: list[str], existing: list[dict[str, Any]]
                 ) -> list[dict[str, Any]]:
    """小额 LLM 批量提议：无法归入现有模式的新开头结构（结构描述，禁抄原句）。"""
    from pydantic_ai import Agent
    from pydantic import BaseModel, Field

    from .ai import _build_transcript_model, apply_settings_to_process_env
    from .config import get_config

    class ProposalItem(BaseModel):
        structure: str = Field(min_length=6, max_length=60,
                               description="新开头结构的描述（不得包含素材原句）")
        mechanisms: list[str] = Field(default_factory=list, max_length=2)

    class ProposalOut(BaseModel):
        proposals: list[ProposalItem] = Field(default_factory=list, max_length=5)

    known = "；".join(f"{p.get('id')} {p.get('name')}：{p.get('structure')}"
                     for p in existing)
    samples = "\n".join(f"- {o[:120]}" for o in new_openings[:12])
    system = (
        "你是短视频开头结构分析师。输入是若干医学视频的开头口播样本，以及已有的开头"
        "模式清单。任务：判断样本中是否存在【无法归入已有模式】的新开头结构；"
        "若有，输出新结构提议（structure 为结构手法的描述，40字以内，"
        "禁止出现任何样本原句、禁止出现具体病例/疾病/数字事实）。"
        "若样本全部可归入已有模式，输出空 proposals。")
    user = f"【已有模式】\n{known}\n\n【新样本开头】\n{samples}"

    config = get_config()
    apply_settings_to_process_env(config.as_runtime_settings())
    agent: Agent[None, ProposalOut] = Agent(
        _build_transcript_model(config), output_type=ProposalOut,
        system_prompt=system)
    from . import llm_budget as _budget_guard  # noqa: F401 保留命名空间提示
    result = agent.run_sync(user)
    return [p.model_dump() for p in result.output.proposals]


def _merge_proposals(raw: dict[str, Any], proposals: list[dict[str, Any]],
                     source_note: str) -> int:
    patterns = raw.get("patterns") or []
    existing_structures = {_norm_key(str(p.get("structure") or "")) for p in patterns
                           if isinstance(p, dict)}
    max_id = 0
    for p in patterns:
        pid = str(p.get("id") or "")
        if pid.startswith("P") and pid[1:].isdigit():
            max_id = max(max_id, int(pid[1:]))
    added = 0
    for proposal in proposals:
        structure = str(proposal.get("structure") or "").strip()
        if not structure:
            continue
        if _norm_key(structure) in existing_structures:
            continue
        if len(patterns) >= MAX_PATTERNS:
            break
        max_id += 1
        mechanisms = [str(m) for m in (proposal.get("mechanisms") or [])][:2]
        patterns.append({
            "id": f"P{max_id}", "name": f"自动提议{max_id}",
            "structure": structure, "mechanisms": mechanisms,
            "material_count": 1, "reference_hint": "",
            "status": "active",
            "provenance": source_note,
        })
        existing_structures.add(_norm_key(structure))
        added += 1
    return added


def _norm_key(text: str) -> str:
    import re
    return re.sub(r"[，。！？；：、,.!?;:\s（）()\"\"''「」]", "", str(text or ""))


def refresh_if_new_material(force: bool = False) -> dict[str, Any]:
    """素材驱动刷新入口：有新转写才更新；LLM 失败只跳过提议；绝不抛出。

    返回摘要：{updated, mode, new_entries, counts_touched, proposals_added,
    new_version, notes}。
    """
    started = time.time()
    summary: dict[str, Any] = {"updated": False, "mode": "noop", "new_entries": 0,
                               "counts_touched": 0, "proposals_added": 0,
                               "new_version": "", "notes": []}
    try:
        state = _load_state()
        since = state.get("last_scripts_created_at")
        new_rows = _new_scripts_since(since)
        if not force and not new_rows:
            summary["mode"] = "no_new_material"
            return summary
        raw = _load_raw_library()
        if raw is None:
            summary["mode"] = "disabled"
            summary["notes"].append("模式库文件缺失，刷新跳过")
            return summary
        new_openings = [str(r.get("body") or "")[:200].strip() for r in new_rows
                        if str(r.get("body") or "").strip()]
        if not new_openings:
            summary["mode"] = "noop"
            summary["notes"].append("新增条目无可分析正文")
            return summary
        counts_touched = _merge_counts(raw, new_openings)
        proposals_added = 0
        if len(new_openings) >= PROPOSAL_THRESHOLD:
            try:
                proposals = _llm_propose(new_openings, raw.get("patterns") or [])
                source_note = f"auto:{new_rows[0].get('created_at')}"
                proposals_added = _merge_proposals(raw, proposals, source_note)
            except Exception as exc:  # noqa: BLE001 — 提议失败不阻塞计数更新
                summary["notes"].append(
                    f"LLM 提议失败（跳过，不影响计数）: {type(exc).__name__}")
        _save_library(raw)
        latest = max(str(r.get("created_at") or "") for r in new_rows) \
            if new_rows else since
        next_state = _load_state()
        next_state["last_scripts_created_at"] = latest or since
        _save_state(next_state)
        summary.update({
            "updated": True, "mode": "refreshed",
            "new_entries": len(new_rows), "counts_touched": counts_touched,
            "proposals_added": proposals_added,
            "new_version": str(raw.get(HOOK_PATTERNS_VERSION_KEY) or ""),
            "duration_ms": int((time.time() - started) * 1000),
        })
        return summary
    except Exception as exc:  # noqa: BLE001 — 刷新绝不阻塞入库
        summary["mode"] = "error"
        summary["notes"].append(f"{type(exc).__name__}: {str(exc)[:200]}")
        return summary


def run_refresh_sync() -> dict[str, Any]:
    """线程入口：独立事件循环执行（API 进程内 fire-and-forget 线程用）。"""
    return refresh_if_new_material()
