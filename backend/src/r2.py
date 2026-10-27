"""对象存储抽象：R2（S3 兼容）为主驱动，本地目录为开发/回退驱动.

- R2 模式（R2_BUCKET 等齐全）：浏览器直传分片（预签名 PUT）、执行器 signed GET
  下载、产物 signed PUT 回传。凭据只在后端/执行器进程环境。
- 本地模式（未配置 R2）：object_key 为 `local:<绝对路径>`；分片仍走既有
  /studio/transcribe/chunk + /assemble 端点；产物存对象目录，由后端直接回源文件。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import get_config
from .studio_runtime import copy_file

LOCAL_SCHEME = "local:"

R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID", "")
R2_BUCKET = os.getenv("R2_BUCKET", "")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "")
R2_PRESIGN_PUT_TTL = int(os.getenv("R2_PRESIGN_PUT_TTL", "600"))
R2_PRESIGN_GET_TTL = int(os.getenv("R2_PRESIGN_GET_TTL", "300"))
R2_PART_SIZE_MB = int(os.getenv("R2_PART_SIZE_MB", "16"))


def storage_mode() -> str:
    return "r2" if (R2_BUCKET and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY and R2_ACCOUNT_ID) else "local"


def is_r2_enabled() -> bool:
    return storage_mode() == "r2"


def part_size_bytes() -> int:
    return max(5, R2_PART_SIZE_MB) * 1024 * 1024


def local_path_for(object_key: str) -> Path:
    """local: 前缀的 object_key → 磁盘路径；R2 key 亦可映射到本地对象目录（供 Worker 落盘）。"""
    if object_key.startswith(LOCAL_SCHEME):
        return Path(object_key[len(LOCAL_SCHEME):])
    root = Path(get_config().temp_dir) / "objectstore"
    safe = object_key.lstrip("/").replace("..", "_")
    return root / safe


@lru_cache(maxsize=1)
def _s3_client():
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )


# ---------------------------------------------------------------------------
# R2 multipart（浏览器直传）
# ---------------------------------------------------------------------------

def r2_create_multipart(object_key: str) -> str:
    response = _s3_client().create_multipart_upload(Bucket=R2_BUCKET, Key=object_key)
    return str(response["UploadId"])


def r2_presign_part(object_key: str, r2_upload_id: str, part_number: int) -> str:
    return _s3_client().generate_presigned_url(
        "upload_part",
        Params={
            "Bucket": R2_BUCKET,
            "Key": object_key,
            "UploadId": r2_upload_id,
            "PartNumber": part_number,
        },
        ExpiresIn=R2_PRESIGN_PUT_TTL,
    )


def r2_complete_multipart(object_key: str, r2_upload_id: str, parts: list[dict[str, Any]]) -> dict[str, Any]:
    return _s3_client().complete_multipart_upload(
        Bucket=R2_BUCKET,
        Key=object_key,
        UploadId=r2_upload_id,
        MultipartUpload={"Parts": parts},
    )


def r2_head(object_key: str) -> dict[str, Any]:
    response = _s3_client().head_object(Bucket=R2_BUCKET, Key=object_key)
    return {"size": int(response.get("ContentLength", 0)), "etag": str(response.get("ETag", "")).strip('"')}


# ---------------------------------------------------------------------------
# 通用下载 / 上传（Worker 与产物回传用）
# ---------------------------------------------------------------------------

def download_to_file(object_key: str, target: Path) -> Path:
    """把 object_key 对应内容落到本地文件；返回本地路径。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    if object_key.startswith(LOCAL_SCHEME):
        source = local_path_for(object_key)
        if source.resolve() != target.resolve():
            copy_file(source, target)
        return target
    _s3_client().download_file(R2_BUCKET, object_key, str(target))
    return target


def upload_file(local: Path, object_key: str) -> dict[str, Any]:
    """把本地文件上传为对象；R2 未配置时自动回退本地对象目录（key 加 local: 前缀）。"""
    local = Path(local)
    size = local.stat().st_size
    if not is_r2_enabled():
        object_key = LOCAL_SCHEME + object_key
    if object_key.startswith(LOCAL_SCHEME):
        target = local_path_for(object_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        if local.resolve() != target.resolve():
            copy_file(local, target)
        return {"object_key": object_key, "size": size}
    _s3_client().upload_file(str(local), R2_BUCKET, object_key)
    return {"object_key": object_key, "size": size}


def presign_get(object_key: str) -> str:
    if object_key.startswith(LOCAL_SCHEME) or not is_r2_enabled():
        return ""  # 本地模式由后端直接回源文件，不走预签名
    return _s3_client().generate_presigned_url(
        "get_object", Params={"Bucket": R2_BUCKET, "Key": object_key}, ExpiresIn=R2_PRESIGN_GET_TTL
    )
