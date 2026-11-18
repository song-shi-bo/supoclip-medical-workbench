"""通用多维度内容标签（content_tags）：热点库/文稿库/标题库统一挂标签与检索。

维度体系（2026-11-18 定稿，不含合规维度）：
- intent  内容意图（患者决策链）：症状识别 / 就医决策 / 方案解释 / 误区辟谣
          / 就医指南 / 临床研究 / 预防生活方式；
- topic   病种主题（标题库 topic_tags / 热点 tags 已承载）；
- audience / format / style / mechanism：后续批次再落。

打标规则：规则关键词优先（本模块，确定性可测试）；人工打标 tagged_by='manual'
永不覆盖；重跑只替换规则标签。检索走 ingest_kb.py 的 /api/v1/tags/browse。
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

INTENT_VOCAB = ("症状识别", "就医决策", "方案解释", "误区辟谣",
                "就医指南", "临床研究", "预防生活方式")

# 内容意图规则：关键词 → 计分，命中最多的意图胜出；并列按 PRIORITY 顺序。
INTENT_RULES: dict[str, tuple[str, ...]] = {
    "症状识别": ("症状", "表现", "信号", "征兆", "前兆", "自查", "自测", "早期",
               "哪些人", "什么人", "最容易", "高危", "概率", "会变成", "癌变",
               "恶变", "转移", "猝死", "最怕", "预警", "多少会"),
    "就医决策": ("挂什么科", "挂号", "就诊", "查一下", "复查", "体检", "空腹",
               "床位", "就医", "要不要做", "医院"),
    "方案解释": ("治疗", "怎么治", "怎么用药", "用药", "开药", "打针", "吃药",
               "药物", "手术", "方案", "免疫", "支架", "扩切", "化疗", "靶向",
               "剂量", "能不能做"),
    "误区辟谣": ("误区", "谣言", "真的吗", "是真的吗", "智商税", "错误",
               "其实不是", "别再", "不要再", "真相", "不是好事", "送走"),
    "就医指南": ("就医指南", "排名", "前十", "前10", "榜单", "专科", "科室",
               "专家门诊", "特需门诊", "医保", "特殊门诊"),
    "临床研究": ("临床", "研究", "入组", "名额", "试验", "新药"),
    "预防生活方式": ("养生", "预防", "饮食", "减肥", "锻炼", "生活方式",
                 "垃圾食品", "清淡"),
}
INTENT_PRIORITY = ("临床研究", "误区辟谣", "就医指南", "方案解释",
                   "就医决策", "症状识别", "预防生活方式")

_SET_TAGS = text("""
    INSERT INTO content_tags (object_type, object_id, dimension, tag, tagged_by)
    VALUES (:object_type, :object_id, :dimension, :tag, :tagged_by)
    ON CONFLICT (object_type, object_id, dimension, tag) DO NOTHING
""")


def classify_intent(title: str, body: str = "") -> str:
    """规则意图分类：命中关键词最多的意图胜出，并列按 PRIORITY；零命中留空。"""
    blob = re.sub(r"\s+", "", f"{title}\n{body}")
    if not blob:
        return ""
    best_tag, best_hits = "", 0
    for intent in INTENT_PRIORITY:
        hits = sum(1 for keyword in INTENT_RULES[intent] if keyword in blob)
        if hits > best_hits:
            best_tag, best_hits = intent, hits
    return best_tag


def _norm_subject(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or ""))


async def tag_intent_all_libraries(db: AsyncSession) -> dict[str, Any]:
    """对热点库/文稿库/标题库存量做 intent 规则打标（人工标签永不覆盖）。"""
    sources = [
        ("hot_topic",
         "SELECT id, title, summary FROM hot_topics WHERE status <> 'disabled'"),
        ("script",
         "SELECT id, title, hook, body FROM scripts WHERE status <> 'disabled'"),
        ("video_title",
         "SELECT id, title FROM video_titles WHERE status = 'active'"),
    ]
    counts: dict[str, int] = {intent: 0 for intent in INTENT_VOCAB}
    counts["未命中"] = 0
    skipped_manual = 0
    for object_type, query in sources:
        rows = (await db.execute(text(query))).fetchall()
        for row in rows:
            data = row._mapping
            object_id = str(data.get("id") or "")
            if not object_id:
                continue
            existing = (await db.execute(text(
                "SELECT tag FROM content_tags WHERE object_type=:ot AND object_id=:oid "
                "AND dimension='intent' AND tagged_by='manual'"),
                {"ot": object_type, "oid": object_id})).scalars().first()
            if existing:
                skipped_manual += 1
                continue
            intent = classify_intent(
                _norm_subject(data.get("title")),
                _norm_subject(" ".join(str(data.get(key) or "") for key in
                                       ("summary", "hook", "body"))))
            await db.execute(text(
                "DELETE FROM content_tags WHERE object_type=:ot AND object_id=:oid "
                "AND dimension='intent' AND tagged_by <> 'manual'"),
                {"ot": object_type, "oid": object_id})
            if intent:
                await db.execute(_SET_TAGS, {
                    "object_type": object_type, "object_id": object_id,
                    "dimension": "intent", "tag": intent, "tagged_by": "rule"})
                counts[intent] += 1
            else:
                counts["未命中"] += 1
    await db.commit()
    return {"tagged": sum(v for k, v in counts.items() if k != "未命中"),
            "skipped_manual": skipped_manual, "by_intent": counts}


async def browse_by_tag(db: AsyncSession, *, dimension: str, tag: str,
                        object_type: str = "") -> list[dict[str, Any]]:
    """按维度+标签跨库检索：返回命中对象（类型、ID、标题）。"""
    library_titles = {
        "hot_topic": "SELECT title FROM hot_topics WHERE id=:oid",
        "script": "SELECT title FROM scripts WHERE id=:oid",
        "video_title": "SELECT title FROM video_titles WHERE id=:oid",
    }
    types = [object_type] if object_type else list(library_titles)
    rows = (await db.execute(text(
        "SELECT object_type, object_id FROM content_tags "
        "WHERE dimension=:dimension AND tag=:tag "
        "AND (:otype = '' OR object_type = :otype) ORDER BY object_type, object_id"),
        {"dimension": dimension, "tag": tag, "otype": object_type})).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        object_type_row, object_id = row.object_type, row.object_id
        title_query = library_titles.get(object_type_row)
        if not title_query:
            continue
        title_row = (await db.execute(text(title_query), {"oid": object_id})).first()
        items.append({"object_type": object_type_row, "object_id": object_id,
                      "title": str(title_row.title) if title_row else ""})
    return items


async def tagged_intents_summary(db: AsyncSession) -> dict[str, Any]:
    """各库 intent 标签分布（供脚本输出与验收）。"""
    rows = (await db.execute(text(
        "SELECT object_type, tag, count(*) FROM content_tags "
        "WHERE dimension='intent' GROUP BY object_type, tag "
        "ORDER BY object_type, count(*) DESC"))).all()
    result: dict[str, Any] = {}
    for object_type, tag, count in rows:
        result.setdefault(str(object_type), {})[str(tag)] = int(count)
    return result
