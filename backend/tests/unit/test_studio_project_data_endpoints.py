"""清空项目任务数据的 HTTP 端点测试（纯文件系统、不联 DB、不跑 ffmpeg）。

覆盖三件事：

1. **路由注册**：``GET /studio/projects/{id}/data`` 与
   ``POST /studio/projects/{id}/data/clear`` 都挂在 media router 上（少一个前端就 404）；
2. **错误映射**：活动任务 / 卡片数变化 → **409**（前端据此提示刷新重试），
   缺 confirm / 缺 expected_card_count → 422，错误码原样透出可排障；
3. **行为**：预览是纯只读；清空后卡片为 0、任务文件消失、源片与转写保留。
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from src import studio_project_purge as purge
from src import studio_project_store as store
from src.api.routes import media

PROJECT_ID = "timeline_abc123def456"
TASK = "autoedit_0123456789ab"


def test_clear_routes_are_registered():
    registered = {(method, route.path) for route in media.router.routes
                  for method in getattr(route, "methods", set())}
    assert ("GET", "/studio/projects/{project_id}/data") in registered
    assert ("POST", "/studio/projects/{project_id}/data/clear") in registered


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "TASKS_ROOT", tmp_path / "tasks")
    root = store.TASKS_ROOT
    (root / "completed").mkdir(parents=True)
    (root / "status").mkdir(parents=True)
    (root / "completed" / f"{TASK}.json").write_text(json.dumps({
        "task_id": TASK, "status": "completed",
        "bridge": {"project_id": PROJECT_ID}, "raw_video_path": f"media/{TASK}/source.mp4",
    }), encoding="utf-8")
    (root / "status" / f"{TASK}.json").write_text(
        json.dumps({"task_id": TASK, "status": "completed"}), encoding="utf-8")
    clip_dir = root / "output" / TASK / "clip-001"
    clip_dir.mkdir(parents=True)
    (clip_dir / "clip_final.mp4").write_bytes(b"v" * 1024)
    (root / "media" / TASK).mkdir(parents=True)
    (root / "media" / TASK / "source.mp4").write_bytes(b"s" * 1024)
    (root / "transcripts" / "src-1").mkdir(parents=True)
    (root / "transcripts" / "src-1" / "v1.json").write_text("{}", encoding="utf-8")
    store.register_source_video(PROJECT_ID, path=f"media/{TASK}/source.mp4")
    store.register_clips(PROJECT_ID, TASK, [{"clip_id": "clip-001", "title": "主题",
                                             "duration": 30.0}])
    return root


async def test_data_preview_is_read_only(seeded):
    payload = await media.studio_project_data(PROJECT_ID)
    assert payload["card_count"] == 1
    assert payload["reclaimable_bytes"] > 0
    assert payload["keep"]["source_video"] == f"media/{TASK}/source.mp4"
    assert (seeded / "output" / TASK / "clip-001" / "clip_final.mp4").is_file()


async def test_clear_endpoint_removes_cards_keeps_source(seeded):
    payload = await media.studio_clear_project_data(
        PROJECT_ID, media.ProjectDataClearRequest(confirm=True, expected_card_count=1))

    assert payload["cleared_card_count"] == 1
    assert store.card_clip_ids(PROJECT_ID) == []
    assert not (seeded / "completed" / f"{TASK}.json").exists()
    assert not (seeded / "output" / TASK).exists()
    assert (seeded / "media" / TASK / "source.mp4").is_file()
    assert (seeded / "transcripts" / "src-1" / "v1.json").is_file()


async def test_clear_endpoint_requires_confirmation(seeded):
    with pytest.raises(HTTPException) as excinfo:
        await media.studio_clear_project_data(
            PROJECT_ID, media.ProjectDataClearRequest(confirm=False, expected_card_count=1))
    assert excinfo.value.status_code == 422
    assert purge.E_PURGE_CONFIRM_REQUIRED in str(excinfo.value.detail)
    assert store.card_clip_ids(PROJECT_ID) == ["clip-001"]


async def test_clear_endpoint_requires_expected_card_count(seeded):
    with pytest.raises(HTTPException) as excinfo:
        await media.studio_clear_project_data(
            PROJECT_ID, media.ProjectDataClearRequest(confirm=True, expected_card_count=None))
    assert excinfo.value.status_code == 422
    assert purge.E_PURGE_CARD_COUNT_MISMATCH in str(excinfo.value.detail)


async def test_card_count_mismatch_maps_to_409(seeded):
    with pytest.raises(HTTPException) as excinfo:
        await media.studio_clear_project_data(
            PROJECT_ID, media.ProjectDataClearRequest(confirm=True, expected_card_count=5))
    assert excinfo.value.status_code == 409, "卡片数变了要 409，让前端刷新后重看一遍"
    assert excinfo.value.detail["current_card_count"] == 1
    assert store.card_clip_ids(PROJECT_ID) == ["clip-001"]


async def test_active_task_maps_to_409(seeded):
    other = "autoedit_abcdefabcdef"
    (seeded / "processing").mkdir(parents=True, exist_ok=True)
    (seeded / "processing" / f"{other}.json").write_text(json.dumps({
        "task_id": other, "status": "processing", "bridge": {"project_id": PROJECT_ID},
    }), encoding="utf-8")
    (seeded / "status" / f"{other}.json").write_text(
        json.dumps({"task_id": other, "status": "processing"}), encoding="utf-8")
    store.register_clips(PROJECT_ID, other, [{"clip_id": "clip-002", "title": "二",
                                              "duration": 30.0}])

    with pytest.raises(HTTPException) as excinfo:
        await media.studio_clear_project_data(
            PROJECT_ID, media.ProjectDataClearRequest(confirm=True, expected_card_count=2))

    assert excinfo.value.status_code == 409
    assert other in excinfo.value.detail["active_task_ids"]
    assert (seeded / "output" / TASK / "clip-001" / "clip_final.mp4").is_file()


async def test_data_preview_rejects_bad_project_id():
    with pytest.raises(HTTPException) as excinfo:
        await media.studio_project_data("not-a-project")
    assert excinfo.value.status_code == 422
