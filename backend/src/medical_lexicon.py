"""医疗领域词典加载与匹配（P1，零依赖纯标准库）。

数据格式（目录下所有 ``*.tsv``，TAB 分隔，UTF-8）::

    标准词\t类型\t别名/错写(|分隔,可空)\t权重(1-9)\t备注(可空)

* ``#`` 开头行与空行跳过；列数不足 3 的坏行跳过不抛错；
* 权重缺失或不可解析时取默认值 5，并夹取到 1-9；
* ``alias_map`` 形如 ``{别名或标准词: 标准词}``，键冲突时先到先得。

设计约束：

* **零依赖**：只用标准库（pathlib / os / functools），不引入任何第三方包；
* **故障不影响主链路**：目录不存在、文件不可读、解析出错一律返回空结构，
  绝不向上抛异常——词典只是纠错提示的增强项，缺了它纠错照常工作；
* **缓存**：:func:`load_lexicon` 用 ``functools.lru_cache``，缓存键含目录
  路径、目录 mtime 与最新 ``*.tsv`` 文件的 mtime——原地改写文件内容也会
  生效（目录 mtime 只在增删文件时变化），无需重启进程。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

# 权重缺失/不可解析时的默认值（取中位，避免偏向最高或最低优先级）
_DEFAULT_WEIGHT = 5
# 权重合法区间（与 TSV 格式约定一致：1-9）
_WEIGHT_MIN, _WEIGHT_MAX = 1, 9

# 默认词典目录：<backend>/medical_lexicon（可用 MEDICAL_LEXICON_DIR 覆盖）
_DEFAULT_LEXICON_DIR = Path(__file__).resolve().parent.parent / "medical_lexicon"

# 空结构（无词典时的统一返回形状）
_EMPTY: dict = {"entries": [], "alias_map": {}}


def lexicon_dir() -> Path:
    """词典目录：env ``MEDICAL_LEXICON_DIR`` 优先，否则默认 ``<backend>/medical_lexicon``。"""
    env_value = str(os.getenv("MEDICAL_LEXICON_DIR", "")).strip()
    if env_value:
        return Path(env_value).expanduser()
    return _DEFAULT_LEXICON_DIR


def _parse_weight(raw: str) -> int:
    """解析权重列：缺失/不可解析 → 默认值；越界 → 夹取到 1-9。"""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return _DEFAULT_WEIGHT
    return max(_WEIGHT_MIN, min(_WEIGHT_MAX, value))


def _parse_tsv(path: Path) -> tuple[list[dict], dict[str, str]]:
    """解析单个 TSV 文件，返回 ``(条目列表, 别名映射)``。坏行跳过不抛错。"""
    entries: list[dict] = []
    alias_map: dict[str, str] = {}
    with path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            raw = raw.rstrip("\r\n")  # 只去换行符：保留行尾空字段（「可空」列）
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue  # 注释行 / 空行
            parts = raw.split("\t")
            if len(parts) < 3:
                continue  # 坏行（列数不足）：跳过，不影响其余行
            standard = parts[0].strip()
            if not standard:
                continue  # 标准词为空等同坏行
            aliases = [alias.strip() for alias in parts[2].split("|") if alias.strip()]
            entries.append({
                "standard": standard,
                "type": parts[1].strip(),
                "aliases": aliases,
                "weight": _parse_weight(parts[3]) if len(parts) > 3 else _DEFAULT_WEIGHT,
                "note": parts[4].strip() if len(parts) > 4 else "",
            })
            alias_map.setdefault(standard, standard)
            for alias in aliases:
                alias_map.setdefault(alias, standard)
    return entries, alias_map


def _lexicon_fingerprint(directory: Path) -> tuple[str, int, int]:
    """缓存键：目录路径 + 目录 mtime + 最新 ``*.tsv`` 文件 mtime（改文件即生效）。"""
    dir_mtime_ns = directory.stat().st_mtime_ns
    newest_file_mtime_ns = 0
    for path in directory.glob("*.tsv"):
        try:
            newest_file_mtime_ns = max(newest_file_mtime_ns, path.stat().st_mtime_ns)
        except OSError:
            continue  # 单个文件 stat 失败不阻塞键计算
    return (str(directory), dir_mtime_ns, newest_file_mtime_ns)


@lru_cache(maxsize=8)
def _load_lexicon_cached(fingerprint: tuple[str, int, int]) -> dict:
    """实际解析（lru_cache 载体）：缓存键见 :func:`_lexicon_fingerprint`。"""
    entries: list[dict] = []
    alias_map: dict[str, str] = {}
    for path in sorted(Path(fingerprint[0]).glob("*.tsv")):
        file_entries, file_aliases = _parse_tsv(path)
        entries.extend(file_entries)
        for key, standard in file_aliases.items():
            alias_map.setdefault(key, standard)
    return {"entries": entries, "alias_map": alias_map}


def load_lexicon() -> dict:
    """加载全部 ``*.tsv`` 词典，返回 ``{"entries": [...], "alias_map": {...}}``。

    任何 IO/解析异常（含目录不存在）都返回空结构，绝不抛出。
    """
    try:
        return _load_lexicon_cached(_lexicon_fingerprint(lexicon_dir()))
    except Exception:  # noqa: BLE001  # 词典故障绝不影响主链路
        return {"entries": [], "alias_map": {}}


def match_segments(texts: list[str], *, limit: int = 20) -> list[dict]:
    """把批文本拼接后做子串匹配，返回命中的词典条目。

    条目的「标准词」或任一「别名」出现在拼接文本中即命中。返回条目形如
    ``{"standard","type","aliases","weight","note"}``，按权重降序、同权重按
    标准词排序，截断 ``limit`` 条。无词典/无命中返回 ``[]``，绝不抛出。
    """
    try:
        haystack = "".join(str(text or "") for text in (texts or []))
        if not haystack:
            return []
        data = load_lexicon() or {}
        hits: list[dict] = []
        for entry in data.get("entries") or []:
            standard = str(entry.get("standard") or "")
            aliases = [str(alias) for alias in (entry.get("aliases") or [])]
            if standard in haystack or any(alias and alias in haystack for alias in aliases):
                hits.append({
                    "standard": standard,
                    "type": str(entry.get("type") or ""),
                    "aliases": list(aliases),
                    "weight": int(entry.get("weight") or 0),
                    "note": str(entry.get("note") or ""),
                })
        hits.sort(key=lambda item: (-item["weight"], item["standard"]))
        return hits[: max(0, int(limit))]
    except Exception:  # noqa: BLE001  # 词典故障绝不影响主链路
        return []
