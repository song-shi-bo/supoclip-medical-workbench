"""封面版型库（2026-10-21 阶段）定向测试：版本化注册表 + 每切片选择持久化 + 模拟 stub。

覆盖：
* 注册表形状：四版型（可爱/酷炫/动漫排版/黄色大字）、id 唯一、名称、示例图、稳定排序、
  REGISTRY_VERSION；validate_template_id 的未知/非法分支；
* mock_result：确定性（同 id 同输出）、mock=true、明确「不调用模型」文案、未知 404 码；
* store.set_cover_template：每条成片各自持有（改 A 不触碰 B —— 不串片的服务端不变量）、
  切换/清除、幂等、未注册模板拒绝、未登记 clip 拒绝、删除期拒绝、card_view 透出；
* 路由注册与端点协程：GET 列表、PUT 成功/未知模板 404/未登记 404/删除期 409、
  POST mock-result 成功与未知 404。

全部 tmp 根合成数据；不调用模型、不生成任何封面文件。
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from src import cover_templates as registry
from src import studio_access as sa
from src import studio_project_store as store
from src.api.routes import media
from src.studio_access import AccessLedger

PID = "timeline_coverlib01"
TASK = "autoedit_aaaabbbbcccc"
CLIP_A = "clip-aaaaaaaaaa"
CLIP_B = "clip-bbbbbbbbbb"


def principal(subject="8"):
    import time
    return {"subject": subject, "slug": subject, "domain_id": sa.DOMAIN_ID,
            "expires_at_ms": int(time.time() * 1000) + 60000}


@pytest.fixture
def env(tmp_path, monkeypatch):
    tasks = tmp_path / "tasks"
    (tasks / "projects").mkdir(parents=True)
    monkeypatch.setattr(store, "TASKS_ROOT", tasks)
    acl = AccessLedger(tmp_path / "acl.sqlite3")
    acl.register("project", PID, principal("8"))
    return {"tasks": tasks, "acl": acl}


def register_clip(clip_id: str) -> dict:
    return {"clip_id": clip_id, "name": clip_id, "title": clip_id,
            "render_task_id": TASK, "render_revision": 1, "cover": None}


def save_ledger(env, entries: list[dict], *, status: str = "active") -> None:
    manifest = store.load_manifest(PID)
    manifest["clips"] = {e["clip_id"]: e for e in entries}
    manifest["clip_order"] = [e["clip_id"] for e in entries]
    manifest["status"] = status
    store.save_manifest(PID, manifest)


# ---------------------------------------------------------------------------
# 注册表（版本化、唯一事实源）
# ---------------------------------------------------------------------------

def test_registry_shape_four_templates():
    payload = registry.list_templates()
    assert payload["registry_version"] == registry.REGISTRY_VERSION
    templates = payload["templates"]
    assert [t["name"] for t in templates] == ["可爱版", "酷炫版", "动漫排版", "黄色大字"]
    ids = [t["template_id"] for t in templates]
    assert len(ids) == len(set(ids)) == 4
    assert [t["sort"] for t in templates] == sorted(t["sort"] for t in templates)
    for t in templates:
        assert t["template_id"] and t["version"] and t["description"]
        assert t["example_path"].startswith("/studio/cover-templates/") and t["example_path"].endswith("/example.svg")


def test_validate_template_id_branches():
    assert registry.validate_template_id(" cute ") == "cute"
    with pytest.raises(registry.CoverTemplateError) as ei:
        registry.validate_template_id("kawaii")
    assert ei.value.code == "E_TEMPLATE_UNKNOWN"
    with pytest.raises(registry.CoverTemplateError) as ei:
        registry.validate_template_id("../evil")
    assert ei.value.code == "E_TEMPLATE_ID_INVALID"
    with pytest.raises(registry.CoverTemplateError):
        registry.validate_template_id("")


def test_mock_result_deterministic_and_honest():
    first = registry.mock_result("cute")
    again = registry.mock_result("cute")
    assert first == again  # 确定性：同 id 同输出
    assert first["mock"] is True
    assert first["template_id"] == "cute" and first["template_name"] == "可爱版"
    assert first["registry_version"] == registry.REGISTRY_VERSION
    assert "模拟" in first["result_text"] and "不调用模型" in first["result_text"]
    with pytest.raises(registry.CoverTemplateError) as ei:
        registry.mock_result("nope")
    assert ei.value.code == "E_TEMPLATE_UNKNOWN"


# ---------------------------------------------------------------------------
# store：每条成片各自持有（不串片）、幂等、校验
# ---------------------------------------------------------------------------

def test_set_cover_template_per_clip_isolation(env):
    save_ledger(env, [register_clip(CLIP_A), register_clip(CLIP_B)])
    store.set_cover_template(PID, CLIP_A, "cute")
    store.set_cover_template(PID, CLIP_B, "yellow_bold")
    ledger = json.loads((env["tasks"] / "projects" / PID / "clips.json").read_text(encoding="utf-8"))
    assert ledger["clips"][CLIP_A]["cover_template"]["template_id"] == "cute"
    assert ledger["clips"][CLIP_B]["cover_template"]["template_id"] == "yellow_bold"
    # 再改 A：B 的选择一个字节都不能动（不串片）
    before_b = json.dumps(ledger["clips"][CLIP_B], sort_keys=True)
    store.set_cover_template(PID, CLIP_A, "cool")
    ledger2 = json.loads((env["tasks"] / "projects" / PID / "clips.json").read_text(encoding="utf-8"))
    assert json.dumps(ledger2["clips"][CLIP_B], sort_keys=True) == before_b
    assert ledger2["clips"][CLIP_A]["cover_template"]["template_id"] == "cool"
    # 卡片透出：刷新恢复的事实源
    cards = {c["clip_id"]: c for c in store.list_cards(PID)}
    assert cards[CLIP_A]["cover_template"]["template_id"] == "cool"
    assert cards[CLIP_B]["cover_template"]["template_id"] == "yellow_bold"


def test_set_cover_template_switch_clear_idempotent(env):
    save_ledger(env, [register_clip(CLIP_A)])
    first = store.set_cover_template(PID, CLIP_A, "anime_layout")
    assert first["changed"] is True and first["cover_template"]["registry_version"] == registry.REGISTRY_VERSION
    again = store.set_cover_template(PID, CLIP_A, "anime_layout")
    assert again["changed"] is False
    assert again["cover_template"]["updated_at"] == first["cover_template"]["updated_at"]
    cleared = store.set_cover_template(PID, CLIP_A, None)
    assert cleared["changed"] is True and cleared["cover_template"] is None
    led = json.loads((env["tasks"] / "projects" / PID / "clips.json").read_text(encoding="utf-8"))
    assert "cover_template" not in led["clips"][CLIP_A]
    assert store.list_cards(PID)[0]["cover_template"] is None


def test_set_cover_template_rejections(env):
    save_ledger(env, [register_clip(CLIP_A)])
    with pytest.raises(registry.CoverTemplateError) as ei:
        store.set_cover_template(PID, CLIP_A, "kawaii")
    assert ei.value.code == "E_TEMPLATE_UNKNOWN"
    with pytest.raises(store.ProjectStoreError) as ei:
        store.set_cover_template(PID, "clip-ghost", "cute")
    assert ei.value.code == "E_CLIP_NOT_REGISTERED"
    # 失败不留半写状态
    assert store.load_manifest(PID)["clips"][CLIP_A].get("cover_template") is None
    # 删除期拒绝
    save_ledger(env, [register_clip(CLIP_A)], status="deleting")
    with pytest.raises(store.ProjectStoreError) as ei:
        store.set_cover_template(PID, CLIP_A, "cute")
    assert ei.value.code == "E_PROJECT_NOT_MUTABLE"


# ---------------------------------------------------------------------------
# 路由层
# ---------------------------------------------------------------------------

def test_routes_registered():
    paths = set()
    for route in media.router.routes:
        for method in (getattr(route, "methods", None) or set()):
            paths.add((method, getattr(route, "path", "")))
    assert ("GET", "/studio/cover-templates") in paths
    assert ("PUT", "/studio/projects/{project_id}/clips/{clip_id}/cover-template") in paths
    assert ("POST", "/studio/cover-templates/{template_id}/mock-result") in paths


@pytest.fixture
def no_owner_check(monkeypatch):
    monkeypatch.setattr(media, "_assert_project_owner", lambda request, project_id: None)


@pytest.mark.asyncio
async def test_endpoints_list_and_put(env, no_owner_check):
    payload = await media.studio_cover_templates()
    assert len(payload["templates"]) == 4
    save_ledger(env, [register_clip(CLIP_A)])
    response = await media.studio_clip_set_cover_template(
        PID, CLIP_A, None, media.CoverTemplateSelectRequest(template_id="cute"))
    assert response["cover_template"]["template_id"] == "cute" and response["changed"] is True
    assert store.list_cards(PID)[0]["cover_template"]["template_id"] == "cute"
    # 未知模板 → 404 带码；未登记 clip → 404
    with pytest.raises(HTTPException) as ei:
        await media.studio_clip_set_cover_template(
            PID, CLIP_A, None, media.CoverTemplateSelectRequest(template_id="kawaii"))
    assert ei.value.status_code == 404 and ei.value.detail["code"] == "E_TEMPLATE_UNKNOWN"
    with pytest.raises(HTTPException) as ei:
        await media.studio_clip_set_cover_template(
            PID, "clip-ghost", None, media.CoverTemplateSelectRequest(template_id="cute"))
    assert ei.value.status_code == 404 and ei.value.detail["code"] == "E_CLIP_NOT_REGISTERED"


@pytest.mark.asyncio
async def test_endpoint_put_409_while_deleting(env, no_owner_check):
    save_ledger(env, [register_clip(CLIP_A)], status="deleting")
    with pytest.raises(HTTPException) as ei:
        await media.studio_clip_set_cover_template(
            PID, CLIP_A, None, media.CoverTemplateSelectRequest(template_id="cute"))
    assert ei.value.status_code == 409 and ei.value.detail["code"] == "E_PROJECT_NOT_MUTABLE"


@pytest.mark.asyncio
async def test_endpoint_mock_result_and_unknown(env):
    payload = await media.studio_cover_template_mock_result("yellow_bold")
    assert payload["mock"] is True and payload["template_name"] == "黄色大字"
    with pytest.raises(HTTPException) as ei:
        await media.studio_cover_template_mock_result("nope")
    assert ei.value.status_code == 404 and ei.value.detail["code"] == "E_TEMPLATE_UNKNOWN"
