"""模型 token 台账与估算（2026-10-15 额度优化的观测部分）。

背景（HANDOFF §1 调查结论）：工作台模型网关按「``prompt_tokens + max_tokens``」
对每个请求做**预留**（未显式设 max_tokens 时按 provider 默认，会把小请求也顶到
429）；额度按日重置，无查询接口、无限额响应头。

本模块只做**观测与留痕**，不做任何拦截/排队（预算闸门已于 2026-10-15 删除，
额度不足时由各模型阶段的自动回退兜底：纠错→保守规整，分析/规划→本地回退，
`plan_source`/`engine`/`fallback_reasons` 如实留痕）：

* :func:`estimate_tokens` / :func:`estimate_reserve`——调用前估算一条请求的
  预留（prompt 估算 + 显式 max_tokens + 每请求固定开销 ~220），随调用记录留痕；
* :func:`record_call` / :func:`today_totals`——把每次调用的预留值与真实 usage
  记入当日台账（``STUDIO_TASKS_ROOT/llm_usage/YYYY-MM-DD.json``，原子写；
  失败静默，记录绝不阻塞管线）；
* :func:`extract_usage`——从 pydantic_ai RunResult 尽力提取真实 usage；
* :func:`is_token_limit_error`——识别「输出上限不足」类错误（供提档重试判断）。

估算系数（诚实说明）：无网关侧 tokenizer 可用，按 CJK ≈0.8 token/字符、
其他 ≈0.3 token/字符保守高估。台账只统计工作台管线（纠错/内容分析/切片规划）；
Copilot 通道与该应用共享额度但不在台账内（不虚报全覆盖）。
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
import logging
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("llm_budget")

# 网关每请求固定开销（HANDOFF §1：prompt 含 ~220 固定开销）。
PER_CALL_OVERHEAD_TOKENS = 220

_CJK_RANGES = (("\u3400", "\u4dbf"), ("\u4e00", "\u9fff"),
               ("\u3000", "\u303f"), ("\uff00", "\uffef"))


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return any(ord(lo) <= code <= ord(hi) for lo, hi in _CJK_RANGES)


def estimate_tokens(text: str) -> int:
    """粗估一条文本的 prompt token 数（保守偏高，宁大勿小）。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if _is_cjk(ch))
    other = len(text) - cjk
    return int(cjk * 0.8 + other * 0.3) + 1


def estimate_reserve(prompt_text: str, max_tokens: int) -> int:
    """一条请求的预计预留 = 估算 prompt + 显式 max_tokens + 固定开销。"""
    return estimate_tokens(prompt_text) + PER_CALL_OVERHEAD_TOKENS + max(0, int(max_tokens))


# ---------------------------------------------------------------------------
# 当日台账（预留制记账）
# ---------------------------------------------------------------------------

def _tasks_root() -> Path:
    base = os.getenv("STUDIO_TASKS_ROOT")
    if not base:
        base = str(Path(__file__).resolve().parent.parent / "tasks")
    return Path(base)


def _ledger_root() -> Path:
    override = os.getenv("LLM_USAGE_DIR", "").strip()
    return Path(override) if override else _tasks_root() / "llm_usage"


def _utc_date(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y-%m-%d")


def ledger_path(now: datetime | None = None) -> Path:
    return _ledger_root() / f"{_utc_date(now)}.json"


def _empty_ledger(now: datetime | None = None) -> dict[str, Any]:
    return {"date": _utc_date(now), "reserved_total": 0, "requests": 0, "entries": []}


def _load_ledger(now: datetime | None = None) -> dict[str, Any]:
    path = ledger_path(now)
    if not path.is_file():
        return _empty_ledger(now)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty_ledger(now)
    if not isinstance(data, dict) or data.get("date") != _utc_date(now):
        return _empty_ledger(now)
    data.setdefault("entries", [])
    data.setdefault("reserved_total", 0)
    data.setdefault("requests", 0)
    return data


def today_totals(now: datetime | None = None) -> tuple[int, int]:
    """返回 ``(当日已记预留 tokens, 当日已记请求数)``。文件缺失/损坏按 0。"""
    data = _load_ledger(now)
    return int(data.get("reserved_total") or 0), int(data.get("requests") or 0)


def record_call(kind: str, *, job_id: str | None = None, est_reserved: int = 0,
                usage: dict[str, Any] | None = None, model: str | None = None,
                note: str | None = None, now: datetime | None = None) -> None:
    """把一次调用（成功或 token-limit 失败——后者同样消耗预留）记入当日台账。

    台账写失败只记日志，绝不抛错（预算记录不阻塞管线）。
    """
    try:
        data = _load_ledger(now)
        entry: dict[str, Any] = {
            "ts": (now or datetime.now(UTC)).isoformat(),
            "kind": kind,
            "job_id": job_id,
            "est_reserved": int(est_reserved),
            "usage": extract_usage(usage),
            "usage_details": extract_usage_details(usage),
            "model": model,
        }
        if note:
            entry["note"] = str(note)[:200]
        data["entries"].append(entry)
        data["reserved_total"] = int(data.get("reserved_total") or 0) + int(est_reserved)
        data["requests"] = int(data.get("requests") or 0) + 1
        directory = _ledger_root()
        directory.mkdir(parents=True, exist_ok=True)
        target = ledger_path(now)
        temporary = target.with_name(f"{target.name}.{uuid.uuid4().hex}.part")
        try:
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001 — 记账失败不影响任务
        logger.debug("llm usage ledger write skipped: %s", exc)


# ---------------------------------------------------------------------------
# 真实 usage 提取
# ---------------------------------------------------------------------------

def extract_usage_details(result: Any) -> dict[str, Any]:
    """读取提供商 usage；未知保留 None，真实零值保留 0，不从模型文本猜测。"""
    def get(obj: Any, key: str) -> Any:
        if isinstance(obj, Mapping):
            return obj.get(key)
        try:
            return getattr(obj, key, None)
        except Exception:
            return None

    raw = get(result, "usage")
    try:
        raw = raw() if callable(raw) else raw
    except Exception:
        raw = None
    # 也接受直接传入的 Usage / Mapping。
    if raw is None and any(get(result, key) is not None for key in
                           ("input_tokens", "request_tokens", "prompt_tokens",
                            "output_tokens", "response_tokens", "total_tokens")):
        raw = result

    def number(obj: Any, *keys: str) -> int | None:
        for key in keys:
            value = get(obj, key)
            if (isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value) and value >= 0 and int(value) == value):
                return int(value)
        return None

    prompt = number(raw, "input_tokens", "request_tokens", "prompt_tokens")
    completion = number(raw, "output_tokens", "response_tokens", "completion_tokens")
    details = get(raw, "details")
    reasoning = number(details, "reasoning_tokens")
    if reasoning is None:
        reasoning = number(get(raw, "output_tokens_details"), "reasoning_tokens")
    if reasoning is None:
        reasoning = number(get(raw, "completion_tokens_details"), "reasoning_tokens")
    if reasoning is None:
        reasoning = number(raw, "reasoning_tokens")
    total = number(raw, "total_tokens", "total")
    if total is None and prompt is not None and completion is not None:
        total = prompt + completion
    status = ("known" if prompt is not None and completion is not None else
              "partial" if any(n is not None for n in (prompt, completion, reasoning, total))
              else "unknown")
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "reasoning_tokens": reasoning, "total_tokens": total,
            "usage_status": status,
            "details": dict(details) if isinstance(details, Mapping) else {}}

def extract_usage(result: Any) -> dict[str, int]:
    """旧三字段兼容视图；判断真实零值必须使用 extract_usage_details。"""
    details = extract_usage_details(result)
    return {key: int(details[key] or 0) for key in
            ("prompt_tokens", "completion_tokens", "reasoning_tokens")}


def is_token_limit_error(exc: BaseException) -> bool:
    """网关/模型「输出上限不足」类错误（可提一档 max_tokens 重试）。"""
    lowered = str(exc).lower()
    return "token limit" in lowered or "max_tokens" in lowered or "token_limit" in lowered
