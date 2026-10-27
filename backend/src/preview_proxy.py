"""原片「预览代理」构建器。

背景：部分源片是 HEVC(H.265)/4K，**浏览器无法解码**（``canPlayType`` 返回空、画面恒为
0×0），导致工作台播放器中间画布全黑。导出的成片本身是 H.264 不受影响，因此这里只为
**预览**生成一份浏览器友好的 H.264 代理，母带保持不动。

设计：
- 源片本身已是浏览器友好编码（h264/vp9/av1…）→ 直接标记 ``native``，不转码；
- 否则后台线程用 ffmpeg 生成 ``{project}/preview/preview-{height}.mp4``（H.264 + faststart），
  先写 ``.part`` 再原子改名，避免半成品被当成可用代理；
- 状态查询不阻塞：``building`` 期间前端轮询，避免长请求被网关超时切断。
"""

from __future__ import annotations

import json
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

# 浏览器普遍可解的编码（H.264 为主；vp9/av1 现代浏览器亦可）
BROWSER_FRIENDLY = {"h264", "vp9", "av1"}

PREVIEW_MAX_HEIGHT = 720
_BUILD_LOCK = threading.Lock()
_BUILDING: set[str] = set()


class PreviewProxyError(RuntimeError):
    """代理构建失败（带机器码，便于前端提示）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass
class PreviewStatus:
    state: str  # native | ready | building | failed
    path: Path | None
    source_codec: str = ""
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "path": str(self.path) if self.path else None,
            "source_codec": self.source_codec,
            "detail": self.detail,
        }


def proxy_path(project_dir: Path) -> Path:
    return project_dir / "preview" / f"preview-{PREVIEW_MAX_HEIGHT}.mp4"


def probe_video(path: Path) -> dict[str, Any]:
    """用 ffprobe 读首个视频流的编码与尺寸（失败返回空 dict，不抛）。"""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,width,height,pix_fmt",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        data = json.loads(out.stdout or "{}")
        streams = data.get("streams") or []
        return streams[0] if streams else {}
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return {}


def source_video(project_dir: Path) -> Path:
    """工程原片：``raw/`` 下恰好一个文件（与 studio_auto_edit_bridge 约定一致）。"""
    raw = project_dir / "raw"
    candidates = [p for p in raw.iterdir() if p.is_file()] if raw.is_dir() else []
    if len(candidates) != 1:
        raise PreviewProxyError("E_PROJECT_SOURCE", "Timeline project source video not found")
    return candidates[0]


def _source_fingerprint(src: Path) -> dict[str, int]:
    """源片指纹（bytes + mtime_ns）：代理与原片的绑定关系，原片变化即判过期。"""
    st = src.stat()
    return {"bytes": st.st_size, "mtime_ns": st.st_mtime_ns}


def probe_duration(path: Path) -> float:
    """ffprobe 读容器时长（秒）；失败/测试桩缺属性返回 0，由调用方按校验失败处理。"""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        stdout = getattr(out, "stdout", None)
        data = json.loads(stdout or "{}")
        return float((data.get("format") or {}).get("duration") or 0)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError, TypeError, ValueError, AttributeError):
        return 0.0


def _update_manifest_meta(project_dir: Path, src: Path, proxy: Path) -> None:
    """project.json 显式区分 source_original 与 preview_proxy（cache_bytes 单独计量）。

    代理是可重建缓存：bytes 记为 ``cache_bytes``，绝不冒充源片、不与原片占用合并。
    读-改-写走临时文件原子替换，失败不影响既有工程状态。
    """
    manifest_path = project_dir / "project.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    src_st = src.stat()
    manifest["source_original"] = {
        "path": str(src), "bytes": src_st.st_size, "mtime_ns": src_st.st_mtime_ns,
    }
    manifest["preview_proxy"] = {
        "path": str(proxy),
        "cache_bytes": proxy.stat().st_size,
        "codec": "h264", "pix_fmt": "yuv420p", "height": PREVIEW_MAX_HEIGHT,
        "source_fingerprint": _source_fingerprint(src),
        "built_at": datetime.now(UTC).isoformat(),
    }
    tmp = manifest_path.with_name(manifest_path.name + ".tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(manifest_path)


def status(project_dir: Path) -> PreviewStatus:
    """当前预览可用性：native（源片即可播）/ ready（代理已生成）/ building / failed。"""
    try:
        src = source_video(project_dir)
    except PreviewProxyError as exc:
        return PreviewStatus("failed", None, detail=exc.message)

    codec = str(probe_video(src).get("codec_name") or "")
    if codec in BROWSER_FRIENDLY:
        return PreviewStatus("native", src, source_codec=codec, detail="源片编码浏览器可直接播放")

    target = proxy_path(project_dir)
    if target.is_file() and target.stat().st_size > 0:
        # 幂等复用前提：代理绑定的还是当前源片（fingerprint 不匹配 = 过期，需重建）。
        fingerprint_ok = True
        try:
            manifest_path = project_dir / "project.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
            recorded = (manifest.get("preview_proxy") or {}).get("source_fingerprint")
            if recorded is not None and recorded != _source_fingerprint(src):
                fingerprint_ok = False
        except (OSError, ValueError):
            pass
        if not fingerprint_ok:
            return PreviewStatus("failed", None, source_codec=codec,
                                 detail="源片已变化：预览代理过期，可重新构建")
        return PreviewStatus("ready", target, source_codec=codec, detail="已生成 H.264 预览代理")
    if str(project_dir) in _BUILDING:
        return PreviewStatus("building", None, source_codec=codec, detail="正在生成预览代理")
    return PreviewStatus("failed", None, source_codec=codec,
                         detail=f"源片编码 {codec or 'unknown'} 浏览器无法解码，且尚未生成预览代理")


def _build(project_dir: Path, src: Path) -> None:
    """后台转码：H.264 + faststart，先写 .part 再 ffprobe 校验，通过才原子激活。"""
    target = proxy_path(project_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(".part.mp4")
    source_duration = probe_duration(src)
    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-vf", f"scale=-2:{PREVIEW_MAX_HEIGHT}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
        "-threads", "4",  # 限线程：4K HEVC 转码极吃 CPU，避免拖垮同容器其它服务
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        "-f", "mp4", str(part),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=3600)
        if result.returncode != 0 or not part.is_file() or part.stat().st_size == 0:
            raise PreviewProxyError(
                "E_PREVIEW_TRANSCODE",
                (result.stderr or "ffmpeg 转码失败").strip()[:300],
            )
        # 激活前 ffprobe 校验：编码/像素格式正确、时长与原片一致（±1s）；不通过不激活。
        part_probe = probe_video(part)
        if part_probe.get("codec_name") != "h264" or part_probe.get("pix_fmt") != "yuv420p":
            raise PreviewProxyError(
                "E_PREVIEW_TRANSCODE",
                f"代理校验失败: codec={part_probe.get('codec_name')} pix_fmt={part_probe.get('pix_fmt')}",
            )
        if source_duration > 0:
            part_duration = probe_duration(part)
            if part_duration <= 0 or abs(part_duration - source_duration) > 1.0:
                raise PreviewProxyError(
                    "E_PREVIEW_TRANSCODE",
                    f"代理校验失败: 时长 {part_duration:.3f}s 与原片 {source_duration:.3f}s 偏差超 1s",
                )
        part.replace(target)
        _update_manifest_meta(project_dir, src, target)
    except (subprocess.SubprocessError, OSError) as exc:
        raise PreviewProxyError("E_PREVIEW_TRANSCODE", f"转码异常：{exc}") from exc
    finally:
        if part.is_file():
            part.unlink(missing_ok=True)
        with _BUILD_LOCK:
            _BUILDING.discard(str(project_dir))


def start_build(project_dir: Path) -> PreviewStatus:
    """幂等启动后台构建并立即返回当前状态（不阻塞请求）。"""
    current = status(project_dir)
    if current.state != "failed":
        return current
    key = str(project_dir)
    from .studio_access import prepare_project_operation, account_derived_output
    from .capacity_guard import reserve_capacity
    import uuid
    with _BUILD_LOCK:
        if key in _BUILDING:
            return PreviewStatus("building", None, current.source_codec, "正在生成预览代理")
        src = source_video(project_dir)
        estimate=src.stat().st_size*4+512*1024*1024
        capacity=reserve_capacity("preview_proxy",estimate,path=project_dir)
        capacity.__enter__()
        task_id="preview_"+uuid.uuid4().hex[:12]
        try:
            access=prepare_project_operation(task_id,project_dir.name,expected_bytes=estimate)
        except BaseException:
            capacity.__exit__(None,None,None)
            raise
        _BUILDING.add(key)

    def runner() -> None:
        stopped=threading.Event()
        def renew():
            while not stopped.wait(5):access.renew(task_id)
        heartbeat=threading.Thread(target=renew,name="preview-quota-"+task_id,daemon=True)
        try:
            heartbeat.start()
            _build(project_dir, src)
            task=access.get("task",task_id)
            account_derived_output((access,{"subject":task["owner"],"domain_id":task["domain"]},task["project_id"]),proxy_path(project_dir))
        except Exception:  # noqa: BLE001 - 失败信息通过 status() 的 detail 暴露
            with _BUILD_LOCK:
                _BUILDING.discard(key)
        finally:
            stopped.set()
            if heartbeat.is_alive():heartbeat.join(timeout=1)
            with _BUILD_LOCK:_BUILDING.discard(key)
            try:
                access.release(task_id)
            finally:
                capacity.__exit__(None,None,None)

    try:
        threading.Thread(target=runner, name=f"preview-proxy-{project_dir.name}", daemon=True).start()
    except BaseException:
        with _BUILD_LOCK:_BUILDING.discard(key)
        try:access.release(task_id)
        finally:capacity.__exit__(None,None,None)
        raise
    return PreviewStatus("building", None, current.source_codec, "已开始生成预览代理")
