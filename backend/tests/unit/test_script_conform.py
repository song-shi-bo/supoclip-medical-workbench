"""script_conform（对稿成片 P0 接线）单元测试。

纯单元：monkeypatch _load_candidates/_auto_link，不连库。覆盖：
* 非 script_mode → None（不进阶段，老工作流零开销）；
* 匹配达标 → 自动挂链 + 报告落盘（STUDIO_TASKS_ROOT=tmp）；
* 覆盖率不达标 → 不挂链 + 回退警告；
* 文稿库为空 / 转写无分段 → 降级警告不阻断；
* 候选加载异常 → 降级警告不抛出。
"""

from src import script_conform as sc

SCRIPT = {
    "id": "s-001", "topic_id": "t-001", "title": "免疫治疗副作用别急着停药", "version": 2,
    "hook": "免疫治疗出现副作用，先别急着停药。",
    "body": "很多患者一看不良反应就自己把药停了。其实大多数副作用是可控的。关键是第一时间告诉你的主治医生。",
    "cta": "具体问题，评论区留言。",
}

TRANSCRIPT = [
    {"segment_id": 0, "start_ms": 0, "end_ms": 4000, "corrected_text": "免疫治疗出现副作用，先别急着停药。"},
    {"segment_id": 1, "start_ms": 4000, "end_ms": 9000, "corrected_text": "很多患者一看不良反应就自己把药停了。"},
    {"segment_id": 2, "start_ms": 9000, "end_ms": 14000, "corrected_text": "其实大多数副作用是可控的。"},
    {"segment_id": 3, "start_ms": 14000, "end_ms": 20000, "corrected_text": "关键是第一时间告诉你的主治医生。"},
    {"segment_id": 4, "start_ms": 20000, "end_ms": 24000, "corrected_text": "具体问题，评论区留言。"},
]


def test_noop_without_script_mode(monkeypatch):
    called = {"match": False}

    def _fail():
        called["match"] = True
        raise AssertionError("非对稿模式不应加载文稿库")
    monkeypatch.setattr(sc, "_load_candidates", _fail)
    assert sc.run_script_match_stage({"config": {}}, "j1", {"asr_segments": TRANSCRIPT}) is None
    assert called["match"] is False


def test_match_success_links_script(monkeypatch, tmp_path):
    monkeypatch.setenv("STUDIO_TASKS_ROOT", str(tmp_path))
    links = []
    monkeypatch.setattr(sc, "_load_candidates", lambda: [SCRIPT])
    monkeypatch.setattr(sc, "_auto_link",
                        lambda sid, tid, jid: links.append((sid, tid, jid)) or True)
    record = sc.run_script_match_stage({"config": {"script_mode": True}}, "job-align-1",
                                       {"asr_segments": TRANSCRIPT})
    assert record is not None
    assert record["is_script_shot"] is True
    assert record["script_linked"] is True
    assert record["matched_script"]["id"] == "s-001"
    assert links == [("s-001", "t-001", "job-align-1")]
    artifact = tmp_path / "alignment" / "job-align-1.json"
    assert artifact.is_file()
    assert record["alignment_artifact_path"] == str(artifact)
    assert len(record["extra_segments"]) == 0


def test_low_coverage_degrades_without_link(monkeypatch, tmp_path):
    monkeypatch.setenv("STUDIO_TASKS_ROOT", str(tmp_path))
    monkeypatch.setattr(sc, "_load_candidates", lambda: [SCRIPT])
    monkeypatch.setattr(sc, "_auto_link",
                        lambda sid, tid, jid: (_ for _ in ()).throw(AssertionError("不该挂链")))
    unrelated = [{"segment_id": 0, "start_ms": 0, "end_ms": 8000,
                  "corrected_text": "今天聊聊肺癌筛查的低剂量螺旋CT。"}]
    record = sc.run_script_match_stage({"config": {"script_mode": True}}, "job-low",
                                       {"asr_segments": unrelated})
    assert record["script_linked"] is False
    assert record["is_script_shot"] is False
    assert any("未达阈值" in w for w in record["warnings"])


def test_empty_library_degrades(monkeypatch, tmp_path):
    monkeypatch.setenv("STUDIO_TASKS_ROOT", str(tmp_path))
    monkeypatch.setattr(sc, "_load_candidates", lambda: [])
    record = sc.run_script_match_stage({"config": {"script_mode": True}}, "job-empty",
                                       {"asr_segments": TRANSCRIPT})
    assert any("文稿库无已批准原创稿" in w for w in record["warnings"])
    assert (tmp_path / "alignment" / "job-empty.json").is_file()


def test_no_segments_degrades(monkeypatch, tmp_path):
    monkeypatch.setenv("STUDIO_TASKS_ROOT", str(tmp_path))
    monkeypatch.setattr(sc, "_load_candidates",
                        lambda: (_ for _ in ()).throw(AssertionError("无分段不应加载文稿库")))
    record = sc.run_script_match_stage({"config": {"script_mode": True}}, "job-noseg",
                                       {"asr_segments": []})
    assert any("转写无分段" in w for w in record["warnings"])


def test_candidate_load_failure_degrades(monkeypatch, tmp_path):
    monkeypatch.setenv("STUDIO_TASKS_ROOT", str(tmp_path))

    def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(sc, "_load_candidates", _boom)
    record = sc.run_script_match_stage({"config": {"script_mode": True}}, "job-err",
                                       {"asr_segments": TRANSCRIPT})
    assert record is not None
    assert any("匹配阶段异常" in w for w in record["warnings"])
    assert (tmp_path / "alignment" / "job-err.json").is_file()
