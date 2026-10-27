"""transcript_revisions 单元测试：不可变基线 + 三态视图 + 修订/回滚 + 台账 mirror。

纯文件系统（``TASKS_ROOT`` 打桩到 tmp_path），不连数据库、不联网、不跑 ffmpeg。
覆盖：
1. seed 幂等 —— 第二次 seed 绝不覆盖 raw/corrected（含 corrected 从无到有的情况）；
2. 三态分歧：raw → corrected → effective；回滚 v1 后 effective 变回去，两个基线不动；
3. add_revision 单调递增 + 同步 clips.json 的 transcript_revision；
4. clip 未登记时仍落盘，只在 strict_mirror / 返回值里如实上报；
5. rollback 校验修订存在（E_TRANSCRIPT_REVISION_UNKNOWN）；
6. changes 结构错误的显式 E_ 码；
7. 纯函数 effective_segments 不写盘、不改传入文档；
8. 路径布局与原子写（不留 .tmp）。
"""

from __future__ import annotations

import json

import pytest

from src import studio_project_store as store
from src import transcript_revisions as tr

PROJECT = "timeline_abc123def456"
CLIP = "clip-0001"


@pytest.fixture
def tasks_root(tmp_path, monkeypatch):
    """两个模块的任务根一起打桩：mirror 与转写文档落在同一个临时目录。"""

    root = tmp_path / "tasks"
    monkeypatch.setattr(store, "TASKS_ROOT", root)
    monkeypatch.setattr(tr, "TASKS_ROOT", root)
    return root


def _segments(texts: tuple[str, ...] = ("原始一", "原始二")) -> list[dict]:
    return [
        {
            "segment_id": f"seg-{index + 1}",
            "start": float(index * 2),
            "end": float(index * 2 + 2),
            "text": text,
            "speaker": "未标注",
        }
        for index, text in enumerate(texts)
    ]


def _texts(doc: dict, variant: str = "effective") -> list[str]:
    return [segment["text"] for segment in tr.effective_segments(doc, variant)]


# ---------------------------------------------------------------------------
# 1. 幂等：raw / corrected 一次定终身
# ---------------------------------------------------------------------------

def test_seed_is_idempotent_and_never_overwrites_baselines(tasks_root):
    first = tr.seed_document(
        PROJECT, CLIP,
        raw_segments=_segments(("原始一", "原始二")),
        corrected_segments=_segments(("纠错一", "纠错二")),
    )
    again = tr.seed_document(
        PROJECT, CLIP,
        raw_segments=_segments(("被改一", "被改二")),
        corrected_segments=_segments(("被改纠错",)),
        raw_text="覆盖 raw_text",
        corrected_text="覆盖 corrected_text",
    )

    assert again["raw_segments"] == first["raw_segments"]
    assert again["corrected_segments"] == first["corrected_segments"]
    assert again["raw_text"] == first["raw_text"]
    assert again["corrected_text"] == first["corrected_text"]
    assert "被改" not in json.dumps(again, ensure_ascii=False)

    # 落盘内容就是第一次 seed 的内容（不是内存里返回了旧的、盘上被改写）。
    on_disk = json.loads(tr.document_path(PROJECT, CLIP).read_text(encoding="utf-8"))
    assert on_disk == first


def test_reseed_cannot_backfill_a_missing_corrected_baseline(tasks_root):
    """第一次 seed 没给 corrected，之后也不能靠再 seed「补上」——只能走 add_revision。"""

    tr.seed_document(PROJECT, CLIP, raw_segments=_segments())
    again = tr.seed_document(PROJECT, CLIP, raw_segments=_segments(),
                             corrected_segments=_segments(("后来的纠错", "后来的纠错二")))
    assert again["corrected_segments"] == []
    assert again["corrected_text"] == ""


def test_ensure_document_seeds_once_and_keeps_first_baseline(tasks_root):
    first = tr.ensure_document(PROJECT, CLIP, segments=_segments(("成片段一", "成片段二")))
    second = tr.ensure_document(PROJECT, CLIP, segments=_segments(("新转写",)))
    assert second["raw_segments"] == first["raw_segments"]
    assert _texts(second, "raw") == ["成片段一", "成片段二"]


def test_ensure_document_accepts_video_brain_segment_keys(tasks_root):
    """成片侧的段可能是 raw_text/corrected_text 键，同样能作为基线。"""

    document = tr.ensure_document(PROJECT, CLIP, segments=[
        {"segment_id": "seg_0000", "start": 0.0, "end": 1.5, "raw_text": "ASR 原文"},
    ])
    assert _texts(document, "raw") == ["ASR 原文"]
    assert document["raw_segments"][0]["speaker"] == "未标注"


def test_seed_rejects_non_list_segments(tasks_root):
    with pytest.raises(tr.TranscriptRevisionError) as excinfo:
        tr.seed_document(PROJECT, CLIP, raw_segments="不是列表")
    assert excinfo.value.code == "E_TRANSCRIPT_SEED_INVALID"


# ---------------------------------------------------------------------------
# 2. 三态分歧与回滚
# ---------------------------------------------------------------------------

def test_three_state_divergence_and_rollback(tasks_root):
    tr.seed_document(
        PROJECT, CLIP,
        raw_segments=_segments(("原始一", "原始二")),
        corrected_segments=_segments(("纠错一", "纠错二")),
    )
    doc = tr.load_document(PROJECT, CLIP)
    assert _texts(doc, "raw") == ["原始一", "原始二"]
    assert _texts(doc, "corrected") == ["纠错一", "纠错二"]
    # 还没有修订 → effective 就是 corrected。
    assert _texts(doc, "effective") == ["纠错一", "纠错二"]

    tr.add_revision(PROJECT, CLIP, changes=[
        {"segment_id": "seg-1", "before": "纠错一", "after": "医生版一"},
    ], reason="改一处")
    doc = tr.load_document(PROJECT, CLIP)
    assert _texts(doc) == ["医生版一", "纠错二"]
    assert _texts(doc, "raw") == ["原始一", "原始二"]
    assert _texts(doc, "corrected") == ["纠错一", "纠错二"]

    tr.add_revision(PROJECT, CLIP, changes=[
        {"segment_id": "seg-1", "before": "医生版一", "after": "医生版一v2"},
        {"segment_id": "seg-2", "speaker_after": "医生"},
    ])
    doc = tr.load_document(PROJECT, CLIP)
    assert _texts(doc) == ["医生版一v2", "纠错二"]
    assert tr.effective_segments(doc)[1]["speaker"] == "医生"
    assert [r["revision"] for r in tr.list_revisions(PROJECT, CLIP)] == [1, 2]

    tr.rollback(PROJECT, CLIP, 1)
    doc = tr.load_document(PROJECT, CLIP)
    assert doc["approved_revision"] == 1
    assert _texts(doc) == ["医生版一", "纠错二"]
    # 回滚只改「被批准的那一条」，两个基线依旧一动不动。
    assert _texts(doc, "raw") == ["原始一", "原始二"]
    assert _texts(doc, "corrected") == ["纠错一", "纠错二"]
    assert tr.effective_segments(doc)[1]["speaker"] == "未标注"


def test_effective_without_corrected_baseline_uses_raw(tasks_root):
    tr.seed_document(PROJECT, CLIP, raw_segments=_segments(("原始一", "原始二")))
    tr.add_revision(PROJECT, CLIP, changes=[{"segment_id": "seg-2", "after": "改第二句"}])
    doc = tr.load_document(PROJECT, CLIP)
    assert _texts(doc) == ["原始一", "改第二句"]
    assert _texts(doc, "corrected") == []          # 没有纠错稿时 corrected 视图就是空
    assert _texts(doc, "raw") == ["原始一", "原始二"]


# ---------------------------------------------------------------------------
# 3. 修订号 / mirror
# ---------------------------------------------------------------------------

def test_add_revision_numbers_monotonically_and_mirrors_store(tasks_root):
    store.register_clips(PROJECT, "autoedit_aaaaaaaaaaaa", [{"clip_id": CLIP, "duration": 4.0}])
    tr.seed_document(PROJECT, CLIP, raw_segments=_segments())

    first = tr.add_revision(PROJECT, CLIP, changes=[{"segment_id": "seg-1", "after": "a"}])
    second = tr.add_revision(PROJECT, CLIP, changes=[{"segment_id": "seg-1", "after": "b"}])
    assert first["revision"] == 1
    assert second["revision"] == 2
    assert second["approved_revision"] == 2
    assert second["mirror"] == "synced" and second["mirror_error"] is None
    assert store.load_manifest(PROJECT)["clips"][CLIP]["transcript_revision"] == 2

    tr.rollback(PROJECT, CLIP, 1)
    assert store.load_manifest(PROJECT)["clips"][CLIP]["transcript_revision"] == 1

    # 瞬时字段不落盘：文档 JSON 严格保持 §3.2 schema。
    on_disk = json.loads(tr.document_path(PROJECT, CLIP).read_text(encoding="utf-8"))
    assert "mirror" not in on_disk and "mirror_error" not in on_disk and "revision" not in on_disk
    assert set(on_disk) == {
        "schema_version", "project_id", "clip_id", "raw_text", "corrected_text",
        "raw_segments", "corrected_segments", "revisions", "approved_revision", "updated_at",
    }


def test_add_revision_persists_even_when_clip_is_not_registered(tasks_root):
    tr.seed_document(PROJECT, CLIP, raw_segments=_segments())
    result = tr.add_revision(PROJECT, CLIP, changes=[{"segment_id": "seg-1", "after": "改"}])

    assert result["mirror"] == "skipped"
    assert "未登记" in result["mirror_error"]
    assert tr.load_document(PROJECT, CLIP)["approved_revision"] == 1
    # mirror 失败不会写出半张台账。
    assert not store.manifest_path(PROJECT).exists()

    # 调用方明确要求 mirror 时（strict_mirror）才抛错。
    with pytest.raises(store.ProjectStoreError) as excinfo:
        tr.add_revision(PROJECT, CLIP, changes=[{"segment_id": "seg-1", "after": "再改"}],
                        strict_mirror=True)
    assert excinfo.value.code == "E_CLIP_NOT_REGISTERED"
    # 修订本身仍然已经落盘（文档是事实源）。
    assert tr.load_document(PROJECT, CLIP)["approved_revision"] == 2


def test_revision_entry_shape(tasks_root):
    tr.seed_document(PROJECT, CLIP, raw_segments=_segments())
    tr.add_revision(PROJECT, CLIP, changes=[
        {"segment_id": "seg-1", "before": "原始一", "after": "新一", "speaker_before": "未标注",
         "speaker_after": "医生"},
    ], reason="医生复核", author="医生A")

    revision = tr.list_revisions(PROJECT, CLIP)[0]
    assert set(revision) == {"revision", "author", "created_at", "reason", "changes"}
    assert revision["author"] == "医生A"
    assert revision["reason"] == "医生复核"
    assert revision["changes"][0] == {
        "segment_id": "seg-1", "before": "原始一", "after": "新一",
        "speaker_before": "未标注", "speaker_after": "医生",
    }


# ---------------------------------------------------------------------------
# 4. 错误码
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("changes", [
    None,
    {},
    "x",
    [],
    ["not-a-dict"],
    [{"after": "缺少 segment_id"}],
    [{"segment_id": "seg-9", "after": "未知段"}],
    [{"segment_id": "seg-1"}],
    [{"segment_id": "seg-1", "after": 5}],
    [{"segment_id": "seg-1", "speaker_after": ["医生"]}],
])
def test_invalid_changes_are_rejected(tasks_root, changes):
    tr.seed_document(PROJECT, CLIP, raw_segments=_segments())
    with pytest.raises(tr.TranscriptRevisionError) as excinfo:
        tr.add_revision(PROJECT, CLIP, changes=changes)
    assert excinfo.value.code == "E_TRANSCRIPT_CHANGES_INVALID"
    assert tr.load_document(PROJECT, CLIP)["revisions"] == []


def test_operations_require_a_seeded_baseline(tasks_root):
    with pytest.raises(tr.TranscriptRevisionError) as first:
        tr.add_revision(PROJECT, CLIP, changes=[{"segment_id": "seg-1", "after": "x"}])
    assert first.value.code == "E_TRANSCRIPT_NOT_SEEDED"

    with pytest.raises(tr.TranscriptRevisionError) as second:
        tr.rollback(PROJECT, CLIP, 1)
    assert second.value.code == "E_TRANSCRIPT_NOT_SEEDED"

    with pytest.raises(tr.TranscriptRevisionError) as third:
        tr.document_view(PROJECT, CLIP)
    assert third.value.code == "E_TRANSCRIPT_NOT_SEEDED"


def test_rollback_rejects_unknown_revision(tasks_root):
    tr.seed_document(PROJECT, CLIP, raw_segments=_segments())
    tr.add_revision(PROJECT, CLIP, changes=[{"segment_id": "seg-1", "after": "a"}])

    for revision in (2, 7, 0, "x"):
        with pytest.raises(tr.TranscriptRevisionError) as excinfo:
            tr.rollback(PROJECT, CLIP, revision)
        assert excinfo.value.code == "E_TRANSCRIPT_REVISION_UNKNOWN"
    # 失败的 rollback 不改动被批准的修订。
    assert tr.load_document(PROJECT, CLIP)["approved_revision"] == 1


def test_error_codes_are_value_errors(tasks_root):
    assert issubclass(tr.TranscriptRevisionError, ValueError)


# ---------------------------------------------------------------------------
# 5. 纯函数 / 读路径容错 / 路径布局
# ---------------------------------------------------------------------------

def test_effective_segments_is_pure_and_supports_three_variants(tasks_root):
    document = tr.seed_document(
        PROJECT, CLIP,
        raw_segments=_segments(("原始一", "原始二")),
        corrected_segments=_segments(("纠错一", "纠错二")),
    )
    snapshot = json.dumps(document, sort_keys=True, ensure_ascii=False)

    raw = tr.effective_segments(document, "raw")
    raw[0]["text"] = "外部改的"
    raw.append({"segment_id": "外部加的"})
    assert json.dumps(document, sort_keys=True, ensure_ascii=False) == snapshot
    assert tr.effective_segments(document, "raw")[0]["text"] == "原始一"

    with pytest.raises(tr.TranscriptRevisionError) as excinfo:
        tr.effective_segments(document, "bogus")
    assert excinfo.value.code == "E_TRANSCRIPT_VARIANT_UNKNOWN"


def test_load_document_missing_or_corrupt_returns_none(tasks_root):
    assert tr.load_document(PROJECT, CLIP) is None
    path = tr.document_path(PROJECT, CLIP)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert tr.load_document(PROJECT, CLIP) is None
    assert tr.list_revisions(PROJECT, CLIP) == []


def test_document_path_layout_and_no_temp_files(tasks_root):
    path = tr.document_path(PROJECT, CLIP)
    assert path == tasks_root / "projects" / PROJECT / "transcripts" / f"{CLIP}.json"

    tr.ensure_document(PROJECT, CLIP, segments=_segments())
    tr.add_revision(PROJECT, CLIP, changes=[{"segment_id": "seg-1", "after": "x"}])
    assert [child.name for child in path.parent.iterdir()] == [path.name]


@pytest.mark.parametrize("project_id", ["", "abc", "timeline_", "timeline_../../etc", "timeline_a/b"])
def test_invalid_project_id_is_rejected(tasks_root, project_id):
    with pytest.raises(store.ProjectStoreError) as excinfo:
        tr.document_path(project_id, CLIP)
    assert excinfo.value.code == "E_PROJECT_ID_INVALID"


@pytest.mark.parametrize("clip_id", ["", "../x", "a/b", "-bad"])
def test_invalid_clip_id_is_rejected(tasks_root, clip_id):
    with pytest.raises(store.ProjectStoreError) as excinfo:
        tr.document_path(PROJECT, clip_id)
    assert excinfo.value.code == "E_CLIP_ID_INVALID"


# ---------------------------------------------------------------------------
# 6. document_view
# ---------------------------------------------------------------------------

def test_document_view_exposes_three_states(tasks_root):
    tr.seed_document(
        PROJECT, CLIP,
        raw_segments=_segments(("原始一", "原始二")),
        corrected_segments=_segments(("纠错一", "纠错二")),
    )
    tr.add_revision(PROJECT, CLIP, changes=[{"segment_id": "seg-1", "after": "医生版一"}])

    view = tr.document_view(PROJECT, CLIP)
    assert view["project_id"] == PROJECT and view["clip_id"] == CLIP
    assert view["transcript_revision"] == 1
    assert view["approved_revision"] == 1
    assert view["raw_text"] == "原始一\n原始二"
    assert view["corrected_text"] == "纠错一\n纠错二"
    assert view["effective_text"] == "医生版一\n纠错二"
    assert [segment["text"] for segment in view["effective_segments"]] == ["医生版一", "纠错二"]
    assert [segment["text"] for segment in view["raw_segments"]] == ["原始一", "原始二"]
    assert [segment["text"] for segment in view["corrected_segments"]] == ["纠错一", "纠错二"]
    assert [revision["revision"] for revision in view["revisions"]] == [1]


def test_document_view_can_seed_on_first_read(tasks_root):
    view = tr.document_view(PROJECT, CLIP, seed_segments=_segments(("只有转写",)))
    assert view["effective_text"] == "只有转写"
    assert view["approved_revision"] == 0
    # 第二次带不同 seed_segments 读取：基线不变（仍然幂等）。
    again = tr.document_view(PROJECT, CLIP, seed_segments=_segments(("不该生效",)))
    assert again["raw_segments"] == view["raw_segments"]
