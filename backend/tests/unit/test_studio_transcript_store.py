"""studio_transcript_store：已纠错转写版本化存储的读写契约。

TRANSCRIBE_ONLY 落盘、AUTO_EDIT 复用的唯一数据面；纯文件存储，tmp_path 隔离。
本地（local:）key 的源片存在性校验是真实契约：测试用 tmp_path 下真实存在的文件。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import studio_transcript_store as store


@pytest.fixture()
def tasks_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "tasks"
    root.mkdir()
    monkeypatch.setenv("STUDIO_TASKS_ROOT", str(root))
    return root


@pytest.fixture()
def local_key(tmp_path: Path) -> str:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fake video bytes")
    return f"local:{source}"


def _save(key: str, project: str = "timeline_test", text: str = "纠正后文本") -> dict:
    return store.save_corrected_transcript(
        key, project,
        asr_segments=[{"start": 0.0, "end": 1.0, "text": text}],
        raw_segments=[{"start": 0.0, "end": 1.0, "text": "原始文本"}],
        correction_audit=[{"segment_id": "seg_0000", "raw_text": "原始文本"}],
        correction_summary={"engine": "local-conservative-rules"},
        correction_provider="local-conservative-rules",
        language="zh",
        words=[],
    )


def test_save_writes_version_file_and_returns_ids(tasks_root: Path, local_key: str):
    ref = _save(local_key)
    assert ref["transcript_id"] == store.transcript_id_for(local_key)
    assert ref["transcript_version"] == 1
    path = tasks_root / "transcripts" / store.source_id_for(local_key) / "v1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema"] == store.SCHEMA
    assert payload["asr_segments"][0]["text"] == "纠正后文本"
    assert payload["source_project_id"] == "timeline_test"


def test_versions_increment_atomically(tasks_root: Path, local_key: str):
    first = _save(local_key, text="第一版")
    second = _save(local_key, text="第二版")
    assert (first["transcript_version"], second["transcript_version"]) == (1, 2)
    latest = store.load_corrected_transcript(local_key)
    assert latest is not None and latest["transcript_version"] == 2
    assert latest["asr_segments"][0]["text"] == "第二版"
    explicit = store.load_corrected_transcript(local_key, transcript_version=1)
    assert explicit is not None and explicit["asr_segments"][0]["text"] == "第一版"


def test_load_missing_returns_none(tasks_root: Path, tmp_path: Path):
    missing = tmp_path / "never.mp4"
    assert store.load_corrected_transcript(f"local:{missing}") is None


def test_load_corrupt_file_returns_none(tasks_root: Path, local_key: str):
    _save(local_key)
    corrupt = tasks_root / "transcripts" / store.source_id_for(local_key) / "v1.json"
    corrupt.write_text("{不是 JSON", encoding="utf-8")
    assert store.load_corrected_transcript(local_key) is None


def test_load_with_mismatched_transcript_id_returns_none(tasks_root: Path, local_key: str):
    _save(local_key)
    assert store.load_corrected_transcript(local_key, transcript_id="tr-别的源") is None
    assert store.load_corrected_transcript(local_key, transcript_id=store.transcript_id_for(local_key)) is not None


def test_local_source_missing_invalidates(tasks_root: Path, tmp_path: Path):
    missing = tmp_path / "gone.mp4"
    key = f"local:{missing}"
    _save(key)
    assert store.load_corrected_transcript(key) is None
    missing.write_bytes(b"x")
    assert store.load_corrected_transcript(key) is not None


def test_r2_key_skips_local_existence_check(tasks_root: Path):
    key = "studio-source/timeline_x/abc.mp4"
    _save(key)
    assert store.load_corrected_transcript(key) is not None


def test_find_latest_by_project_returns_newest_matching(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_TASKS_ROOT", str(tmp_path))
    source_a = tmp_path / "a.mp4"
    source_b = tmp_path / "b.mp4"
    source_a.write_bytes(b"a")
    source_b.write_bytes(b"b")
    key_a, key_b = f"local:{source_a}", f"local:{source_b}"
    kwargs = dict(raw_segments=[], correction_audit=[], correction_summary={},
                  correction_provider="x", language="zh", words=[])
    store.save_corrected_transcript(key_a, "timeline_p", asr_segments=[
        {"start": 0.0, "end": 1.0, "text": "v1"}], **kwargs)
    store.save_corrected_transcript(key_a, "timeline_p", asr_segments=[
        {"start": 0.0, "end": 1.0, "text": "v2"}], **kwargs)
    store.save_corrected_transcript(key_b, "timeline_q", asr_segments=[
        {"start": 0.0, "end": 1.0, "text": "别的项目"}], **kwargs)

    ref = store.find_latest_by_project("timeline_p")
    assert ref == {"object_key": key_a, "transcript_id": store.transcript_id_for(key_a),
                   "transcript_version": 2}
    assert store.find_latest_by_project("timeline_none") is None


def test_find_latest_by_project_skips_missing_source(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_TASKS_ROOT", str(tmp_path))
    gone = tmp_path / "gone.mp4"
    gone.write_bytes(b"x")
    store.save_corrected_transcript(f"local:{gone}", "timeline_p", asr_segments=[
        {"start": 0.0, "end": 1.0, "text": "v1"}],
        raw_segments=[], correction_audit=[], correction_summary={},
        correction_provider="x", language="zh", words=[])
    gone.unlink()  # 源片被清理后，该转写不再可用
    assert store.find_latest_by_project("timeline_p") is None
