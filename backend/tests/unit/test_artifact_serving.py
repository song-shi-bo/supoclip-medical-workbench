"""Artifact 媒体接口契约测试（播放链路 B 线）。

覆盖两个给前端播放器供片的端点：

1. ``GET /api/jobs/{job_id}/artifacts/{artifact_id}/file``（jobs.py，studio_artifacts 台账分支）
2. ``GET /studio/auto-edit/{task_id}/artifact``（media.py，本地任务 output 目录分支）

播放链路契约：200 + ``Content-Type: video/mp4`` + ``Content-Length`` +
``Accept-Ranges: bytes``；``Range`` → 206（含 ``Content-Range``）；非法/越界
Range → 416（畸形 → 400）；路径穿越 → 400/404。

素材用 ffmpeg 现场生成 0.5s mp4（写法同 test_reedit_replace.py 的
source_video fixture）；端点实现走 Starlette ``FileResponse``，这里同时
证实其 Range 支持确实生效，而不是想当然。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import clip_render as cr
from src import studio_auto_edit_bridge as bridge
from src.api.routes import jobs as jobs_routes
from src.api.routes import media as media_routes

JOB_ID = "192a7b28a47c41059fb147651593d480"  # 32 位 hex job id（reedit 新卡片路径）
LEGACY_TASK_ID = "autoedit_aaaaaaaaaaaa"  # 旧任务标识（防回退）
CLIP_ID = "clip-x1"
ARTIFACT_ID = "art-1"


@pytest.fixture(scope="module")
def clip_mp4(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """0.5s 合成 mp4（> 1KiB，保证 bytes=0-1023 是真正的子区间）。"""
    path = tmp_path_factory.mktemp("media") / "clip_final.mp4"
    subprocess.run(
        [cr._resolve(cr.FFMPEG_BIN), "-y",
         "-f", "lavfi", "-i", "testsrc2=duration=0.5:size=320x240:rate=15",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest",
         str(path)],
        check=True, capture_output=True)
    assert path.stat().st_size > 4096
    return path


@pytest.fixture
def app() -> FastAPI:
    """最小 app：只挂载被测两个路由（不触发主 app 的 DB/Redis lifespan）。"""
    app = FastAPI()
    app.include_router(jobs_routes.router)
    app.include_router(media_routes.router)
    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# 1) ledger 端点：/studio/auto-edit/{task_id}/artifact
# ---------------------------------------------------------------------------

def _stage_ledger_clip(root: Path, task_id: str, dest_name: str = "clip_final.mp4") -> Path:
    staged = root / "output" / task_id / "clips" / CLIP_ID / dest_name
    staged.parent.mkdir(parents=True, exist_ok=True)
    return staged


@pytest.fixture
def ledger_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clip_mp4: Path) -> Path:
    """临时 STUDIO_TASKS_ROOT；resolve_auto_edit_artifact 读 bridge 模块级 TASKS_ROOT。"""
    root = tmp_path / "tasks"
    monkeypatch.setenv("STUDIO_TASKS_ROOT", str(root))
    monkeypatch.setattr(bridge, "TASKS_ROOT", root)
    shutil.copy2(clip_mp4, _stage_ledger_clip(root, JOB_ID))
    return root


def _artifact_url(task_id: str) -> str:
    return f"/studio/auto-edit/{task_id}/artifact"


def _artifact_params(task_id: str, filename: str = "clip_final.mp4") -> dict:
    return {"path": f"output/{task_id}/clips/{CLIP_ID}/{filename}"}


def test_ledger_serves_full_mp4(client: TestClient, ledger_root: Path):
    staged = ledger_root / "output" / JOB_ID / "clips" / CLIP_ID / "clip_final.mp4"
    resp = client.get(_artifact_url(JOB_ID), params=_artifact_params(JOB_ID))
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "video/mp4"
    assert int(resp.headers["content-length"]) == staged.stat().st_size
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.content == staged.read_bytes()


def test_ledger_range_prefix_returns_206(client: TestClient, ledger_root: Path):
    size = (ledger_root / "output" / JOB_ID / "clips" / CLIP_ID / "clip_final.mp4").stat().st_size
    resp = client.get(_artifact_url(JOB_ID), params=_artifact_params(JOB_ID),
                      headers={"Range": "bytes=0-1023"})
    assert resp.status_code == 206, resp.text
    assert resp.headers["content-range"] == f"bytes 0-1023/{size}"
    assert int(resp.headers["content-length"]) == 1024
    assert resp.content == (ledger_root / "output" / JOB_ID / "clips" / CLIP_ID
                            / "clip_final.mp4").read_bytes()[:1024]


def test_ledger_range_open_ended_returns_206(client: TestClient, ledger_root: Path):
    size = (ledger_root / "output" / JOB_ID / "clips" / CLIP_ID / "clip_final.mp4").stat().st_size
    resp = client.get(_artifact_url(JOB_ID), params=_artifact_params(JOB_ID),
                      headers={"Range": "bytes=1024-"})
    assert resp.status_code == 206, resp.text
    assert resp.headers["content-range"] == f"bytes 1024-{size - 1}/{size}"
    assert int(resp.headers["content-length"]) == size - 1024


def test_ledger_range_unsatisfiable_returns_416(client: TestClient, ledger_root: Path):
    size = (ledger_root / "output" / JOB_ID / "clips" / CLIP_ID / "clip_final.mp4").stat().st_size
    resp = client.get(_artifact_url(JOB_ID), params=_artifact_params(JOB_ID),
                      headers={"Range": "bytes=99999999-"})
    assert resp.status_code == 416, resp.text
    assert resp.headers["content-range"] == f"bytes */{size}"


def test_ledger_malformed_range_returns_400(client: TestClient, ledger_root: Path):
    resp = client.get(_artifact_url(JOB_ID), params=_artifact_params(JOB_ID),
                      headers={"Range": "bytes=abc"})
    assert resp.status_code == 400, resp.text


def test_ledger_traversal_rejected(client: TestClient, ledger_root: Path):
    # 相对穿越逃出任务 output 根 → FileNotFoundError → 404
    resp = client.get(_artifact_url(JOB_ID),
                      params={"path": f"output/{JOB_ID}/clips/{CLIP_ID}/../../secret.mp4"})
    assert resp.status_code == 404, resp.text
    # 绝对路径 → FileNotFoundError → 404
    resp = client.get(_artifact_url(JOB_ID), params={"path": "/etc/passwd"})
    assert resp.status_code == 404, resp.text
    # 非法任务标识 → ValueError → 400。（带斜杠的 "../evil" 会被 httpx 按 RFC 3986
    # 在客户端就规范化掉，到不了路由；带非法字符且不含斜杠的 id 才能进到校验分支。）
    resp = client.get(_artifact_url("short"), params={"path": "output/x/clip_final.mp4"})
    assert resp.status_code == 400, resp.text


def test_ledger_non_mp4_suffix_media_type(client: TestClient, ledger_root: Path, clip_mp4: Path):
    staged = _stage_ledger_clip(ledger_root, JOB_ID, "cover.jpg")
    shutil.copy2(clip_mp4, staged)  # 内容无所谓，只看后缀推断的 Content-Type
    resp = client.get(_artifact_url(JOB_ID), params=_artifact_params(JOB_ID, "cover.jpg"))
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "image/jpeg"


def test_ledger_legacy_task_id_still_served(
        client: TestClient, ledger_root: Path, clip_mp4: Path):
    """旧 autoedit_* 任务 + 平铺布局不得回归（2026-10-16 修复点）。"""
    staged = ledger_root / "output" / LEGACY_TASK_ID / "clip_final.mp4"
    staged.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(clip_mp4, staged)
    resp = client.get(_artifact_url(LEGACY_TASK_ID),
                      params={"path": f"output/{LEGACY_TASK_ID}/clip_final.mp4"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "video/mp4"
    assert resp.headers["accept-ranges"] == "bytes"


# ---------------------------------------------------------------------------
# 2) jobs 文件端点：/api/jobs/{job_id}/artifacts/{artifact_id}/file（local 分支）
# ---------------------------------------------------------------------------

def _wire_local_artifact(monkeypatch: pytest.MonkeyPatch, path: Path | None,
                         presign: str | None = None) -> None:
    """桩掉台账查询与 r2（本地模式：presign 为空，local_path_for 指到临时文件）。"""
    rows = [{"id": ARTIFACT_ID, "object_key": f"artifacts/{JOB_ID}/clip_final.mp4"}]

    async def fake_list_artifacts(_job_id: str):
        return rows

    r2_stub = SimpleNamespace(
        presign_get=lambda key: presign,
        local_path_for=lambda key: path if path is not None else Path(key),
    )
    monkeypatch.setattr(jobs_routes, "list_artifacts", fake_list_artifacts)
    monkeypatch.setattr(jobs_routes, "r2", r2_stub)


def test_jobs_file_serves_full_local_artifact(
        client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clip_mp4: Path):
    local = tmp_path / "artifact.mp4"
    shutil.copy2(clip_mp4, local)
    _wire_local_artifact(monkeypatch, local)
    resp = client.get(f"/api/jobs/{JOB_ID}/artifacts/{ARTIFACT_ID}/file")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "video/mp4"
    assert int(resp.headers["content-length"]) == local.stat().st_size
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.content == local.read_bytes()


def test_jobs_file_range_returns_206(
        client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clip_mp4: Path):
    local = tmp_path / "artifact.mp4"
    shutil.copy2(clip_mp4, local)
    _wire_local_artifact(monkeypatch, local)
    resp = client.get(f"/api/jobs/{JOB_ID}/artifacts/{ARTIFACT_ID}/file",
                      headers={"Range": "bytes=0-1023"})
    assert resp.status_code == 206, resp.text
    assert resp.headers["content-range"] == f"bytes 0-1023/{local.stat().st_size}"
    assert int(resp.headers["content-length"]) == 1024


def test_jobs_file_range_unsatisfiable_returns_416(
        client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clip_mp4: Path):
    local = tmp_path / "artifact.mp4"
    shutil.copy2(clip_mp4, local)
    _wire_local_artifact(monkeypatch, local)
    resp = client.get(f"/api/jobs/{JOB_ID}/artifacts/{ARTIFACT_ID}/file",
                      headers={"Range": "bytes=99999999-"})
    assert resp.status_code == 416, resp.text
    assert resp.headers["content-range"] == f"bytes */{local.stat().st_size}"


def test_jobs_file_missing_returns_404(
        client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _wire_local_artifact(monkeypatch, tmp_path / "not-here.mp4")
    resp = client.get(f"/api/jobs/{JOB_ID}/artifacts/{ARTIFACT_ID}/file")
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Artifact file missing"


def test_jobs_artifact_unknown_id_returns_404(
        client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clip_mp4: Path):
    local = tmp_path / "artifact.mp4"
    shutil.copy2(clip_mp4, local)
    _wire_local_artifact(monkeypatch, local)
    resp = client.get(f"/api/jobs/{JOB_ID}/artifacts/does-not-exist/file")
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Artifact not found"


def test_jobs_file_non_mp4_suffix_media_type(
        client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clip_mp4: Path):
    local = tmp_path / "cover.jpg"
    shutil.copy2(clip_mp4, local)
    _wire_local_artifact(monkeypatch, local)
    resp = client.get(f"/api/jobs/{JOB_ID}/artifacts/{ARTIFACT_ID}/file")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "image/jpeg"


def test_jobs_file_presign_redirects_307(
        client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clip_mp4: Path):
    """R2 启用分支：presign 非空时 307 跳对象存储，不经本地文件。"""
    _wire_local_artifact(monkeypatch, tmp_path / "artifact.mp4",
                         presign="https://r2.example.com/signed")
    resp = client.get(f"/api/jobs/{JOB_ID}/artifacts/{ARTIFACT_ID}/file",
                      follow_redirects=False)
    assert resp.status_code == 307, resp.text
    assert resp.headers["location"] == "https://r2.example.com/signed"
