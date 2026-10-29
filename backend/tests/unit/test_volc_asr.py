"""volc_asr 单测：提交契约（头/热词注入）+ 查询解析 + 状态判断。全 mock，不触网。"""
import json

import httpx
import pytest
from fastapi import HTTPException

from src import volc_asr


def _cfg(monkeypatch):
    monkeypatch.setenv("VOLC_ASR_API_KEY", "sk-test")
    monkeypatch.setenv("VOLC_ASR_AUDIO_URL_TEMPLATE", "https://push.zcmed.net/drop/files/{filename}")


def test_submit_sends_contract_headers_and_hotwords(monkeypatch):
    _cfg(monkeypatch)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["x-api-key"] = request.headers.get("X-Api-Key")
        seen["resource"] = request.headers.get("X-Api-Resource-Id")
        seen["sequence"] = request.headers.get("X-Api-Sequence")
        seen["request-id"] = bool(request.headers.get("X-Api-Request-Id"))
        body = json.loads(request.content)
        seen["audio_url"] = body["audio"]["url"]
        seen["format"] = body["audio"]["format"]
        seen["model_name"] = body["request"]["model_name"]
        seen["hotwords"] = json.loads(body["request"]["corpus"]["context"])["hotwords"]
        return httpx.Response(200, headers={"X-Api-Status-Code": "20000000",
                                            "X-Api-Message": "OK"},
                              json={"task_id": "tid-1"})

    task = volc_asr.submit_task("https://pub/x.mp3", ["氨氯地平", "汞柱"], transport=httpx.MockTransport(handler))
    assert task == "tid-1"
    assert seen["x-api-key"] == "sk-test"
    assert seen["resource"] == "volc.seedasr.auc"
    assert seen["sequence"] == "-1" and seen["request-id"] is True
    assert seen["audio_url"] == "https://pub/x.mp3" and seen["format"] == "mp3"
    assert seen["model_name"] == "bigmodel"
    assert {"word": "氨氯地平"} in seen["hotwords"]


def test_submit_api_error_raises_502(monkeypatch):
    _cfg(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"X-Api-Status-Code": "45000001",
                                            "X-Api-Message": "quota exceeded"}, json={})

    with pytest.raises(HTTPException) as exc:
        volc_asr.submit_task("https://pub/x.mp3", None, transport=httpx.MockTransport(handler))
    assert exc.value.status_code == 502
    assert exc.value.detail["api_status"] == "45000001"


def test_parse_result_utterances_to_segments():
    q = {"result": {"text": "全文", "utterances": [
        {"start_time": 100, "end_time": 2500, "text": "第一句"},
        {"start_time": 2500, "end_time": 4800, "text": "  第二句 "},
        {"start_time": 3000, "end_time": 5000, "text": ""},
    ]}, "language": "zh-CN"}
    out = volc_asr.parse_result(q)
    assert [s["text"] for s in out["asr_segments"]] == ["第一句", "第二句"]
    assert out["asr_segments"][0]["start"] == 0.1 and out["asr_segments"][0]["end"] == 2.5


def test_is_done_states():
    assert volc_asr.is_done({"status": "20000000"}) is True
    assert volc_asr.is_done({"status": "20000001"}) is False
    assert volc_asr.is_done({}) is None


def test_not_configured_raises(monkeypatch):
    monkeypatch.delenv("VOLC_ASR_API_KEY", raising=False)
    with pytest.raises(HTTPException) as exc:
        volc_asr.submit_task("https://pub/x.mp3", None, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    assert exc.value.status_code == 503
