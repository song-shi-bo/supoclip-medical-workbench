"""项目级音频轨模型 + 纯函数式编辑操作 + 真实波形 peaks（Phase A 后端地基）。

定位
----
供 Phase B 的路由调用：路由只做 HTTP ↔ 纯函数 的搬运，编辑语义全在这里，
所以每一次音轨改动都能直接产出一个新的 ``timelineRevision`` payload 交给
:func:`src.timeline_history.push`，撤销/重做即天然成立。

复用而非重写（**不修改** ``clip_audio.py``）
-------------------------------------------
* 真实波形：直接调 ``clip_audio.extract_peaks(asset_path, points=buckets)``
  （ffmpeg → 峰值 → 重采样 → ``[0,1]``），本模块**不重写解码**。
* 前端载荷形状：调 ``clip_audio.peaks_payload(track)`` 拿到
  ``{id, asset_id, kind, duration_s, peaks, peak_count, max_peaks, truncated}``。
  因为 ``clip_audio.normalize_track`` 只白名单 ``kind="bgm"``，本模块用一个
  bgm 信封调用它、再把 ``kind`` 改回真实值（唯一的一处适配，已在代码里标注）。
* 时长：``clip_audio.probe_duration``（失败返回 ``None``，不抛）。
* 错误码：``clip_audio.ClipAudioError``（如 ``E_AUDIO_PEAKS_FAILED``）原样透传，
  便于 Phase B 复用既有的 4xx 映射。

越界策略（二选一，本模块选**钳制**）
------------------------------------
* 负数（任何时间字段、音量、淡入淡出）→ **一律报错** ``E_TIMELINE_AUDIO_TRACK_INVALID``。
* 区间必须有序（``source_end_s > source_start_s``），否则报错。
* 相对**素材时长**的越界（``source_end_s`` 超出素材长度）：仅当调用方传入
  ``asset_durations`` 时才知道真实长度，此时**钳制**到素材末尾并在返回值里体现；
  钳制后区间为空 → 报错。未提供时长时不做越界判断。

预留（本阶段不实现）
--------------------
``trim`` 字段（每个轨道的非破坏性裁剪参数，如 ``{in_s, out_s, keep: [...]}``）本阶段
**只做字段预留与校验**，不实现裁剪语义 —— 见 :data:`TRIM_TODO`。渲染侧在 Phase B/C
落地前应忽略该字段。

落盘
----
peaks 缓存：``<project_dir>/timeline/peaks/<sha256(名字|字节数|mtime_ns|桶数)>.json``。
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from . import clip_audio

__all__ = [
    "AUDIO_KINDS",
    "DEFAULT_VOLUME",
    "E_TIMELINE_AUDIO_ASSET_MISSING",
    "E_TIMELINE_AUDIO_PEAKS_FAILED",
    "E_TIMELINE_AUDIO_REVISION_INVALID",
    "E_TIMELINE_AUDIO_SOURCE_TRACK_RESERVED",
    "E_TIMELINE_AUDIO_TRACK_INVALID",
    "E_TIMELINE_AUDIO_TRACK_NOT_FOUND",
    "PEAKS_CACHE_DIRNAME",
    "PEAKS_DEFAULT_BUCKETS",
    "TIMELINE_DIRNAME",
    "TRIM_TODO",
    "AudioTrack",
    "TimelineAudioError",
    "add_track",
    "asset_durations_from_peaks",
    "clear_peaks_cache",
    "find_track",
    "is_source_track",
    "move_track",
    "new_track_id",
    "normalise_tracks",
    "peaks_cache_dir",
    "peaks_cache_key",
    "peaks_for_asset",
    "remove_track",
    "seed_source_tracks",
    "set_fades",
    "set_muted",
    "set_volume",
    "source_auto_track",
    "track_ids",
    "update_track",
]

TIMELINE_DIRNAME = "timeline"
PEAKS_CACHE_DIRNAME = "peaks"
PEAKS_DEFAULT_BUCKETS = clip_audio.MAX_PEAKS
DEFAULT_VOLUME = 1.0

#: 音轨种类：``voice`` 原声/人声、``bgm`` 背景音乐、``sfx`` 音效。
AUDIO_KINDS = ("voice", "bgm", "sfx")
SOURCE_ORIGIN = "source"
USER_ORIGIN = "user"

TRIM_TODO = "TODO(Phase B/C): trim 字段已预留（非破坏性裁剪），本阶段不实现裁剪语义。"

E_TIMELINE_AUDIO_TRACK_INVALID = "E_TIMELINE_AUDIO_TRACK_INVALID"
E_TIMELINE_AUDIO_TRACK_NOT_FOUND = "E_TIMELINE_AUDIO_TRACK_NOT_FOUND"
E_TIMELINE_AUDIO_REVISION_INVALID = "E_TIMELINE_AUDIO_REVISION_INVALID"
E_TIMELINE_AUDIO_ASSET_MISSING = "E_TIMELINE_AUDIO_ASSET_MISSING"
E_TIMELINE_AUDIO_PEAKS_FAILED = "E_TIMELINE_AUDIO_PEAKS_FAILED"
E_TIMELINE_AUDIO_SOURCE_TRACK_RESERVED = "E_TIMELINE_AUDIO_SOURCE_TRACK_RESERVED"

AudioKind = Literal["voice", "bgm", "sfx"]
TrackOrigin = Literal["source", "user"]


class TimelineAudioError(ValueError):
    """带机器码的显式失败（沿用工作台 ``ValueError`` → HTTP 4xx 的既有映射）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _error(code: str, detail: str) -> TimelineAudioError:
    return TimelineAudioError(code, f"{code}: {detail}")


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# 数字校验助手
# ---------------------------------------------------------------------------

def _number(value: Any, name: str, *, low: float = 0.0, high: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _error(E_TIMELINE_AUDIO_TRACK_INVALID,
                     f"{name} 必须是数字，收到 {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number):
        raise _error(E_TIMELINE_AUDIO_TRACK_INVALID, f"{name} 必须是有限数字，收到 {value!r}")
    if number < low:
        raise _error(E_TIMELINE_AUDIO_TRACK_INVALID, f"{name} 不能小于 {low}，收到 {number}")
    if high is not None and number > high:
        raise _error(E_TIMELINE_AUDIO_TRACK_INVALID, f"{name} 不能大于 {high}，收到 {number}")
    return number


def _optional_text(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _error(E_TIMELINE_AUDIO_TRACK_INVALID,
                     f"{name} 必须是字符串或 null，收到 {type(value).__name__}")
    text = value.strip()
    return text or None


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

class AudioTrack(BaseModel):
    """一条音频轨（``timelineRevision.audio_tracks[]``）。

    ==================  ===================  ====================================
    字段                 类型 / 范围          说明
    ==================  ===================  ====================================
    track_id            str（必填）          项目内唯一；用 :func:`new_track_id` 生成
    kind                voice|bgm|sfx        音轨种类
    asset_id            str|null             素材库 id（可选）
    path                str|null             素材文件路径（可选，peaks 由它生成）
    source_start_s      float ≥ 0            素材内的入点
    source_end_s        float > start        素材内的出点（相对素材，不是时间线）
    record_start_s      float ≥ 0            时间线上的落点
    volume              float 0..1           音量（默认 1.0）
    muted               bool                 静音
    fade_in_s           float ≥ 0            淡入
    fade_out_s          float ≥ 0            淡出
    label               str|null             显示名
    origin              source|user          ``source`` = 导入/自动剪辑自动进入
    trim                dict|null            **预留**，本阶段不实现（见 TRIM_TODO）
    ==================  ===================  ====================================
    """

    model_config = ConfigDict(extra="allow")

    track_id: str
    kind: AudioKind = "bgm"
    asset_id: str | None = None
    path: str | None = None
    source_start_s: float = 0.0
    source_end_s: float = 0.0
    record_start_s: float = 0.0
    volume: float = DEFAULT_VOLUME
    muted: bool = False
    fade_in_s: float = 0.0
    fade_out_s: float = 0.0
    label: str | None = None
    origin: TrackOrigin = USER_ORIGIN
    trim: dict[str, Any] | None = None

    @field_validator("track_id", mode="before")
    @classmethod
    def _validate_track_id(cls, value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"track_id 必须是非空字符串，收到 {value!r}")
        return value.strip()

    @field_validator("kind", mode="before")
    @classmethod
    def _validate_kind(cls, value: Any) -> str:
        if not isinstance(value, str) or value not in AUDIO_KINDS:
            raise ValueError(f"kind 只能是 {'/'.join(AUDIO_KINDS)}，收到 {value!r}")
        return value

    @field_validator("origin", mode="before")
    @classmethod
    def _validate_origin(cls, value: Any) -> str:
        if value is None or value == "":
            return USER_ORIGIN
        if value not in (SOURCE_ORIGIN, USER_ORIGIN):
            raise ValueError(f"origin 只能是 {SOURCE_ORIGIN}/{USER_ORIGIN}，收到 {value!r}")
        return value

    @field_validator("source_start_s", "source_end_s", "record_start_s",
                     "fade_in_s", "fade_out_s", mode="before")
    @classmethod
    def _validate_seconds(cls, value: Any, info: Any) -> float:
        if value is None:
            value = 0.0
        return _number(value, info.field_name)

    @field_validator("volume", mode="before")
    @classmethod
    def _validate_volume(cls, value: Any) -> float:
        if value is None:
            return DEFAULT_VOLUME
        return _number(value, "volume", low=0.0, high=1.0)

    @field_validator("muted", mode="before")
    @classmethod
    def _validate_muted(cls, value: Any) -> bool:
        if value is None:
            return False
        if not isinstance(value, bool):
            raise ValueError(f"muted 必须是布尔值，收到 {type(value).__name__}")
        return value

    @field_validator("asset_id", "path", "label", mode="before")
    @classmethod
    def _validate_optional_text(cls, value: Any, info: Any) -> str | None:
        return _optional_text(value, info.field_name)

    @field_validator("trim", mode="before")
    @classmethod
    def _validate_trim(cls, value: Any) -> dict[str, Any] | None:
        #: 预留字段：只校验形状，不解释内容（裁剪语义见 TRIM_TODO）。
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError(f"trim 必须是 JSON 对象或 null，收到 {type(value).__name__}")
        return dict(value)


def _normalise_track(
    raw: Any, *, asset_durations: dict[str, float] | None = None, where: str = "audio_tracks"
) -> dict[str, Any]:
    """单条轨道：模型校验 + 有序性/越界策略（钳制），返回规范 dict。"""

    if not isinstance(raw, dict):
        raise _error(E_TIMELINE_AUDIO_TRACK_INVALID,
                     f"{where} 的元素必须是 JSON 对象，收到 {type(raw).__name__}")
    try:
        track = AudioTrack.model_validate(raw)
    except ValidationError as error:
        details = "; ".join(
            f"{'.'.join(str(part) for part in item['loc']) or '<root>'}: {item['msg']}"
            for item in error.errors()
        )
        raise _error(E_TIMELINE_AUDIO_TRACK_INVALID, f"{where} 非法: {details}") from error

    payload = track.model_dump(mode="json")
    start = payload["source_start_s"]
    end = payload["source_end_s"]

    duration = _asset_duration(payload, asset_durations)
    if duration is not None:
        # 越界策略：钳制到素材末尾（并保证入点不越界）。
        clamped_start = min(start, duration)
        clamped_end = min(end, duration)
        if clamped_end <= clamped_start:
            raise _error(
                E_TIMELINE_AUDIO_TRACK_INVALID,
                f"{where} 的 {payload['track_id']} 区间非法: 素材时长 {duration}s，"
                f"钳制后 [{clamped_start}, {clamped_end}] 为空",
            )
        payload["source_start_s"] = clamped_start
        payload["source_end_s"] = clamped_end
    elif end <= start:
        raise _error(
            E_TIMELINE_AUDIO_TRACK_INVALID,
            f"{where} 的 {payload['track_id']} 区间必须有序: source_end_s({end}) "
            f"必须大于 source_start_s({start})",
        )

    if payload["fade_in_s"] + payload["fade_out_s"] > (payload["source_end_s"] - payload["source_start_s"]):
        # 淡入淡出只影响渲染混音，超出区间属于配置错误（钳制到区间长度，保持可用）。
        span = payload["source_end_s"] - payload["source_start_s"]
        if payload["fade_in_s"] > span:
            payload["fade_in_s"] = span
        if payload["fade_out_s"] > span:
            payload["fade_out_s"] = span
    return payload


def _asset_duration(
    track: dict[str, Any], asset_durations: dict[str, float] | None
) -> float | None:
    if not asset_durations:
        return None
    for key in (track.get("asset_id"), track.get("path")):
        if not key:
            continue
        value = asset_durations.get(key)
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number) and number > 0:
            return number
    return None


def normalise_tracks(
    raw: Any, *, asset_durations: dict[str, float] | None = None
) -> list[dict[str, Any]]:
    """校验整个 ``audio_tracks`` 数组。

    * ``None`` → ``[]``；不是数组 → ``E_TIMELINE_AUDIO_TRACK_INVALID``。
    * ``track_id`` 重复 → 报错（时间线不允许同 id 两条轨）。
    * 越界策略：见模块 docstring（需要 ``asset_durations`` 才钳制）。
    """

    if raw is None:
        return []
    if not isinstance(raw, list):
        raise _error(E_TIMELINE_AUDIO_TRACK_INVALID,
                     f"audio_tracks 必须是数组，收到 {type(raw).__name__}")
    tracks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        track = _normalise_track(item, asset_durations=asset_durations,
                                 where=f"audio_tracks[{index}]")
        if track["track_id"] in seen:
            raise _error(E_TIMELINE_AUDIO_TRACK_INVALID,
                         f"track_id 重复: {track['track_id']!r}")
        seen.add(track["track_id"])
        tracks.append(track)
    return tracks


# ---------------------------------------------------------------------------
# 轨道查询助手
# ---------------------------------------------------------------------------

def new_track_id(kind: str = "bgm") -> str:
    """生成轨道 id（本模块唯一非纯函数；其余变换都是纯函数）。"""

    prefix = kind if isinstance(kind, str) and kind in AUDIO_KINDS else "bgm"
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def track_ids(revision: Any) -> list[str]:
    """当前 revision 里的轨道 id（顺序即时间线顺序）。"""

    return [str(item.get("track_id", "")) for item in _tracks_of(_revision_dict(revision))]


def find_track(revision: Any, track_id: str) -> dict[str, Any] | None:
    """按 id 查轨道；找不到返回 ``None``（写操作用 :func:`_require_track` 报错）。"""

    for item in _tracks_of(_revision_dict(revision)):
        if str(item.get("track_id")) == str(track_id):
            return copy.deepcopy(item)
    return None


def is_source_track(track: Any) -> bool:
    """是否是「导入/自动剪辑自动进入时间线」的原声轨（``origin == "source"``）。"""

    return isinstance(track, dict) and track.get("origin") == SOURCE_ORIGIN


def source_auto_track(
    *, track_id: str | None = None, kind: str = "voice", asset_id: str | None = None,
    path: str | None = None, duration_s: float = 0.0, label: str | None = "原始视频原声",
) -> dict[str, Any]:
    """构造导入/自动剪辑时**自动**进入时间线的原声轨（``origin="source"``）。

    Phase B 在导入与自动剪辑落地后调用它并 push 一次 revision；``＋`` 菜单由
    :func:`add_track` 的守卫拦住，不允许再加第二条同素材的源轨。
    """

    span = _number(duration_s, "duration_s")
    return AudioTrack(
        track_id=track_id or new_track_id(kind),
        kind=kind,
        asset_id=asset_id,
        path=path,
        source_start_s=0.0,
        source_end_s=span,
        record_start_s=0.0,
        volume=DEFAULT_VOLUME,
        muted=False,
        fade_in_s=0.0,
        fade_out_s=0.0,
        label=label,
        origin=SOURCE_ORIGIN,
    ).model_dump(mode="json")


# ---------------------------------------------------------------------------
# revision 变换（纯函数：输入 revision → 输出新 revision，绝不原地修改）
# ---------------------------------------------------------------------------

def _revision_dict(revision: Any) -> dict[str, Any]:
    if isinstance(revision, dict):
        return copy.deepcopy(revision)
    if hasattr(revision, "model_dump"):
        data = revision.model_dump(mode="json")
        if isinstance(data, dict):
            return data
    raise _error(E_TIMELINE_AUDIO_REVISION_INVALID,
                 f"revision 必须是 JSON 对象或 TimelineRevision，收到 {type(revision).__name__}")


def _tracks_of(revision: dict[str, Any]) -> list[dict[str, Any]]:
    tracks = revision.get("audio_tracks")
    if tracks is None:
        return []
    if not isinstance(tracks, list):
        raise _error(E_TIMELINE_AUDIO_TRACK_INVALID,
                     f"revision.audio_tracks 必须是数组，收到 {type(tracks).__name__}")
    return tracks


def _require_track(revision: dict[str, Any], track_id: str) -> dict[str, Any]:
    for item in _tracks_of(revision):
        if str(item.get("track_id")) == str(track_id):
            return item
    raise _error(E_TIMELINE_AUDIO_TRACK_NOT_FOUND, f"找不到音轨: {track_id!r}")


def _resolved(revision: dict[str, Any], asset_durations: dict[str, float] | None) -> dict[str, Any]:
    """把 revision 的 audio_tracks 规范一遍（校验 + 钳制）后返回新的 revision。"""

    payload = copy.deepcopy(revision)
    payload["audio_tracks"] = normalise_tracks(
        payload.get("audio_tracks"), asset_durations=asset_durations)
    return payload


def _same_asset(track: dict[str, Any], other: dict[str, Any]) -> bool:
    for key in ("asset_id", "path"):
        left, right = track.get(key), other.get(key)
        if left and right and str(left) == str(right):
            return True
    return False


def seed_source_tracks(
    revision: Any,
    tracks: list[dict[str, Any]],
    *,
    asset_durations: dict[str, float] | None = None,
) -> dict[str, Any]:
    """**系统路径**：导入/自动剪辑完成后把原始视频原声轨自动放进时间线。

    与 :func:`add_track`（用户 ``＋`` 菜单）的区别：
    * 这里允许 ``origin="source"`` 的轨道（它们是自动进入的，不是用户加的）；
    * 幂等：同 ``track_id`` 或同素材的源轨已存在 → **跳过**，不会产生重复轨，
      所以「重新导入/重跑自动剪辑」不会把原声叠加两次。
    """

    if not isinstance(tracks, list):
        raise _error(E_TIMELINE_AUDIO_TRACK_INVALID,
                     f"tracks 必须是数组，收到 {type(tracks).__name__}")
    payload = _revision_dict(revision)
    existing = normalise_tracks(payload.get("audio_tracks"), asset_durations=asset_durations)
    known_ids = {str(item.get("track_id")) for item in existing}
    source_assets = [item for item in existing if is_source_track(item)]

    for index, item in enumerate(tracks):
        candidate = _normalise_track(item, asset_durations=asset_durations,
                                     where=f"source_tracks[{index}]")
        candidate["origin"] = SOURCE_ORIGIN
        if str(candidate["track_id"]) in known_ids:
            continue
        if any(_same_asset(existing_item, candidate) for existing_item in source_assets):
            continue
        existing.append(candidate)
        known_ids.add(str(candidate["track_id"]))
        source_assets.append(candidate)
    payload["audio_tracks"] = existing
    return payload


def add_track(
    revision: Any,
    track: dict[str, Any],
    *,
    asset_durations: dict[str, float] | None = None,
) -> dict[str, Any]:
    """新增音轨（``＋`` 菜单的落点）。

    守卫：``origin="source"`` 的轨道由导入/自动剪辑自动生成，**不能用 ＋ 重复添加**；
    同一素材的源轨已存在时再加同素材轨道 → ``E_TIMELINE_AUDIO_SOURCE_TRACK_RESERVED``。
    返回新的 revision（调用方接着 ``timeline_history.push``）。

    系统侧自动进入时间线请用 :func:`seed_source_tracks`，不要放宽这里的守卫。
    """

    payload = _revision_dict(revision)
    tracks = normalise_tracks(payload.get("audio_tracks"), asset_durations=asset_durations)
    candidate = _normalise_track(track, asset_durations=asset_durations, where="new track")
    if is_source_track(candidate):
        raise _error(
            E_TIMELINE_AUDIO_SOURCE_TRACK_RESERVED,
            "原始视频/原声在导入或自动剪辑后自动进入时间线，不能用 ＋ 重复添加",
        )
    for existing in tracks:
        if is_source_track(existing) and _same_asset(existing, candidate):
            raise _error(
                E_TIMELINE_AUDIO_SOURCE_TRACK_RESERVED,
                f"素材 {candidate.get('asset_id') or candidate.get('path')!r} 的原声轨已自动进入"
                "时间线，不能用 ＋ 重复添加",
            )
    if any(str(item.get("track_id")) == candidate["track_id"] for item in tracks):
        raise _error(E_TIMELINE_AUDIO_TRACK_INVALID,
                     f"track_id 已存在: {candidate['track_id']!r}")
    tracks.append(candidate)
    payload["audio_tracks"] = tracks
    return payload


def remove_track(revision: Any, track_id: str) -> dict[str, Any]:
    """删除音轨；找不到 → ``E_TIMELINE_AUDIO_TRACK_NOT_FOUND``。"""

    payload = _revision_dict(revision)
    tracks = normalise_tracks(payload.get("audio_tracks"))
    remaining = [item for item in tracks if str(item.get("track_id")) != str(track_id)]
    if len(remaining) == len(tracks):
        raise _error(E_TIMELINE_AUDIO_TRACK_NOT_FOUND, f"找不到音轨: {track_id!r}")
    payload["audio_tracks"] = remaining
    return payload


def move_track(
    revision: Any,
    track_id: str,
    *,
    record_start_s: float | None = None,
    source_start_s: float | None = None,
    asset_durations: dict[str, float] | None = None,
) -> dict[str, Any]:
    """移动音轨：改时间线落点（``record_start_s``）或素材入点（``source_start_s``）。

    只改素材入点时**保持区间长度不变**（``source_end_s`` 同步平移），避免移动变成裁剪。
    """

    payload = _revision_dict(revision)
    track = copy.deepcopy(_require_track(payload, track_id))
    if record_start_s is None and source_start_s is None:
        raise _error(E_TIMELINE_AUDIO_TRACK_INVALID,
                     "move_track 至少需要 record_start_s 或 source_start_s 之一")
    if record_start_s is not None:
        track["record_start_s"] = _number(record_start_s, "record_start_s")
    if source_start_s is not None:
        start = _number(source_start_s, "source_start_s")
        span = track["source_end_s"] - track["source_start_s"]
        track["source_start_s"] = start
        track["source_end_s"] = start + span
    payload = update_track(payload, track_id, track, asset_durations=asset_durations)
    return payload


def update_track(
    revision: Any,
    track_id: str,
    patch: dict[str, Any],
    *,
    asset_durations: dict[str, float] | None = None,
) -> dict[str, Any]:
    """按 id 局部更新（``track_id`` 不可改）；返回新的 revision。"""

    if not isinstance(patch, dict):
        raise _error(E_TIMELINE_AUDIO_TRACK_INVALID,
                     f"patch 必须是 JSON 对象，收到 {type(patch).__name__}")
    payload = _revision_dict(revision)
    tracks = normalise_tracks(payload.get("audio_tracks"))
    merged: list[dict[str, Any]] = []
    found = False
    for item in tracks:
        if str(item.get("track_id")) != str(track_id):
            merged.append(item)
            continue
        found = True
        candidate = {**item, **{key: value for key, value in patch.items() if key != "track_id"}}
        merged.append(_normalise_track(candidate, asset_durations=asset_durations,
                                       where=f"track {track_id}"))
    if not found:
        raise _error(E_TIMELINE_AUDIO_TRACK_NOT_FOUND, f"找不到音轨: {track_id!r}")
    payload["audio_tracks"] = merged
    return payload


def set_volume(revision: Any, track_id: str, volume: float) -> dict[str, Any]:
    """设置音量（0..1）。"""

    value = _number(volume, "volume", low=0.0, high=1.0)
    return update_track(revision, track_id, {"volume": value})


def set_muted(revision: Any, track_id: str, muted: bool) -> dict[str, Any]:
    """设置静音。"""

    if not isinstance(muted, bool):
        raise _error(E_TIMELINE_AUDIO_TRACK_INVALID,
                     f"muted 必须是布尔值，收到 {type(muted).__name__}")
    return update_track(revision, track_id, {"muted": muted})


def set_fades(
    revision: Any,
    track_id: str,
    *,
    fade_in_s: float | None = None,
    fade_out_s: float | None = None,
    asset_durations: dict[str, float] | None = None,
) -> dict[str, Any]:
    """设置淡入/淡出（秒，非负）。"""

    if fade_in_s is None and fade_out_s is None:
        raise _error(E_TIMELINE_AUDIO_TRACK_INVALID,
                     "set_fades 至少需要 fade_in_s 或 fade_out_s 之一")
    patch: dict[str, Any] = {}
    if fade_in_s is not None:
        patch["fade_in_s"] = _number(fade_in_s, "fade_in_s")
    if fade_out_s is not None:
        patch["fade_out_s"] = _number(fade_out_s, "fade_out_s")
    return update_track(revision, track_id, patch, asset_durations=asset_durations)


# ---------------------------------------------------------------------------
# 真实波形 peaks（复用 clip_audio）+ 磁盘缓存
# ---------------------------------------------------------------------------

def peaks_cache_dir(project_dir: str | os.PathLike[str]) -> Path:
    """``<project_dir>/timeline/peaks``。"""

    return Path(project_dir) / TIMELINE_DIRNAME / PEAKS_CACHE_DIRNAME


def peaks_cache_key(asset_path: str | os.PathLike[str], buckets: int) -> str:
    """缓存键 = sha256(文件名 | 字节数 | mtime_ns | 桶数)。

    文件改名会换键（保守，宁可重算），文件**内容**变化必然改 mtime/大小 → 自动失效。
    加文件名的原因：同尺寸同 mtime 的不同素材也不会互相命中。
    """

    path = Path(asset_path)
    stat = path.stat()
    material = f"{path.name}|{stat.st_size}|{stat.st_mtime_ns}|{int(buckets)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _peaks_payload_from_clip_audio(
    *, track_id: str, asset_id: str | None, kind: str, duration_s: float, peaks: list[float]
) -> dict[str, Any]:
    """用 ``clip_audio.peaks_payload`` 生成前端载荷；非 bgm 只改 kind 字段。

    ``clip_audio.normalize_track`` 的白名单只有 ``"bgm"``，所以这里用 bgm 信封调用
    既有校验/截断逻辑（``max_peaks=800``、``truncated`` 标注），然后把 ``kind``
    改回真实种类。**这是本模块唯一的适配层**，没有改 ``clip_audio.py``。
    """

    payload = clip_audio.peaks_payload({
        "id": track_id,
        "asset_id": asset_id or "",
        "kind": "bgm",
        "duration_s": duration_s,
        "peaks": peaks,
    })
    payload["kind"] = kind
    return payload


def peaks_for_asset(
    project_dir: str | os.PathLike[str],
    asset_path: str | os.PathLike[str],
    *,
    buckets: int = PEAKS_DEFAULT_BUCKETS,
    kind: str = "bgm",
    asset_id: str | None = None,
    track_id: str | None = None,
) -> dict[str, Any]:
    """真实波形 peaks（带磁盘缓存）。

    * 首次/失效：``clip_audio.extract_peaks(asset_path, points=buckets)``（ffmpeg 解码）。
    * 命中缓存：不调用 ffmpeg，直接回读 ``timeline/peaks/<key>.json``。
    * ``clip_audio.ClipAudioError``（如 ``E_AUDIO_PEAKS_FAILED``）**原样透传**机器码。
    * 返回：``clip_audio.peaks_payload`` 的形状 + 缓存元数据（``cache_key`` /
      ``cache_path`` / ``cached`` / ``asset`` / ``buckets``）。
    """

    if not isinstance(kind, str) or kind not in AUDIO_KINDS:
        raise _error(E_TIMELINE_AUDIO_TRACK_INVALID,
                     f"kind 只能是 {'/'.join(AUDIO_KINDS)}，收到 {kind!r}")
    try:
        requested = int(buckets)
    except (TypeError, ValueError):
        raise _error(E_TIMELINE_AUDIO_TRACK_INVALID,
                     f"buckets 必须是整数，收到 {buckets!r}") from None
    # clip_audio 内部硬钳到 MAX_PEAKS(800)，这里先对齐，缓存键才与真实结果一致。
    requested = max(1, min(clip_audio.MAX_PEAKS, requested))

    path = Path(asset_path)
    if not path.is_file():
        raise _error(E_TIMELINE_AUDIO_ASSET_MISSING, f"素材文件不存在: {path}")

    key = peaks_cache_key(path, requested)
    cache_file = peaks_cache_dir(project_dir) / f"{key}.json"
    cached = _read_peaks_cache(cache_file, key)
    if cached is not None:
        return {**cached, "cached": True, "cache_key": key, "cache_path": str(cache_file)}

    from .studio_access import heavy_operation, account_derived_output
    with heavy_operation(project_id=Path(project_dir).name, expected_bytes=1024 * 1024) as usage:
        try:
            peaks = clip_audio.extract_peaks(path, requested)
        except clip_audio.ClipAudioError as error:
            # 复用既有机器码（E_AUDIO_PEAKS_FAILED），HTTP 层映射不变。
            raise TimelineAudioError(error.code, error.message) from error

        duration = clip_audio.probe_duration(path) or 0.0
        payload = _peaks_payload_from_clip_audio(
            track_id=track_id or f"asset_{key[:12]}",
            asset_id=asset_id,
            kind=kind,
            duration_s=duration,
            peaks=list(peaks),
        )
        stat = path.stat()
        stored = {
            "version": 1,
            #: 文档标记（不要用 "kind"：那是音轨种类，由下面 **payload 提供）。
            "document": "timeline_peaks",
            "cache_key": key,
            "asset": {
                "name": path.name,
                "path": str(path),
                "bytes": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            },
            "buckets": requested,
            "max_peaks": clip_audio.MAX_PEAKS,
            "source": "clip_audio.extract_peaks",
            "created_at": _utc_now(),
            **payload,
        }
        _atomic_json(cache_file, stored)
        account_derived_output(usage, cache_file)
        return {**stored, "cached": False, "cache_key": key, "cache_path": str(cache_file)}


def _read_peaks_cache(cache_file: Path, key: str) -> dict[str, Any] | None:
    """读缓存；损坏/键不符/peaks 非法 → ``None``（退回重算，不抛错）。"""

    if not cache_file.is_file():
        return None
    try:
        raw = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or raw.get("cache_key") != key:
        return None
    peaks = raw.get("peaks")
    if not isinstance(peaks, list) or not peaks:
        return None
    if any(not isinstance(item, (int, float)) or isinstance(item, bool) for item in peaks):
        return None
    return raw


def clear_peaks_cache(project_dir: str | os.PathLike[str]) -> int:
    """删除整个 peaks 缓存目录，返回删除的文件数。"""

    directory = peaks_cache_dir(project_dir)
    if not directory.is_dir():
        return 0
    removed = 0
    for item in sorted(directory.glob("*.json")):
        try:
            item.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def asset_durations_from_peaks(
    peaks_payload: dict[str, Any],
) -> dict[str, float]:
    """把 :func:`peaks_for_asset` 的结果转成 ``asset_durations``（给钳制校验用）。"""

    duration = peaks_payload.get("duration_s")
    try:
        value = float(duration)
    except (TypeError, ValueError):
        return {}
    if not math.isfinite(value) or value <= 0:
        return {}
    asset = peaks_payload.get("asset") if isinstance(peaks_payload.get("asset"), dict) else {}
    durations: dict[str, float] = {}
    for key in (peaks_payload.get("asset_id"), asset.get("name"), asset.get("path")):
        if key:
            durations[str(key)] = value
    return durations


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.urandom(6).hex()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
