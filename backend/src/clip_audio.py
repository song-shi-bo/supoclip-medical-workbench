"""BGM 资产与波形峰值（Inspector Phase 1 契约 §3.4）。

本模块只吃 plain ``Path`` / ``dict``：既不 import ``clip_config`` 也不 import
``studio_project_store``，所以可以独立单测，也不会产生循环导入。

职责
----
* :func:`extract_peaks` —— 用 ffmpeg 把音频解成 8kHz 单声道 s16le，按窗口取
  绝对值峰值，线性重采样到 ``points`` 个 ``[0,1]`` 的浮点数，给前端画波形；
* :func:`probe_duration` —— ffprobe 取时长（失败返回 ``None``，不抛错）；
* :func:`save_bgm` / :func:`delete_bgm` —— BGM 上传落地到
  ``{clip_dir}/audio/bgm-{sha8}{ext}``（内容寻址：同样的字节只存一份）与删除；
* :func:`normalize_track` / :func:`validate_tracks` —— ``audio_tracks`` 的结构与
  范围校验（``E_AUDIO_TRACK_INVALID``）。

ffmpeg 缺席时也是安全的：**没有任何 import 期二进制检查**，只有真正要解码/探测时
才 ``shutil.which``，缺了就把 ``E_AUDIO_PEAKS_FAILED`` 或 ``None`` 如实上报。
内存上限：``-t 600`` + 读取长度上限（600s × 8000Hz × 2B ≈ 9.6MB），
不会让 ffmpeg 的输出把内存灌爆。
"""

from __future__ import annotations

import array
import hashlib
import math
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "AUDIO_DIRNAME",
    "BGM_KINDS",
    "MAX_PEAKS",
    "SAMPLE_RATE",
    "SUPPORTED_BGM_EXTENSIONS",
    "ClipAudioError",
    "audio_dir",
    "delete_bgm",
    "extract_peaks",
    "normalize_track",
    "peaks_payload",
    "probe_duration",
    "save_bgm",
    "validate_tracks",
]

AUDIO_DIRNAME = "audio"
BGM_KINDS = ("bgm",)
MAX_PEAKS = 800
SUPPORTED_BGM_EXTENSIONS = (".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac")

SAMPLE_RATE = 8000
#: 只分析前 10 分钟（且读取长度有上限），避免长音频把内存与耗时拉爆。
MAX_ANALYZE_SECONDS = 600
MAX_PCM_BYTES = MAX_ANALYZE_SECONDS * SAMPLE_RATE * 2
FFMPEG_TIMEOUT_S = 120
FFPROBE_TIMEOUT_S = 30
FULL_SCALE = 32768.0


class ClipAudioError(ValueError):
    """带 E_ 码的显式失败（沿用工作台 ValueError → HTTP 4xx 的既有映射）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


E_AUDIO_TRACK_INVALID = "E_AUDIO_TRACK_INVALID"
E_AUDIO_FILE_TOO_LARGE = "E_AUDIO_FILE_TOO_LARGE"
E_AUDIO_UNSUPPORTED_FORMAT = "E_AUDIO_UNSUPPORTED_FORMAT"
E_AUDIO_TRACK_NOT_FOUND = "E_AUDIO_TRACK_NOT_FOUND"
E_AUDIO_PEAKS_FAILED = "E_AUDIO_PEAKS_FAILED"


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def audio_dir(clip_dir: Path) -> Path:
    return Path(clip_dir) / AUDIO_DIRNAME


# ---------------------------------------------------------------------------
# 峰值 / 时长
# ---------------------------------------------------------------------------

def _peak_points(points: Any) -> int:
    """目标点数：非法输入退回 ``MAX_PEAKS``，硬上限 ``MAX_PEAKS``。"""

    try:
        value = int(points)
    except (TypeError, ValueError):
        return MAX_PEAKS
    return max(1, min(MAX_PEAKS, value))


def _stderr_tail(stderr: Any, limit: int = 300) -> str:
    if isinstance(stderr, bytes):
        text = stderr.decode("utf-8", errors="replace")
    elif stderr is None:
        text = ""
    else:
        text = str(stderr)
    text = " ".join(text.split())
    return text[-limit:]


def _pcm_samples(payload: bytes) -> array.array:
    """s16le 字节 → 采样数组（长度取偶；非小端机器做一次字节交换）。"""

    usable = payload[: len(payload) - (len(payload) % 2)]
    samples = array.array("h")
    samples.frombytes(usable)
    if sys.byteorder != "little":  # pragma: no cover - x86/arm 都是小端
        samples.byteswap()
    return samples


def _bucket_peaks(samples: array.array, buckets: int) -> list[float]:
    """把采样切成 ``buckets`` 段，每段取绝对值峰值（0..1）。"""

    total = len(samples)
    peaks: list[float] = []
    for index in range(buckets):
        start = (index * total) // buckets
        end = ((index + 1) * total) // buckets
        if end <= start:
            end = min(total, start + 1)
        chunk = samples[start:end]
        if not chunk:
            peaks.append(0.0)
            continue
        # -min() 处理 -32768（abs(-32768) 在 int16 上会溢出）。
        peaks.append(max(max(chunk), -min(chunk)) / FULL_SCALE)
    return peaks


def _resample(values: list[float], points: int) -> list[float]:
    """线性重采样到恰好 ``points`` 个值（端点对齐，保持包络形状）。"""

    count = len(values)
    if count == 0:
        return []
    if points <= 1:
        return [max(values)]
    if count == points:
        return list(values)
    if count == 1:
        return [values[0]] * points
    out: list[float] = []
    for index in range(points):
        position = index * (count - 1) / (points - 1)
        low = int(math.floor(position))
        high = min(low + 1, count - 1)
        fraction = position - low
        out.append(values[low] * (1.0 - fraction) + values[high] * fraction)
    return out


def extract_peaks(media_path: Path, points: int = MAX_PEAKS) -> list[float]:
    """ffmpeg 解码 → 每窗口绝对值峰值 → 线性重采样成 ``points`` 个 ``[0,1]``。

    ffmpeg 不可用或解码失败 → ``ClipAudioError(E_AUDIO_PEAKS_FAILED, 可读原因)``。
    返回长度 ≤ ``MAX_PEAKS``（``points`` 被硬钳到 800），保留 4 位小数。
    """

    media_path = Path(media_path)
    target = _peak_points(points)
    binary = shutil.which("ffmpeg")
    if not binary:
        raise ClipAudioError(
            E_AUDIO_PEAKS_FAILED, "本机没有 ffmpeg，无法解析音频波形（请先安装 ffmpeg）")

    command = [
        binary, "-v", "error", "-nostdin",
        "-i", str(media_path),
        "-t", str(MAX_ANALYZE_SECONDS),
        "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
        "-f", "s16le", "-",
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, timeout=FFMPEG_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise ClipAudioError(E_AUDIO_PEAKS_FAILED, f"ffmpeg 执行失败: {error}") from error

    if completed.returncode != 0:
        reason = _stderr_tail(getattr(completed, "stderr", b"")) or (
            f"ffmpeg 退出码 {completed.returncode}")
        raise ClipAudioError(E_AUDIO_PEAKS_FAILED, f"解码音频失败: {reason}")

    payload = (completed.stdout or b"")[:MAX_PCM_BYTES]
    if not payload:
        return []
    samples = _pcm_samples(payload)
    if not len(samples):
        return []
    buckets = min(target, len(samples))
    peaks = _bucket_peaks(samples, buckets)
    values = peaks if buckets == target else _resample(peaks, target)
    return [round(min(1.0, max(0.0, float(value))), 4) for value in values]


def probe_duration(media_path: Path) -> float | None:
    """ffprobe 取时长（秒）；没有 ffprobe / 失败 / 解析失败一律返回 ``None``。"""

    binary = shutil.which("ffprobe")
    if not binary:
        return None
    command = [
        binary, "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(Path(media_path)),
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, timeout=FFPROBE_TIMEOUT_S, check=False, text=True)
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    try:
        value = float(str(completed.stdout or "").strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return round(value, 3)


# ---------------------------------------------------------------------------
# audio_tracks 校验（§2 + §3.4）
# ---------------------------------------------------------------------------

def _track_text(value: Any, name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ClipAudioError(
            E_AUDIO_TRACK_INVALID, f"audio track 的 {name} 必须是字符串，收到: {value!r}")
    return value.strip()


def _track_optional_text(value: Any, name: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ClipAudioError(
            E_AUDIO_TRACK_INVALID, f"audio track 的 {name} 必须是字符串，收到: {value!r}")
    return value.strip()


def _track_number(value: Any, name: str, default: float, low: float,
                  high: float | None) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ClipAudioError(
            E_AUDIO_TRACK_INVALID, f"audio track 的 {name} 必须是数字，收到: {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ClipAudioError(
            E_AUDIO_TRACK_INVALID, f"audio track 的 {name} 必须是有限数字，收到: {value!r}")
    if number < low or (high is not None and number > high):
        upper = "" if high is None else f"..{high}"
        raise ClipAudioError(
            E_AUDIO_TRACK_INVALID, f"audio track 的 {name} 必须在 {low}{upper} 之间，收到: {number}")
    return number


def _track_int(value: Any, name: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ClipAudioError(
            E_AUDIO_TRACK_INVALID, f"audio track 的 {name} 必须是整数，收到: {value!r}")
    number = float(value)
    if not math.isfinite(number) or number < 0 or number != int(number):
        raise ClipAudioError(
            E_AUDIO_TRACK_INVALID, f"audio track 的 {name} 必须是非负整数，收到: {value!r}")
    return int(number)


def _track_peaks(value: Any) -> list[float]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ClipAudioError(
            E_AUDIO_TRACK_INVALID, f"audio track 的 peaks 必须是数组，收到: {type(value).__name__}")
    peaks: list[float] = []
    for index, item in enumerate(value[:MAX_PEAKS]):
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ClipAudioError(
                E_AUDIO_TRACK_INVALID, f"audio track 的 peaks[{index}] 必须是数字，收到: {item!r}")
        number = float(item)
        if not math.isfinite(number) or number < 0.0 or number > 1.0:
            raise ClipAudioError(
                E_AUDIO_TRACK_INVALID, f"audio track 的 peaks[{index}] 必须在 0..1 之间，收到: {number}")
        peaks.append(number)
    return peaks


def normalize_track(raw: Any) -> dict[str, Any]:
    """严格校验一条 ``audio_tracks`` 条目（类型/范围错 → ``E_AUDIO_TRACK_INVALID``）。

    缺字段补默认（``kind="bgm"``、``volume=1.0``、淡入出 0、``peaks=[]``），
    返回值只含 §3.4 的规范键集合；``peaks`` 超过 ``MAX_PEAKS`` 时截断到 800。
    """

    if not isinstance(raw, dict):
        raise ClipAudioError(
            E_AUDIO_TRACK_INVALID, f"audio track 必须是 JSON 对象，收到: {type(raw).__name__}")

    kind = raw.get("kind")
    if kind is None or kind == "":
        kind = BGM_KINDS[0]
    if not isinstance(kind, str) or kind not in BGM_KINDS:
        raise ClipAudioError(
            E_AUDIO_TRACK_INVALID, f"audio track 的 kind 只能是 {'/'.join(BGM_KINDS)}，收到: {kind!r}")

    return {
        "id": _track_text(raw.get("id"), "id"),
        "kind": kind,
        "asset_id": _track_text(raw.get("asset_id"), "asset_id"),
        "path": _track_text(raw.get("path"), "path"),
        "filename": _track_text(raw.get("filename"), "filename"),
        "volume": _track_number(raw.get("volume"), "volume", 1.0, 0.0, 1.0),
        "fade_in_s": _track_number(raw.get("fade_in_s"), "fade_in_s", 0.0, 0.0, None),
        "fade_out_s": _track_number(raw.get("fade_out_s"), "fade_out_s", 0.0, 0.0, None),
        "duration_s": _track_number(raw.get("duration_s"), "duration_s", 0.0, 0.0, None),
        "peaks": _track_peaks(raw.get("peaks")),
        "sha256": _track_optional_text(raw.get("sha256"), "sha256"),
        "bytes": _track_int(raw.get("bytes"), "bytes", 0),
        "created_at": _track_text(raw.get("created_at"), "created_at"),
    }


def validate_tracks(raw: Any) -> list[dict[str, Any]]:
    """校验整个 ``audio_tracks``（``None`` → ``[]``；不是数组 → 报错）。"""

    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ClipAudioError(
            E_AUDIO_TRACK_INVALID, f"audio_tracks 必须是数组，收到: {type(raw).__name__}")
    return [normalize_track(item) for item in raw]


def peaks_payload(track: dict[str, Any]) -> dict[str, Any]:
    """前端画波形用的载荷（``peaks`` 超过 800 会被截断，用 ``truncated`` 标注）。"""

    if not isinstance(track, dict):
        raise ClipAudioError(
            E_AUDIO_TRACK_INVALID, f"audio track 必须是 JSON 对象，收到: {type(track).__name__}")
    raw_peaks = track.get("peaks")
    truncated = isinstance(raw_peaks, (list, tuple)) and len(raw_peaks) > MAX_PEAKS
    normalized = normalize_track(track)
    peaks = normalized["peaks"]
    return {
        "id": normalized["id"],
        "asset_id": normalized["asset_id"],
        "kind": normalized["kind"],
        "duration_s": normalized["duration_s"],
        "peaks": peaks,
        "peak_count": len(peaks),
        "max_peaks": MAX_PEAKS,
        "truncated": truncated,
    }


# ---------------------------------------------------------------------------
# BGM 资产
# ---------------------------------------------------------------------------

def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_atomic(source_path: Path, destination: Path) -> None:
    """复制到同目录临时文件后 ``os.replace``（不会有半个 BGM 文件被读到）。"""

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.urandom(6).hex()}.tmp")
    try:
        shutil.copyfile(source_path, temporary)
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def save_bgm(
    clip_dir: Path,
    *,
    clip_id: str,
    filename: str,
    source_path: Path,
    max_bytes: int,
) -> dict[str, Any]:
    """校验并落盘一个 BGM 资产，返回 §3.4 的 track 文档。

    顺序：扩展名白名单 → 体积上限 → sha256（内容寻址）→ 复制到
    ``{clip_dir}/audio/bgm-{sha8}{ext}`` → 时长（ffprobe，失败 ``None`` → 记 0.0）
    → 峰值。

    ``track["path"]`` 是**相对 clip_dir** 的路径（``audio/bgm-xxxxxxxx.mp3``），
    调用方拼上 clip 目录即可取到文件；``track["asset_id"]`` 就是文件名主干，
    ``DELETE`` 端点用它定位。

    ``clip_id`` 由签名要求传入，但资产路径已由 clip_dir 唯一确定，故不重复入档。
    """

    clip_dir = Path(clip_dir)
    source_path = Path(source_path)
    safe_name = Path(str(filename or "").strip()).name.strip()
    extension = Path(safe_name).suffix.lower()
    if not safe_name or not extension:
        raise ClipAudioError(
            E_AUDIO_UNSUPPORTED_FORMAT,
            f"音频格式不受支持（缺少扩展名），仅支持 {'/'.join(SUPPORTED_BGM_EXTENSIONS)}")
    if extension not in SUPPORTED_BGM_EXTENSIONS:
        raise ClipAudioError(
            E_AUDIO_UNSUPPORTED_FORMAT,
            f"音频格式 {extension} 不受支持，仅支持 {'/'.join(SUPPORTED_BGM_EXTENSIONS)}")
    if not source_path.is_file():
        raise FileNotFoundError(f"上传的音频文件不存在: {source_path}")

    size = source_path.stat().st_size
    try:
        limit = int(max_bytes)
    except (TypeError, ValueError):
        limit = 0
    if limit > 0 and size > limit:
        raise ClipAudioError(
            E_AUDIO_FILE_TOO_LARGE, f"音频 {size} 字节超过上限 {limit} 字节")

    digest = _sha256_file(source_path)
    asset_id = f"bgm-{digest[:8]}"
    destination = audio_dir(clip_dir) / f"{asset_id}{extension}"
    _copy_atomic(source_path, destination)

    duration = probe_duration(destination)
    peaks = extract_peaks(destination)
    return {
        "id": asset_id,
        "kind": BGM_KINDS[0],
        "asset_id": asset_id,
        "path": f"{AUDIO_DIRNAME}/{destination.name}",
        "filename": safe_name,
        "volume": 1.0,
        "fade_in_s": 0.0,
        "fade_out_s": 0.0,
        "duration_s": float(duration) if duration is not None else 0.0,
        "peaks": peaks,
        "sha256": digest,
        "bytes": destination.stat().st_size,
        "created_at": _utc_now(),
    }


def delete_bgm(clip_dir: Path, asset_id: str) -> dict[str, Any]:
    """删除一个 BGM 资产文件；文件本来就不在时返回 ``removed=False``（幂等）。"""

    clip_dir = Path(clip_dir)
    token = str(asset_id or "").strip()
    if (not token or token.startswith(".") or "/" in token or "\\" in token
            or token in {".", ".."} or token != Path(token).name):
        raise ClipAudioError(E_AUDIO_TRACK_INVALID, f"非法 BGM 资产 id: {asset_id!r}")

    directory = audio_dir(clip_dir)
    target: Path | None = None
    if directory.is_dir():
        for candidate in sorted(directory.iterdir()):
            if candidate.is_file() and candidate.stem == token:
                target = candidate
                break
    if target is None:
        return {"removed": False, "asset_id": token, "path": None}
    try:
        target.unlink()
    except OSError as error:
        raise ClipAudioError(
            E_AUDIO_TRACK_NOT_FOUND, f"删除 BGM {target.name} 失败: {error}") from error
    return {"removed": True, "asset_id": token, "path": f"{AUDIO_DIRNAME}/{target.name}"}
