"""标题库同步/导入逻辑：双标签组装、自动判类与病种匹配（不依赖真实数据库的纯逻辑部分）。"""
from __future__ import annotations

import json
from types import SimpleNamespace

from src import title_library as tl


class _Row:
    def __init__(self, **values):
        self._mapping = values


def test_build_rows_assigns_both_tags_and_defaults_empty_topic_tags():
    rows = [
        _Row(id="id-1", title="秋冬季是心梗脑梗的高发期",
             tags=["热点型"], heat_score=473583),
        _Row(id="id-2", title="未标注的新标题", tags=["流量型"], heat_score=1),
        _Row(id="id-3", title="", tags=["问答型"], heat_score=5),
    ]
    payloads = tl.build_rows(rows, {"id-1": ["心梗脑梗", "心脑血管"]})
    assert payloads[0] == {
        "id": "id-1", "title": "秋冬季是心梗脑梗的高发期", "hook_tag": "热点型",
        "topic_tags": ["心梗脑梗", "心脑血管"], "heat_score": 473583,
        "content_hash": tl.title_hash("秋冬季是心梗脑梗的高发期", "热点型"),
        "source": "hot_topic", "project_id": "",
    }
    assert payloads[1]["topic_tags"] == []
    assert len(payloads) == 2


def test_title_hash_is_stable_and_content_sensitive():
    first = tl.title_hash("同一个标题", "热点型")
    assert first == tl.title_hash("同一个标题", "热点型")
    assert first != tl.title_hash("同一个标题", "流量型")
    assert first != tl.title_hash("另一个标题", "热点型")


def test_seed_mapping_covers_all_hook_tagged_ids_and_clean_vocab():
    payload = json.loads(tl.SEED_PATH.read_text(encoding="utf-8"))
    assert set(payload["hook_tags"]) == set(tl.HOOK_TAGS)
    mapping = payload["topic_tags"]
    assert mapping and len(mapping) >= 59
    vocab: set[str] = set()
    for entry_id, tags in mapping.items():
        assert len(entry_id) == 36 and entry_id.count("-") == 4
        assert tags and len(tags) <= 4
        assert len(set(tags)) == len(tags)
        vocab.update(tags)
    assert all(tag and tag == tag.strip() for tag in vocab)


def test_lexical_hook_tag_rules_hit_each_category():
    assert tl._lexical_hook_tag("广州最强专科科室排行榜，看病路上不求人") == "品牌型"
    assert tl._lexical_hook_tag("普通人必会急救知识top 10") == "收藏型"
    assert tl._lexical_hook_tag("甲流全国性爆发！为啥今年流感这么猛？") == "热点型"
    assert tl._lexical_hook_tag("一般医生不会主动说的10个健康秘密") == "流量型"
    assert tl._lexical_hook_tag("心脏支架能用一辈子吗？8大最实用的问题") == "问答型"
    assert tl._lexical_hook_tag("深度对话上海新华医院何继业医生") in tl.HOOK_TAGS


def test_auto_topic_tags_alias_and_empty_guard(monkeypatch):
    monkeypatch.setattr(tl, "disease_vocabulary",
                        lambda: ["心梗脑梗", "高血压", "体检", "肾病"])
    # 别名强证据：标题没出现完整标签也能命中
    assert "心梗脑梗" in tl.auto_topic_tags("天冷后心脏最怕的猝死瞬间")
    assert "高血压" in tl.auto_topic_tags("做好日常降压管理")
    # 完全无关标题且嵌入不可用 → 空（等人工标注）
    monkeypatch.setattr(tl, "_embed_cache", lambda key, texts: None)
    assert tl.auto_topic_tags("今天天气不错") == []


def test_build_generated_rows_prefers_overrides_and_marks_source():
    cards = [
        {"clip_id": "clip-1", "title": "", "topic": "高血压误区",
         "project_id": "timeline_a"},
        {"clip_id": "clip-2", "title": "体检别乱做", "project_id": "timeline_a"},
    ]
    overrides = {"clip-2": {"hook_tag": "收藏型", "topic_tags": ["体检"]}}
    monkeypatch_embed = {"hook": [[1.0, 0.0]]}

    payloads = tl.build_generated_rows(cards, overrides, vocab=["高血压", "体检"])
    assert payloads[0]["source"] == "generated"
    assert payloads[0]["project_id"] == "timeline_a"
    assert payloads[0]["title"] == "高血压误区"
    assert payloads[1]["hook_tag"] == "收藏型"
    assert payloads[1]["topic_tags"] == ["体检"]
    assert all(row["id"] for row in payloads)


def test_generated_overrides_seed_is_valid():
    payload = json.loads(tl.GENERATED_SEED_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload.get("overrides"), dict)
    for clip_id, override in payload["overrides"].items():
        assert clip_id and isinstance(override, dict)
        hook = str(override.get("hook_tag") or "")
        assert hook in tl.HOOK_TAGS or hook == ""
        tags = override.get("topic_tags") or []
        assert isinstance(tags, list) and all(isinstance(tag, str) for tag in tags)
