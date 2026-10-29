"""火山引擎豆包语音「录音文件识别 2.0」云端转写（2026-10-28）。

官方契约（docs 6561）：
  提交  POST https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit
        头：X-Api-Key / X-Api-Resource-Id: volc.seedasr.auc / X-Api-Request-Id: UUID / X-Api-Sequence: -1
        体：{"audio": {"url": <公网音频链接>, "format": "mp3"},
             "request": {"model_name": "bigmodel", "enable_itn": true, "enable_punc": true,
                          "show_utterances": true,
                          "corpus": {"context": "<JSON 字符串，含 hotwords 术语表>"}}}
  响应  task_id + 头 X-Api-Status-Code=20000000 / X-Api-Message=OK
  查询  POST …/bigmodel/query（X-Api-Request-Id = task_id；响应含 utterances 分句）

前置依赖：音频必须能通过**公网 URL** 下载——工作台把音频经 95 投递通道上传后，
由 95 侧以 HTTPS 暴露下载地址（VOLC_ASR_AUDIO_URL_TEMPLATE 指向该地址模板）。

配置（backend/.env）：
  VOLC_ASR_API_KEY            豆包语音 API Key
  VOLC_ASR_AUDIO_URL_TEMPLATE 公网音频地址模板，{filename} 占位
  TRANSCRIPTION_PROVIDER      volc_cloud 时启用云端转写
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import HTTPException

SUBMIT_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
QUERY_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"
RESOURCE_ID = "volc.seedasr.auc"


def _api_key() -> str:
    key = os.environ.get("VOLC_ASR_API_KEY", "").strip()
    if not key:
        raise HTTPException(503, "volc_asr_not_configured")
    return key


def extract_audio_mp3(video_path: Path, out_dir: Optional[Path] = None) -> Path:
    """视频 → 16kHz 单声道 mp3（ASR 友好、体积小）。25MB 上限内自动降码率。"""
    out_dir = out_dir or video_path.parent
    out = out_dir / (video_path.stem + ".asr.mp3")
    cmd = ["ffmpeg", "-y", "-i", str(video_path), "-vn", "-acodec", "libmp3lame",
           "-ar", "16000", "-ac", "1", "-b:a", "64k", str(out)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if proc.returncode != 0 or not out.is_file():
        raise RuntimeError(f"音频提取失败: {proc.stderr[-300:]}")
    if out.stat().st_size > 90 * 1024 * 1024:  # 超 90MB 降为 32kbps 再压一次
        cmd[-3] = "32k"
        subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    return out


def upload_audio(mp3: Path, subject: str, project_id: str) -> str:
    """把音频送到 95 的公开下载根（/opt/dropbox/files/asr_audio/），返回公网文件名。

    传输：SSH（scp，aliyun_ecs_rsa 密钥）——95 侧 nginx 已把 files/ 目录暴露为
    https://push.zcmed.net/drop/files/<相对路径>（只读 GET，含 Range/防穿越）。
    文件名含项目与内容哈希（不可枚举），转写完成后由本地清理任务删除。
    """
    key = os.environ.get("VOLC_ASR_SSH_KEY", str(Path.home() / ".ssh" / "aliyun_ecs_rsa"))
    target = os.environ.get("VOLC_ASR_SSH_TARGET", "zyb@101.200.241.95")
    remote_dir = os.environ.get("VOLC_ASR_FILES_ROOT", "/opt/dropbox/files/asr_audio")
    remote_name = f"asr_{project_id}_{mp4_hash(mp3)}.mp3"
    meta = {"subject": subject, "project_id": project_id, "kind": "asr_audio",
            "filename": remote_name, "sha256": _sha256(mp3), "size": mp3.stat().st_size}
    meta_tmp = mp3.parent / (remote_name + ".meta.json")
    meta_tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    cmd = ["scp", "-i", key, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
           str(mp3), str(meta_tmp), f"{target}:{remote_dir}/"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    meta_tmp.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise HTTPException(502, {"detail": "audio_upload_failed", "stderr": proc.stderr[-300:]})
    return remote_name


def mp4_hash(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def audio_public_url(remote_name: str) -> str:
    """投递到 95 的音频的公网下载地址（95 侧以静态方式暴露 inbox 文件）。"""
    tmpl = os.environ.get("VOLC_ASR_AUDIO_URL_TEMPLATE", "").strip()
    if not tmpl:
        raise HTTPException(503, "volc_asr_audio_url_not_configured")
    return tmpl.format(filename=remote_name)


def cleanup_remote_audio(remote_name: str) -> bool:
    """转写完成后删除 95 上的中转音频（SSH rm）。返回是否删除成功。"""
    key = os.environ.get("VOLC_ASR_SSH_KEY", str(Path.home() / ".ssh" / "aliyun_ecs_rsa"))
    target = os.environ.get("VOLC_ASR_SSH_TARGET", "zyb@101.200.241.95")
    remote_dir = os.environ.get("VOLC_ASR_FILES_ROOT", "/opt/dropbox/files/asr_audio")
    cmd = ["ssh", "-i", key, "-o", "BatchMode=yes",
           target, f"rm -f {remote_dir}/{remote_name}"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return proc.returncode == 0
    except Exception:  # noqa: BLE001 — 清理失败不影响主流程（挂账后续清）
        return False


def submit_task(audio_url: str, glossary_terms: Optional[list[str]] = None,
                *, language: str = "zh-CN", transport: Optional[httpx.BaseTransport] = None) -> str:
    """提交转写任务（术语表走 corpus.context 直传热词）。返回 task_id。"""
    key = _api_key()
    request_id = str(uuid.uuid4())
    corpus: dict[str, Any] = {}
    if glossary_terms:
        corpus["context"] = json.dumps(
            {"hotwords": [{"word": t} for t in glossary_terms[:200]]}, ensure_ascii=False)
    body = {
        "audio": {"url": audio_url, "format": "mp3", "codec": "raw", "rate": 16000,
                  "bits": 16, "channel": 1},
        "request": {"model_name": "bigmodel", "enable_itn": True, "enable_punc": True,
                    "show_utterances": True, "language": language},
    }
    if corpus:
        body["request"]["corpus"] = corpus
    headers = {"X-Api-Key": key, "X-Api-Resource-Id": RESOURCE_ID,
               "X-Api-Request-Id": request_id, "X-Api-Sequence": "-1"}
    with httpx.Client(timeout=httpx.Timeout(120, connect=15), transport=transport) as c:
        r = c.post(SUBMIT_URL, headers=headers, json=body)
    status = r.headers.get("X-Api-Status-Code", "")
    if r.status_code >= 400 or status != "20000000":
        raise HTTPException(502, {"detail": "volc_asr_submit_failed", "http": r.status_code,
                                  "api_status": status, "api_message": r.headers.get("X-Api-Message", ""),
                                  "body": r.text[:300]})
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    return data.get("task_id") or request_id


def query_task(task_id: str, *, transport: Optional[httpx.BaseTransport] = None) -> dict[str, Any]:
    """查询任务结果。返回 {"json":…, "status": X-Api-Status-Code, "message": …}。

    状态码：20000000=完成；20000001/20000002=处理中；其它=失败/未找到。
    注意：X-Api-Request-Id 必须传 **task_id 本身**（提交返回的），传新 UUID 会报任务不存在。
    """
    key = _api_key()
    headers = {"X-Api-Key": key, "X-Api-Resource-Id": RESOURCE_ID,
               "X-Api-Request-Id": task_id, "X-Api-Sequence": "-1"}
    with httpx.Client(timeout=httpx.Timeout(120, connect=15), transport=transport) as c:
        r = c.post(QUERY_URL, headers=headers, json={})
    if r.status_code >= 400:
        raise HTTPException(502, {"detail": "volc_asr_query_failed", "http": r.status_code,
                                  "body": r.text[:300]})
    try:
        body = r.json()
    except ValueError:
        body = {"raw": r.text[:500]}
    return {"json": body, "status": r.headers.get("X-Api-Status-Code", ""),
            "message": r.headers.get("X-Api-Message", "")}


def parse_result(query_json: dict[str, Any]) -> dict[str, Any]:
    """查询结果 → 工作台转写格式（asr_segments: start/end/text 秒；tolerant 解析）。"""
    resp = query_json.get("result") or {}
    utterances = resp.get("utterances") or []
    segments: list[dict[str, Any]] = []
    for u in utterances:
        try:
            start_ms = float(u.get("start_time", u.get("start", 0)))
            end_ms = float(u.get("end_time", u.get("end", 0)))
        except (TypeError, ValueError):
            continue
        text = str(u.get("text", "")).strip()
        if text:
            segments.append({"start": round(start_ms / 1000, 3),
                             "end": round(end_ms / 1000, 3), "text": text})
    return {"asr_segments": segments, "language": query_json.get("language", "zh-CN"),
            "words": [], "full_text": (resp.get("text") or "").strip()}


def is_done(query_result: dict[str, Any]) -> Optional[bool]:
    """入参为 query_task 返回值。True=完成 False=处理中 None=无法判断。"""
    s = str(query_result.get("status") or "")
    if s == "20000000":
        return True
    if s.startswith("20000"):
        return False
    return None
