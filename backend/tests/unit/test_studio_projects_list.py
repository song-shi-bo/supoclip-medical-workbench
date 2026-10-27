"""项目管理弹层列表聚合的单元测试（纯函数，不碰 request/PG/ffmpeg）。

覆盖 2026-10-19 新增 ``GET /studio/projects/list`` 的核心拼装逻辑：
搜索（project_id/名称子串、大小写不敏感）、排序（更新时间/名称/成片数）、
分页（page/page_size 边界钳制）以及路由注册。可见性过滤复用 W07
``AccessLedger.filter_visible``（其自身契约在 test_studio_access.py 锁定），
状态来自 PG ``studio_jobs``（DISTINCT ON 最新一条），此处用合成字典代替。
"""

from __future__ import annotations

import pytest

from src.api.routes import media

PROJECT_ID = "timeline_abc123def456"
TASK = "a" * 32

RECORDS = [
    {"project_id": "timeline_aaa", "card_count": 3, "updated_at": "2026-10-19T05:57:20"},
    {"project_id": "timeline_bbb", "card_count": 1, "updated_at": "2026-10-18T01:00:00"},
    {"project_id": "timeline_ccc", "card_count": 0, "updated_at": "2026-10-17T01:00:00"},
]
NAMES = {
    "timeline_aaa": {"name": "血压管理.mp4", "has_project": True, "has_source": True},
    "timeline_bbb": {"name": None, "has_project": False, "has_source": False},
    "timeline_ccc": {"name": None, "has_project": True, "has_source": False},
}
STATUS = {"timeline_bbb": {"status": "transcript_ready", "stage": "转写与纠错已完成"}}


def test_list_route_is_registered():
    registered = {(method, route.path) for route in media.router.routes
                  for method in getattr(route, "methods", set())}
    assert ("GET", "/studio/projects/list") in registered
    assert ("GET", "/studio/projects/{project_id}/thumbnail.jpg") in registered


def test_assemble_defaults_and_fields():
    payload = media._assemble_project_items(RECORDS, NAMES, STATUS, q="", order="updated_desc",
                                            page=1, page_size=12)
    assert payload["total"] == 3 and payload["page"] == 1 and payload["page_size"] == 12
    assert [item["project_id"] for item in payload["items"]] == \
        ["timeline_aaa", "timeline_bbb", "timeline_ccc"]
    first = payload["items"][0]
    assert first["name"] == "血压管理.mp4" and first["card_count"] == 3
    assert first["openable"] is True and first["has_source"] is True
    second = payload["items"][1]
    assert second["name"] is None and second["status"] == "transcript_ready"
    # 无 timeline 工程的孤儿台账：openable=False（前端置灰，不再让用户点了才发现 404）。
    assert second["openable"] is False and second["has_source"] is False
    third = payload["items"][2]
    assert third["openable"] is True and third["has_source"] is False  # 有工程、源片已被清理
    assert payload["items"][2]["status"] is None  # 无任务项目：null，前端显示未开始


def test_assemble_search_matches_id_or_name_case_insensitive():
    by_name = media._assemble_project_items(RECORDS, NAMES, {}, q="血压", order="updated_desc",
                                            page=1, page_size=12)
    assert [item["project_id"] for item in by_name["items"]] == ["timeline_aaa"]
    by_id = media._assemble_project_items(RECORDS, NAMES, {}, q="TIMELINE_CCC",
                                          order="updated_desc", page=1, page_size=12)
    assert [item["project_id"] for item in by_id["items"]] == ["timeline_ccc"]
    empty = media._assemble_project_items(RECORDS, NAMES, {}, q="  ", order="updated_desc",
                                          page=1, page_size=12)
    assert empty["total"] == 3  # 空白关键词 = 不过滤


def test_assemble_order_and_pagination():
    asc = media._assemble_project_items(RECORDS, NAMES, {}, q="", order="updated_asc",
                                        page=1, page_size=12)
    assert [item["project_id"] for item in asc["items"]] == \
        ["timeline_ccc", "timeline_bbb", "timeline_aaa"]
    by_name = media._assemble_project_items(RECORDS, NAMES, {}, q="", order="name",
                                            page=1, page_size=12)
    # 无名称项目按 project_id 参与排序：timeline_bbb/ccc 在 血压管理.mp4 之前（b < c < 血）。
    assert [item["project_id"] for item in by_name["items"]] == \
        ["timeline_bbb", "timeline_ccc", "timeline_aaa"]
    by_cards = media._assemble_project_items(RECORDS, NAMES, {}, q="", order="cards",
                                             page=1, page_size=12)
    assert [item["project_id"] for item in by_cards["items"]] == \
        ["timeline_aaa", "timeline_bbb", "timeline_ccc"]
    paged = media._assemble_project_items(RECORDS, NAMES, {}, q="", order="updated_desc",
                                          page=2, page_size=2)
    assert paged["total"] == 3 and len(paged["items"]) == 1
    assert paged["items"][0]["project_id"] == "timeline_ccc"
    clamped = media._assemble_project_items(RECORDS, NAMES, {}, q="", order="updated_desc",
                                            page=0, page_size=999)
    assert clamped["page"] == 1 and clamped["page_size"] == 50


async def test_exports_endpoint_lists_clip_downloads(seeded_exports):
    """exports 端点：只读台账 + 定位 artifact id；成片/SRT 各给出既有 file 端点 URL。"""
    class StubResult:
        def __init__(self, rows): self._rows = rows
        def mappings(self): return self._rows
    class StubSession:
        def __init__(self): self.queries = []
        async def execute(self, query, params=None):
            self.queries.append(str(query))
            rows = [
                {"id": "art-mp4-1", "kind": "clip_final"},
                {"id": "art-srt-1", "kind": "srt"},
            ] if params and params.get("cid") == "clip-001" else []
            return StubResult(rows)
    session = StubSession()
    payload = await media.studio_project_exports(PROJECT_ID, session)
    assert payload["project_id"] == PROJECT_ID
    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert item["clip_id"] == "clip-001"
    assert item["mp4_url"] == f"/api/jobs/{TASK}/artifacts/art-mp4-1/file"
    assert item["srt_url"] == f"/api/jobs/{TASK}/artifacts/art-srt-1/file"
    assert item["source_start"] is not None
    assert len(session.queries) == 1  # 只查一次 artifact，无写入


async def test_exports_endpoint_without_artifacts_yields_null_urls(seeded_exports):
    class StubResult:
        def __init__(self, rows): self._rows = rows
        def mappings(self): return self._rows
    class StubSession:
        async def execute(self, query, params=None): return StubResult([])
    payload = await media.studio_project_exports(PROJECT_ID, StubSession())
    assert payload["items"][0]["mp4_url"] is None
    assert payload["items"][0]["srt_url"] is None


@pytest.fixture
def seeded_exports(tmp_path, monkeypatch):
    from src import studio_project_store as store
    monkeypatch.setattr(store, "TASKS_ROOT", tmp_path / "tasks")
    store.register_source_video(PROJECT_ID, path="media/src.mp4")
    store.register_clips(PROJECT_ID, TASK, [
        {"clip_id": "clip-001", "title": "主题", "duration": 30.0,
         "source_start": 0.0, "source_end": 30.0},
    ])
    return tmp_path
