"""clip_delivery 单测：配置缺失 503 / 成功投递 / 上游 401 透传 / 上游 500 归一 502。

HTTP 层用 httpx.MockTransport，不触网；PG 解析路径不在本文件范围（路由集成覆盖）。
"""
import httpx
import pytest
from fastapi import HTTPException

from src.clip_delivery import deliver_clip_file, drop_config


def test_drop_config_missing_returns_503(monkeypatch):
    monkeypatch.delenv("DROP_INBOX_URL", raising=False)
    monkeypatch.delenv("DROP_UPLOAD_TOKEN", raising=False)
    with pytest.raises(HTTPException) as exc:
        drop_config()
    assert exc.value.status_code == 503
    assert "drop_inbox_not_configured" in str(exc.value.detail)


def _cfg(monkeypatch):
    monkeypatch.setenv("DROP_INBOX_URL", "https://drop.test/api/inbox")
    monkeypatch.setenv("DROP_UPLOAD_TOKEN", "tok-1")


def test_deliver_success_sends_token_and_bytes(monkeypatch, tmp_path):
    _cfg(monkeypatch)
    f = tmp_path / "clip_final.mp4"
    f.write_bytes(b"0123456789abcdef")

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["has_bytes"] = b"0123456789abcdef" in request.content
        return httpx.Response(200, json={"ok": True, "id": "drop-1"})

    out = deliver_clip_file(f, {"subject": "u1"}, transport=httpx.MockTransport(handler))
    assert out == {"ok": True, "id": "drop-1"}
    assert seen["auth"] == "Bearer tok-1"
    assert seen["has_bytes"] is True


def test_deliver_upstream_401_passthrough(monkeypatch, tmp_path):
    _cfg(monkeypatch)
    f = tmp_path / "c.mp4"
    f.write_bytes(b"x")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "error": "unauthorized"})

    with pytest.raises(HTTPException) as exc:
        deliver_clip_file(f, {}, transport=httpx.MockTransport(handler))
    assert exc.value.status_code == 401


def test_deliver_upstream_500_normalized_to_502(monkeypatch, tmp_path):
    _cfg(monkeypatch)
    f = tmp_path / "c.mp4"
    f.write_bytes(b"x")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(HTTPException) as exc:
        deliver_clip_file(f, {}, transport=httpx.MockTransport(handler))
    assert exc.value.status_code == 502
    assert exc.value.detail["upstream_status"] == 500
