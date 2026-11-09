"""双库向量：fastembed 本地模型（BAAI/bge-small-zh-v1.5，512 维）。

- 模型走 HF 镜像（HF_ENDPOINT，默认 https://hf-mirror.com）懒加载单例；
- 任何失败（包缺失/模型下载失败/推理异常）返回 None，调用方降级为无向量，
  绝不阻塞入库/检索主流程；
- 文本截断 2000 字（覆盖标题+摘要+全文的有效语义）。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

MODEL_NAME = "BAAI/bge-small-zh-v1.5"
_MAX_CHARS = 2000

_lock = threading.Lock()
_model = None
_state: bool | None = None  # None=未尝试；True=可用；False=不可用（缓存失败，不再重试）


def _load():
    global _model, _state
    if _state is not None:
        return _model if _state else None
    with _lock:
        if _state is not None:
            return _model if _state else None
        try:
            os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
            from fastembed import TextEmbedding  # 可选依赖，未安装则降级

            # 缓存显式放大盘：fastembed 默认落 /tmp（128M tmpfs），91M 模型缓存
            # 会把系统临时区挤到近乎满盘，殃及封面抽帧等（2026-11-09 Errno 2）。
            cache_dir = os.getenv("FASTEMBED_CACHE_DIR", "/domain/.tools/fastembed-cache")
            Path(cache_dir).mkdir(parents=True, exist_ok=True)
            _model = TextEmbedding(MODEL_NAME, cache_dir=cache_dir)
            _state = True
        except Exception:
            _model = None
            _state = False
    return _model if _state else None


def embed_texts(texts: list[str]) -> Optional[list[list[float]]]:
    """批量向量化；不可用/失败返回 None。"""
    model = _load()
    if model is None:
        return None
    try:
        clipped = [(t or "")[:_MAX_CHARS] for t in texts]
        return [v.tolist() for v in model.embed(clipped)]
    except Exception:
        return None


def embed_topic(title: str, summary: str = "", raw_text: str = "") -> Optional[list[float]]:
    vectors = embed_texts([f"{title}\n{summary or ''}\n{raw_text or ''}"])
    return vectors[0] if vectors else None


def embed_script(title: str, hook: str = "", body: str = "") -> Optional[list[float]]:
    vectors = embed_texts([f"{title}\n{hook or ''}\n{body or ''}"])
    return vectors[0] if vectors else None
