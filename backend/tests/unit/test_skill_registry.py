"""剪辑规则注册表 + Skill 适配器测试（纯逻辑，不连数据库、不调真实服务）。

覆盖：
- 清单解析/校验（缺失、损坏、校验和不匹配→stale、禁用跳过、schema 非法）；
- 任务级规则快照落盘与读回；
- 规则优先级四层覆盖（用户指令 > 项目设置 > Skill > 默认）；
- 适配器 schema 声明、PIPELINE_ORDER 完整性、未实现适配器显式报错；
- 「规则来自 manifest」：改清单参数 → 适配器实际下发的载荷随之变化。
"""

from __future__ import annotations

import hashlib
import json

import pytest

from src import skill_registry as registry
from src import skills


def _sha16(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _write_manifest(tmp_path, *, checksum_override: str | None = None, cover_style: str = "realframe_v2"):
    """构造一份最小可用清单（含真实文件与校验和）。"""
    skill_file = tmp_path / "cover_styles.json"
    skill_file.write_text('{"default_style_id": "%s"}' % cover_style, encoding="utf-8")
    manifest = {
        "workflow_version": "test-1",
        "enabled": True,
        "updated_at": "2026-10-12T00:00:00Z",
        "skills": [
            {
                "skill_id": "cover",
                "version": "v1",
                "source_path": str(skill_file),
                "purpose": "test",
                "priority": 20,
                "enabled": True,
                "checksum": checksum_override if checksum_override is not None else _sha16(skill_file),
                "input_schema": {"type": "object", "required": ["clip_id"]},
                "output_schema": {"type": "object", "required": ["cover_jpg"]},
            },
            {
                "skill_id": "clip_planning",
                "version": "v1",
                "source_path": str(skill_file),
                "purpose": "test",
                "priority": 10,
                "enabled": True,
                "checksum": _sha16(skill_file),
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
            },
            {
                "skill_id": "disabled_one",
                "version": "v1",
                "source_path": str(skill_file),
                "priority": 1,
                "enabled": False,
                "checksum": _sha16(skill_file),
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
            },
        ],
        "cover_rules": {"skill_id": "cover", "default_style_id": cover_style, "intro_duration_s": 0.8},
        "clip_planning_rules": {"duration_range_default": [25, 45], "llm_enabled": True},
        "qa_rules": {"fail_action": "reject_clip"},
    }
    path = tmp_path / "editing-workflow.manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return path, manifest


def test_load_and_validate_ok(tmp_path, monkeypatch):
    path, _ = _write_manifest(tmp_path)
    monkeypatch.setenv("EDITING_WORKFLOW_MANIFEST", str(path))
    manifest = registry.load_manifest()
    report = registry.validate_manifest(manifest)
    assert report.ok, report.errors
    statuses = {item.skill_id: item.status for item in report.skills}
    assert statuses["cover"] == "ok"
    assert statuses["disabled_one"] == "disabled"  # 禁用项被跳过校验


def test_missing_manifest_raises_machine_code(tmp_path, monkeypatch):
    monkeypatch.setenv("EDITING_WORKFLOW_MANIFEST", str(tmp_path / "nope.json"))
    with pytest.raises(registry.SkillRegistryError) as exc:
        registry.load_manifest()
    assert exc.value.code == registry.E_MANIFEST_MISSING


def test_corrupt_manifest_raises(tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("EDITING_WORKFLOW_MANIFEST", str(bad))
    with pytest.raises(registry.SkillRegistryError) as exc:
        registry.load_manifest()
    assert exc.value.code == registry.E_MANIFEST_INVALID


def test_checksum_mismatch_marks_stale_but_not_fatal(tmp_path, monkeypatch):
    path, _ = _write_manifest(tmp_path, checksum_override="deadbeefdeadbeef")
    monkeypatch.setenv("EDITING_WORKFLOW_MANIFEST", str(path))
    report = registry.validate_manifest()
    assert report.ok  # 校验和不符只告警，不阻断任务
    assert "cover" in report.stale_skills
    assert any("校验和不匹配" in item for item in report.warnings)


def test_invalid_schema_shape_is_error(tmp_path, monkeypatch):
    path, manifest = _write_manifest(tmp_path)
    manifest["skills"][0]["input_schema"] = {"required": "should-be-list"}
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("EDITING_WORKFLOW_MANIFEST", str(path))
    report = registry.validate_manifest()
    assert not report.ok
    assert any("schema 形状非法" in item for item in report.errors)


def test_snapshot_written_and_readable(tmp_path, monkeypatch):
    path, _ = _write_manifest(tmp_path)
    monkeypatch.setenv("EDITING_WORKFLOW_MANIFEST", str(path))
    snap_dir = tmp_path / "job"
    snapshot = registry.build_workflow_snapshot("task-1", snapshot_dir=snap_dir)
    assert snapshot["workflow_version"] == "test-1"
    assert snapshot["rules"]["cover_rules"]["default_style_id"] == "realframe_v2"
    assert snapshot["rules_source"]["cover_rules"] == registry.SOURCE_SKILL
    # 禁用项不进快照；启用项按 priority 升序（clip_planning=10 先于 cover=20）
    assert [item["skill_id"] for item in snapshot["skills"]] == ["clip_planning", "cover"]
    assert (snap_dir / registry.SNAPSHOT_FILENAME).is_file()
    again = registry.read_workflow_snapshot(snap_dir)
    assert again and again["task_id"] == "task-1"


def test_resolve_rule_priority_order(tmp_path, monkeypatch):
    path, _ = _write_manifest(tmp_path)
    monkeypatch.setenv("EDITING_WORKFLOW_MANIFEST", str(path))
    # 用户指令最高
    value, source = registry.resolve_rule(
        "theme", user_instruction={"theme": "用户"}, project_settings={"theme": "项目"},
        skill_value="skill", default="默认")
    assert (value, source) == ("用户", registry.SOURCE_USER_INSTRUCTION)
    # 项目设置次之
    value, source = registry.resolve_rule(
        "theme", project_settings={"theme": "项目"}, skill_value="skill", default="默认")
    assert (value, source) == ("项目", registry.SOURCE_PROJECT_SETTINGS)
    # 再之已启用 Skill
    value, source = registry.resolve_rule("theme", skill_value="skill", default="默认")
    assert (value, source) == ("skill", registry.SOURCE_SKILL)
    # 最后系统默认（旧硬编码只能待在这一层）
    value, source = registry.resolve_rule("theme", default="默认")
    assert (value, source) == ("默认", registry.SOURCE_SYSTEM_DEFAULT)


def test_describe_active_workflow_is_readonly_summary(tmp_path, monkeypatch):
    path, _ = _write_manifest(tmp_path)
    monkeypatch.setenv("EDITING_WORKFLOW_MANIFEST", str(path))
    summary = registry.describe_active_workflow()
    assert summary["workflow_version"] == "test-1"
    assert summary["skills"][0]["skill_id"] == "cover"
    assert summary["rules"]["cover_rules"]["default_style_id"] == "realframe_v2"


def test_pipeline_order_covers_all_adapters():
    assert len(skills.PIPELINE_ORDER) == 9
    assert set(skills.PIPELINE_ORDER) == set(skills.ADAPTERS)
    # 依赖顺序：转写必须在纠错之前、渲染在封面之前、QA 最后
    order = list(skills.PIPELINE_ORDER)
    assert order.index("transcription") < order.index("transcript_correction")
    assert order.index("clip_planning") < order.index("render")
    assert order[-1] == "quality_assurance"


def test_unimplemented_adapters_fail_explicitly():
    ctx = skills.SkillContext(task_id="t1")
    with pytest.raises(skills.SkillExecutionError) as exc:
        skills.run_speaker_diarization_skill(ctx)
    assert exc.value.code == "E_SKILL_NOT_IMPLEMENTED"
    with pytest.raises(skills.SkillExecutionError):
        skills.run_highlight_selection_skill(ctx)
    with pytest.raises(skills.SkillExecutionError):
        skills.run_subtitle_composition_skill(ctx)


def test_cover_skill_rules_come_from_manifest(tmp_path, monkeypatch):
    """改清单参数 → 适配器下发载荷随之变化（证明规则没写死在代码里）。"""
    path, _ = _write_manifest(tmp_path, cover_style="cutout_v3")
    monkeypatch.setenv("EDITING_WORKFLOW_MANIFEST", str(path))

    captured: dict = {}

    def fake_call(ctx, endpoint, payload=None, **kwargs):
        captured["endpoint"] = endpoint
        captured["payload"] = payload or {}
        return {"cover_jpg_artifact_id": "art-1", "layout": {"style_id": payload.get("style_id")}}

    monkeypatch.setattr("src.skills.cover.call_video_brain", fake_call)
    ctx = skills.SkillContext(task_id="t1", manifest=registry.load_manifest())
    result = skills.run_cover_generation_skill(ctx, clip_id="clip-01", frame_ts=1.5)
    assert captured["endpoint"] == "/v1/tools/cover.render"
    assert captured["payload"]["style_id"] == "cutout_v3"  # 来自清单
    assert result.rules_source["cover_rules.default_style_id"] == registry.SOURCE_SKILL

    # 换成 realframe_v2 后载荷随之变化
    path2, _ = _write_manifest(tmp_path, cover_style="realframe_v2")
    monkeypatch.setenv("EDITING_WORKFLOW_MANIFEST", str(path2))
    ctx2 = skills.SkillContext(task_id="t1", manifest=registry.load_manifest())
    skills.run_cover_generation_skill(ctx2, clip_id="clip-01")
    assert captured["payload"]["style_id"] == "realframe_v2"


def test_planning_skill_uses_manifest_duration_range(tmp_path, monkeypatch):
    path, manifest = _write_manifest(tmp_path)
    manifest["clip_planning_rules"]["duration_range_default"] = [30, 60]
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("EDITING_WORKFLOW_MANIFEST", str(path))

    captured: dict = {}

    def fake_call(ctx, endpoint, payload=None, **kwargs):
        captured["payload"] = payload or {}
        return {"clips": [{"clip_id": "clip-01"}], "validated": True}

    monkeypatch.setattr("src.skills.planning.call_video_brain", fake_call)
    ctx = skills.SkillContext(task_id="t1", manifest=registry.load_manifest())
    skills.run_clip_planning_skill(ctx, segments=[{"segment_id": "1", "start": 0, "end": 30, "text": "x"}])
    assert captured["payload"]["duration_range"] == [30, 60]


def test_task_versions_and_display_line(tmp_path, monkeypatch):
    """Phase B 接线契约：任务记录字段与成片详情的版本展示行。"""
    path, _ = _write_manifest(tmp_path)
    monkeypatch.setenv("EDITING_WORKFLOW_MANIFEST", str(path))
    snap = registry.build_workflow_snapshot("task-9", snapshot_dir=tmp_path / "job")

    versions = registry.task_versions(snap)
    assert versions["workflow_version"] == "test-1"
    assert versions["skill_versions"] == {"clip_planning": "v1", "cover": "v1"}
    assert versions["snapshot_path"].endswith(registry.SNAPSHOT_FILENAME)

    line = registry.workflow_version_line(snap)
    assert line.startswith("使用规则版本：workflow vtest-1")
    assert "cover v1" in line
    assert "legacy_default" not in line  # 最低优先级默认层不进展示行
    assert "未知" in registry.workflow_version_line(None)


def test_active_workflow_banner(tmp_path, monkeypatch):
    path, _ = _write_manifest(tmp_path)
    monkeypatch.setenv("EDITING_WORKFLOW_MANIFEST", str(path))
    banner = registry.active_workflow_banner()
    assert banner["current_version"] == "test-1"
    assert "vtest-1" in banner["label"]
    assert banner["update_hint"] == ""  # 校验和一致 → 无新版本提示

    stale_path, _ = _write_manifest(tmp_path, checksum_override="deadbeefdeadbeef")
    monkeypatch.setenv("EDITING_WORKFLOW_MANIFEST", str(stale_path))
    stale = registry.active_workflow_banner()
    assert "检测到新规则版本" in stale["update_hint"]
    assert "cover" in stale["stale_skills"]


def test_attach_snapshot_to_payload_is_non_destructive(tmp_path, monkeypatch):
    path, _ = _write_manifest(tmp_path)
    monkeypatch.setenv("EDITING_WORKFLOW_MANIFEST", str(path))
    snap = registry.build_workflow_snapshot("task-9", snapshot_dir=tmp_path / "job")
    original = {"source_video": "/x.mp4"}
    merged = registry.attach_snapshot_to_payload(original, snap)
    assert "workflow_snapshot" not in original  # 不改原对象
    assert merged["workflow_snapshot"]["workflow_version"] == "test-1"
    assert merged["workflow_snapshot"]["skills"][0]["skill_id"] in {"clip_planning", "cover"}
    assert merged["source_video"] == "/x.mp4"
