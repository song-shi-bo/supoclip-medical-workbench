"""时间线项目载入的转写回读回归测试（Step 4 / 2026-10-16）。

场景：工作台导入会**先创建时间线项目**（内嵌转写快照为空段），转写/纠错完成后
打开历史项目必须显示转写库中**当前有效版本**（模型纠错后的文本）——
此前 load_timeline_project 不回读转写库，打开后转写面板为空（旧文本/无文本）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import timeline_copilot as tc


@pytest.fixture
def tasks_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "tasks"
    monkeypatch.setenv("STUDIO_TASKS_ROOT", str(root))
    return root


def _make_project(uploads_root: Path, project_id: str, source_stem: str = "video") -> Path:
    """生产同款最小项目结构：project.json + 空 EDL + 空转写快照（导入期形态）。"""
    project_dir = uploads_root / "studio-projects" / project_id
    edit_transcripts = project_dir / "edit" / "transcripts"
    edit_transcripts.mkdir(parents=True)
    (project_dir / "project.json").write_text(json.dumps({
        "id": project_id, "source_stem": source_stem,
        "source": {"name": f"{source_stem}.mp4"}, "artifacts": {},
    }, ensure_ascii=False), encoding="utf-8")
    (project_dir / "edit" / "edl.json").write_text(
        json.dumps({"timelines": [{"id": "t001", "ranges": []}]}), encoding="utf-8")
    (edit_transcripts / f"{source_stem}.json").write_text(
        json.dumps({"segments": []}, ensure_ascii=False), encoding="utf-8")
    return project_dir


def _plant_transcript(tasks_root: Path, uploads_root: Path, project_id: str,
                      upload_id: str, text: str) -> None:
    """按生产同款路径植入纠错转写：local object_key + 源文件存在（存在性守卫通过）。"""
    from src import studio_transcript_store as sts

    uploads_root.mkdir(parents=True, exist_ok=True)
    object_key = f"local:{uploads_root}/{upload_id}.mp4"
    Path(object_key[len("local:"):]).write_bytes(b"0")
    ref = sts.save_corrected_transcript(
        object_key, project_id,
        asr_segments=[{"segment_id": "seg_0000", "start": 0.1, "end": 2.0,
                       "text": text, "speaker": "未标注",
                       "correction_status": "corrected"}],
        raw_segments=[], correction_audit=[], correction_summary={},
        correction_provider="dsh-llm:openai:glm-5.3-flash", language="zh", words=[],
    )
    assert ref["transcript_version"] >= 1


def test_load_timeline_project_reads_latest_transcript_when_snapshot_empty(
        tasks_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """内嵌快照为空 → 回读转写库最新版本（模型纠错后的当前有效文本）。"""
    uploads_root = tmp_path / "uploads"
    _make_project(uploads_root, "timeline_ab12cd34ef56")
    _plant_transcript(tasks_root, uploads_root, "timeline_ab12cd34ef56",
                      "9a3090f5", "模型纠错后的正确文本")

    payload = tc.load_timeline_project(
        project_id="timeline_ab12cd34ef56", uploads_root=uploads_root)

    assert len(payload["transcript"]) == 1
    assert payload["transcript"][0]["text"] == "模型纠错后的正确文本"
    assert payload["transcript"][0]["correction_status"] == "corrected"
    assert payload["transcript_source"]["transcript_version"] == 1
    assert "glm-5.3-flash" in payload["transcript_source"]["provider"]


def test_load_timeline_project_prefers_existing_snapshot(
        tasks_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """内嵌快照非空时**不回读**——保持既有行为（快照是时间线编辑的事实来源）。"""
    uploads_root = tmp_path / "uploads"
    project_dir = _make_project(uploads_root, "timeline_ab12cd34ef56")
    snap = project_dir / "edit" / "transcripts" / "video.json"
    snap.write_text(json.dumps({"segments": [
        {"segment_id": "seg_0001", "start": 0.0, "end": 1.0, "text": "快照文本"}]}),
        encoding="utf-8")
    _plant_transcript(tasks_root, uploads_root, "timeline_ab12cd34ef56",
                      "9a3090f5", "转写库文本")

    payload = tc.load_timeline_project(
        project_id="timeline_ab12cd34ef56", uploads_root=uploads_root)

    assert payload["transcript"][0]["text"] == "快照文本"
    assert payload.get("transcript_source") is None


def test_load_timeline_project_empty_without_any_transcript(
        tasks_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """转写库也没有 → 保持空段（行为不变，不虚构）。"""
    uploads_root = tmp_path / "uploads"
    uploads_root.mkdir()
    _make_project(uploads_root, "timeline_ab12cd34ef56")

    payload = tc.load_timeline_project(
        project_id="timeline_ab12cd34ef56", uploads_root=uploads_root)

    assert payload["transcript"] == []
