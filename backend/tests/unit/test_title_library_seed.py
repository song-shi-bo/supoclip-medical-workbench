"""标题库同步逻辑：双标签组装、幂等字段与种子文件完整性（不依赖真实数据库）。"""
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
