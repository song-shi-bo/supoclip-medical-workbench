"""标题库（video_titles）：热点标题与剪辑成片标题的双标签（钩子类型 + 病种主题）管理。

两类来源（迁移 20261116_0007 / 20261116_0008）：
- ``source='hot_topic'``：``hot_topics`` 中带钩子类型标签的条目，``tags[0]`` 即标签1，
  标签2 来自 ``seed/title_library_topic_tags.json`` 人工标注，``id`` 复用热点条目 id；
- ``source='generated'``：工作台剪辑成片标题（项目台账 clips.json 卡片），手动批量导入
  （``scripts/import_generated_titles.py``）。标签1 按六类原型自动判类（本地嵌入 + 词法
  规则回退），标签2 按病种词表别名命中/相似度自动匹配；人工纠错写在
  ``seed/title_library_generated.json`` 的 overrides 里，权重永远高于自动结果。
- 检索走 ``GET /api/v1/titles/search``（ingest_kb.py）。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

SEED_PATH = Path(__file__).resolve().parent.parent / "seed" / "title_library_topic_tags.json"
GENERATED_SEED_PATH = Path(__file__).resolve().parent.parent / "seed" / "title_library_generated.json"

HOOK_TAGS = ("热点型", "流量型", "信任型", "品牌型", "收藏型", "问答型")

# 六类钩子原型（自动判类用）：短句描述每类标题的典型形态，供本地嵌入模型比对。
HOOK_PROTOTYPES = {
    "热点型": "秋冬春季高发期疫情爆发流行趋势上涨提醒大家注意防护换季热点时事",
    "流量型": "惊人真相秘密智商税千万别崩溃最怕送走颠覆认知悬念好奇点击",
    "信任型": "权威专家医生真实病例深度对话专业背书误区讲解医学生",
    "品牌型": "医院科室排名前十榜单挂号门诊区别医生日常机构展示",
    "收藏型": "实用干货清单收藏备查体检项目急救知识盘点合集必查保存",
    "问答型": "患者常见疑问能不能为什么怎么治怎么办区别在哪答疑一问一答",
}

# 词法回退规则（嵌入不可用时）：按顺序取第一个命中的类别。
_HOOK_LEXICAL_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("品牌型", ("排名", "医院", "科室", "前十", "前10", "榜单", "门诊", "挂号", "院区")),
    ("收藏型", ("收藏", "必查", "清单", "盘点", "top", "记住这", "攻略", "必会")),
    ("热点型", ("高发期", "爆发", "又来了", "秋冬", "冬季", "换季", "防护", "预警", "天冷", "寒冷")),
    ("流量型", ("秘密", "真相", "智商税", "千万别", "崩溃", "最怕", "送走", "不会主动说", "漏诊")),
    ("问答型", ("为什么", "怎么", "能不能", "是什么", "什么区别", "答疑", "吗？", "?")),
    ("信任型", ("医生", "专家", "病例", "对话", "误区", "科普")),
)

# 病种词表别名：标题命中别名即视为强证据（不受相似度阈值限制）。
DISEASE_ALIASES: dict[str, tuple[str, ...]] = {
    "心梗脑梗": ("心梗", "脑梗", "猝死", "心脑血管意外"),
    "高血压": ("降压", "血压"),
    "糖尿病": ("血糖",),
    "肝病": ("转氨酶", "肝硬化", "肝癌", "肝指标", "肝衰竭"),
    "腰椎间盘突出": ("腰间盘突出", "腰突", "腰疼腿麻", "腰椎"),
    "血管斑块": ("斑块",),
    "流感": ("甲流",),
    "多囊卵巢": ("多囊",),
    "老龄化": (),
}

DISEASE_COSINE_THRESHOLD = 0.55
DISEASE_MAX_TAGS = 3
HOOK_COSINE_THRESHOLD = 0.42

UPSERT = text("""
    INSERT INTO video_titles (id, title, hook_tag, topic_tags, heat_score, content_hash,
                              source, project_id)
    VALUES (:id, :title, :hook_tag, CAST(:topic_tags AS text[]), :heat_score, :content_hash,
            :source, :project_id)
    ON CONFLICT (id) DO UPDATE SET
        title = EXCLUDED.title,
        hook_tag = EXCLUDED.hook_tag,
        topic_tags = EXCLUDED.topic_tags,
        heat_score = EXCLUDED.heat_score,
        content_hash = EXCLUDED.content_hash,
        source = EXCLUDED.source,
        project_id = EXCLUDED.project_id,
        updated_at = now()
    RETURNING (xmax = 0) AS inserted
""")


def load_topic_tags(path: Path | None = None) -> dict[str, list[str]]:
    payload = json.loads((path or SEED_PATH).read_text(encoding="utf-8"))
    return {str(key): [str(tag) for tag in value]
            for key, value in payload.get("topic_tags", {}).items()}


def load_generated_overrides(path: Path | None = None) -> dict[str, dict[str, Any]]:
    payload = json.loads((path or GENERATED_SEED_PATH).read_text(encoding="utf-8"))
    return {str(key): dict(value) for key, value in payload.get("overrides", {}).items()}


def disease_vocabulary(mapping: dict[str, list[str]] | None = None) -> list[str]:
    mapping = mapping if mapping is not None else load_topic_tags()
    seen: list[str] = []
    for tags in mapping.values():
        for tag in tags:
            if tag and tag not in seen:
                seen.append(tag)
    return seen


def title_hash(title: str, hook_tag: str) -> str:
    norm = "".join(ch for ch in f"{title}|{hook_tag}".lower() if ch.isalnum())
    return hashlib.sha256(norm.encode()).hexdigest()


def _norm_tag(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(value or "")).lower()


def _lexical_hook_tag(title: str) -> str:
    lowered = str(title or "").lower()
    for tag, keywords in _HOOK_LEXICAL_RULES:
        if any(keyword in lowered for keyword in keywords):
            return tag
    return ""


def _embed_cache(key: str, texts: list[str]) -> list[list[float]] | None:
    from . import kb_embedding

    cache = _EMBED_CACHE.setdefault(key, {})
    missing = [value for value in texts if value not in cache]
    if missing:
        vectors = kb_embedding.embed_texts(missing)
        if not vectors:
            return None
        for value, vector in zip(missing, vectors):
            cache[value] = [float(item) for item in vector]
    return [cache[value] for value in texts]


_EMBED_CACHE: dict[str, dict[str, list[float]]] = {}


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    denom = (sum(x * x for x in a) ** 0.5) * (sum(y * y for y in b) ** 0.5)
    score = dot / denom if denom else 0.0
    return score if 0.0 <= score <= 1.0 else max(0.0, min(1.0, score))


def auto_hook_tag(title: str) -> str:
    """六类钩子类型自动判类：本地嵌入相似度为主，词法规则回退，判定不了留空。"""
    title = str(title or "").strip()
    if not title:
        return ""
    vectors = _embed_cache("hook", [title, *HOOK_PROTOTYPES.values()])
    if vectors:
        title_vec = vectors[0]
        scored = sorted(zip(HOOK_PROTOTYPES.keys(), vectors[1:]),
                        key=lambda pair: -_cosine(title_vec, pair[1]))
        best_tag, best_vec = scored[0]
        best = _cosine(title_vec, best_vec)
        second = _cosine(title_vec, scored[1][1])
        if best >= HOOK_COSINE_THRESHOLD:
            return str(best_tag)
    return _lexical_hook_tag(title)


def auto_topic_tags(title: str, vocab: Iterable[str] | None = None) -> list[str]:
    """病种标签自动匹配：别名/全名命中为强证据，其余按嵌入相似度过阈值。"""
    title = str(title or "").strip()
    if not title:
        return []
    vocabulary = list(vocab) if vocab is not None else disease_vocabulary()
    title_norm = _norm_tag(title)
    title_vec = (_embed_cache("topic", [title]) or [None])[0]
    vectors = _embed_cache("topic", list(vocabulary)) if title_vec else None
    scored: list[tuple[float, str]] = []
    for index, tag in enumerate(vocabulary):
        tag_norm = _norm_tag(tag)
        aliases = DISEASE_ALIASES.get(tag, ())
        if tag_norm and tag_norm in title_norm:
            scored.append((1.0, tag))
            continue
        if any(_norm_tag(alias) and _norm_tag(alias) in title_norm for alias in aliases):
            scored.append((0.95, tag))
            continue
        if vectors and title_vec:
            similarity = _cosine(title_vec, vectors[index])
            if similarity >= DISEASE_COSINE_THRESHOLD:
                scored.append((similarity, tag))
    scored.sort(key=lambda pair: -pair[0])
    return [tag for _, tag in scored[:DISEASE_MAX_TAGS]]


def build_rows(rows: Iterable[Any], mapping: dict[str, list[str]] | None = None) -> list[dict[str, Any]]:
    """hot_topics 行 → video_titles 行。

    标签1（hook_tag）取 tags 中的六类钩子类型（无则留空待标）；
    标签2（topic_tags）优先人工标注映射，缺省回退热点自带标签
    （剔除钩子类型与批次标记，如「门诊素材」）。
    """
    mapping = mapping if mapping is not None else {}
    out: list[dict[str, Any]] = []
    for row in rows:
        data = row._mapping if hasattr(row, "_mapping") else row
        title = str(data.get("title") or "").strip()
        if not title:
            continue
        tags = [str(tag) for tag in (data.get("tags") or [])]
        hook_tag = tags[0] if tags and tags[0] in HOOK_TAGS else ""
        entry_id = str(data.get("id") or "")
        topic_tags = list(mapping.get(entry_id) or [])
        if not topic_tags:
            topic_tags = [t for t in tags if t and t not in HOOK_TAGS
                          and t != "门诊素材"]
        out.append({
            "id": entry_id,
            "title": title,
            "hook_tag": hook_tag,
            "topic_tags": topic_tags,
            "heat_score": int(data.get("heat_score") or 0),
            "content_hash": title_hash(title, hook_tag),
            "source": "hot_topic",
            "project_id": "",
        })
    return out


def build_generated_rows(cards: Iterable[dict[str, Any]],
                         overrides: dict[str, dict[str, Any]] | None = None,
                         vocab: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """项目台账成片卡片 → video_titles 行；overrides（seed 文件）优先于自动判类。"""
    overrides = overrides or {}
    out: list[dict[str, Any]] = []
    for card in cards:
        clip_id = str(card.get("clip_id") or "").strip()
        override = overrides.get(clip_id, {})
        title = str(override.get("title") or card.get("title")
                    or card.get("topic") or "").strip()
        if not title or not clip_id:
            continue
        override_hook = str(override.get("hook_tag") or "").strip()
        hook_tag = override_hook if override_hook in HOOK_TAGS else auto_hook_tag(title)
        override_tags = [str(tag).strip() for tag in (override.get("topic_tags") or [])]
        topic_tags = [tag for tag in override_tags if tag] or auto_topic_tags(title, vocab)
        out.append({
            "id": clip_id,
            "title": title,
            "hook_tag": hook_tag,
            "topic_tags": topic_tags,
            "heat_score": int(card.get("heat_score") or 0),
            "content_hash": title_hash(title, hook_tag),
            "source": "generated",
            "project_id": str(card.get("project_id") or ""),
        })
    return out


async def sync_title_library(db: AsyncSession, *, mapping_path: Path | None = None) -> dict[str, int]:
    """全量幂等同步非 disabled 热点标题到标题库；返回插入/更新计数。

    2026-11-18 扩大范围：不再限于六类钩子标签——病种标签批次（如门诊素材）
    同样入库，钩子类型留空待标；病种标签回退用热点自带标签。
    """
    mapping = load_topic_tags(mapping_path)
    rows = (await db.execute(text(
        "SELECT id, title, tags, heat_score FROM hot_topics "
        "WHERE status <> 'disabled'"))).fetchall()
    payloads = build_rows(rows, mapping)
    inserted = updated = 0
    for values in payloads:
        result = await db.execute(UPSERT, values)
        row = result.first()
        if row is not None and row.inserted:
            inserted += 1
        else:
            updated += 1
    await db.commit()
    return {"synced": len(payloads), "inserted": inserted, "updated": updated}


async def import_generated_titles(db: AsyncSession) -> dict[str, Any]:
    """把项目台账里的成片标题幂等导入标题库（手动触发，不挂自动剪辑链）。"""
    from . import studio_project_store as store

    cards: list[dict[str, Any]] = []
    for project in store.list_projects(include_archived=False):
        manifest = store.load_manifest(project["project_id"])
        for clip_id, entry in (manifest.get("clips") or {}).items():
            if not isinstance(entry, dict):
                continue
            cards.append({**entry, "clip_id": str(clip_id),
                          "project_id": project["project_id"]})
    payloads = build_generated_rows(cards, load_generated_overrides())
    inserted = updated = 0
    for values in payloads:
        result = await db.execute(UPSERT, values)
        row = result.first()
        if row is not None and row.inserted:
            inserted += 1
        else:
            updated += 1
    await db.commit()
    need_manual = [value["id"] for value in payloads
                   if not value["hook_tag"] or not value["topic_tags"]]
    return {"imported": len(payloads), "inserted": inserted, "updated": updated,
            "need_manual_tags": need_manual}
