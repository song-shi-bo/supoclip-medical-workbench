"""FFmpeg re-render of a finished Studio clip through its frame transform + effects.

The Studio player previews ``clip_final.mp4`` with a CSS transform derived
from :mod:`clip_transform` (phase B).  Export must compose the identical
frame, so the very same transform document is turned into a real filter chain
(:func:`clip_transform.build_filter_chain`) and applied to ``clip_final.mp4``
itself — preview and the exported ``output.mp4`` share both the source pixels
and the maths.  Originals are never modified: each render lands in a cache
file next to the clip, keyed by transform + background + source bytes.

成片风格微调（Inspector「效果」标签）扩展同一条链路：

* 画面调整 → ``eq`` / ``unsharp`` / ``colortemperature``；
* 转场（淡入淡出）→ 片段首尾 ``fade=t=in/out``；
* 音频 → 原声 ``volume`` + BGM ``amix``（``sidechaincompress`` 做 ducking）
  + BGM ``afade`` 淡入淡出。

效果/音频快照来自 ``clip_config`` 写出的渲染 sidecar
(:data:`studio_auto_edit_bridge.CLIP_EFFECTS_SIDECAR`)；没有任何改动时
导出短路返回原成片，与对照组逐字节一致。
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import uuid
from pathlib import Path
from typing import Any

from helpers.inventory import ffprobe

from . import subtitle_compose
from .clip_render import _filter_value as _escape_filter_value
from .clip_render import _subtitle_font as _resolve_subtitle_font
from .clip_transform import (
    BACKGROUND_MODES,
    CANVAS_HEIGHT,
    CANVAS_WIDTH,
    NEUTRAL_PICTURE,
    TransformValidationError,
    build_filter_chain,
    is_default_transform,
    is_neutral_picture,
    is_neutral_transition,
    parse_transform,
)
from .studio_auto_edit_bridge import (
    read_clip_render_settings,
    read_clip_transform,
    resolve_auto_edit_artifact,
)

FFMPEG_TIMEOUT_SECONDS = 1800


# ---------------------------------------------------------------------------
# 探测 / 缓存键
# ---------------------------------------------------------------------------

def _probe_media(path: Path) -> dict[str, Any]:
    media = ffprobe(path)
    width, height = int(media.get("width") or 0), int(media.get("height") or 0)
    if width < 2 or height < 2:
        raise ValueError(f"无法读取成片分辨率: {path.name}")
    return {
        "width": width,
        "height": height,
        "duration": float(media.get("duration") or 0.0),
        "has_audio": bool(media.get("audio_codec")),
    }


def _video_size(path: Path) -> tuple[int, int]:
    media = _probe_media(path)
    return int(media["width"]), int(media["height"])


def _cache_key(payload: dict[str, Any], background_mode: str, video: Path,
               settings: Any = None, burn: dict[str, Any] | None = None) -> str:
    digest = hashlib.sha256()
    digest.update(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    if settings is not None:
        digest.update(b"|")
        digest.update(json.dumps(settings, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    digest.update(f"|{background_mode}|transform-export/v3-burn|".encode("utf-8"))
    # 字幕烧录源参与缓存键：用户保存新字幕修订后，旧导出缓存必须失效。
    burn_tag = f"{(burn or {}).get('kind', 'none')}:{(burn or {}).get('sha256', '')}"
    digest.update(f"|burn:{burn_tag}".encode("utf-8"))
    with video.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    for track in _bgm_tracks(video.parent, settings):
        digest.update(str(track.get("path")).encode("utf-8"))
        with track["_file"].open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()[:10]


def clip_video_path(task_id: str, clip_id: str) -> Path:
    """Locate the rendered clip that transform exports operate on."""

    from . import studio_auto_edit_bridge as bridge
    from .studio_clip_locator import clip_video_path as locate_video
    return locate_video(bridge.TASKS_ROOT, task_id, clip_id)


# ---------------------------------------------------------------------------
# 字幕烧录（画布叠加层模型：先变换成 1080x1920 画布，最后烧字幕）
# ---------------------------------------------------------------------------

def _clip_warnings_mode(clip_dir: Path) -> str | None:
    try:
        doc = json.loads((clip_dir / "warnings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    mode = doc.get("subtitle_mode")
    return str(mode) if isinstance(mode, str) else None


def _subtitle_burn_spec(video: Path) -> dict[str, Any] | None:
    """决定导出时是否烧字幕、以什么为源（在 transform 之后的画布上合成）。

    * 底片是旧渲染（warnings.json ``subtitle_mode=burned``，像素里已有字幕）
      → ``None``：跳过烧录保持旧行为，避免字幕叠字幕；底片重渲染后自动升级。
    * 工作台字幕修订（``subtitles/rev-NNN/{clip_id}.ass``）优先——用户显式保存的
      配置样式（字号/位置/底板/卡拉OK），ASS 自带样式且 PlayRes=导出画布。
    * 渲染契约 sidecar ``subtitles.srt`` 兜底——按 subtitle_compose 默认画布样式烧。
    * 无可用 CJK 字体 / 无字幕文件 → ``None``（纯变换行为，不阻断导出）。
    """
    clip_dir = video.parent
    if _clip_warnings_mode(clip_dir) == "burned":
        return None
    font, family = _resolve_subtitle_font()
    if font is None:
        return None
    fonts_dir = str(font.parent)
    try:
        revisions = subtitle_compose.list_revisions(clip_dir)
    except Exception:  # 字幕修订损坏不阻断导出
        revisions = []
    for meta in reversed(revisions or []):
        # 每个 rev 目录恰有一个 {clip_id}.ass；glob 而非拼名，不依赖目录名约定。
        for candidate in sorted(Path(str(meta.get("dir") or "")).glob("*.ass")):
            if candidate.is_file():
                return _burn_spec(candidate, "ass", fonts_dir, family)
    srt_path = clip_dir / "subtitles.srt"
    if srt_path.is_file() and srt_path.stat().st_size > 0:
        return _burn_spec(srt_path, "srt", fonts_dir, family)
    return None


def _burn_spec(path: Path, kind: str, fonts_dir: str, family: str | None) -> dict[str, Any]:
    return {"path": str(path), "kind": kind, "fonts_dir": fonts_dir,
            "family": family or "", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _subtitle_burn_filter(spec: dict[str, Any]) -> str:
    """subtitles 滤镜片段；只能拼在 transform 链之后（画布叠加层最后合成）。"""
    esc = _escape_filter_value
    base = (f"subtitles=filename='{esc(spec['path'])}'"
            f":fontsdir='{esc(spec['fonts_dir'])}'")
    if spec["kind"] == "ass":
        return base  # ASS 自带全部样式且 PlayRes == 导出画布
    margin_v = round(CANVAS_HEIGHT * (100.0 - subtitle_compose.DEFAULT_Y_PCT) / 100.0)
    style = (f"FontName={esc(spec['family'])},Bold=1,"
             f"FontSize={subtitle_compose.DEFAULT_FONT_SIZE},"
             # SRT 无内嵌样式，主色显式给金黄（ASS 修订路径自带样式不经此处）。
             f"PrimaryColour=&H0000D7FF,"
             f"Outline={subtitle_compose.DEFAULT_OUTLINE_WIDTH},Shadow=0,"
             f"MarginL={subtitle_compose.DEFAULT_MARGIN_LR},"
             f"MarginR={subtitle_compose.DEFAULT_MARGIN_LR},MarginV={margin_v}")
    return f"{base}:force_style='{style}'"


# ---------------------------------------------------------------------------
# 效果快照解析
# ---------------------------------------------------------------------------

def _ff(value: float) -> str:
    return f"{value:g}"


def _picture_from_settings(settings: dict[str, Any] | None) -> dict[str, Any] | None:
    """解析出生效的画面调整：新 ``picture`` 优先，旧 ``filter`` 作为兜底。"""

    if not isinstance(settings, dict):
        return None
    effects = settings.get("effects") if isinstance(settings.get("effects"), dict) else {}
    picture = effects.get("picture") if isinstance(effects.get("picture"), dict) else None
    legacy = effects.get("filter") if isinstance(effects.get("filter"), dict) else {}
    legacy_name = str((legacy or {}).get("name") or "none")
    if is_neutral_picture(picture) and legacy_name in ("warm", "cool", "bw"):
        return {**NEUTRAL_PICTURE, "filter": legacy_name}
    return picture


def _transition_from_settings(settings: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(settings, dict):
        return None
    effects = settings.get("effects") if isinstance(settings.get("effects"), dict) else {}
    transition = effects.get("transition")
    return transition if isinstance(transition, dict) else None


def _bgm_tracks(clip_dir: Path, settings: dict[str, Any] | None) -> list[dict[str, Any]]:
    """该 clip 的 BGM 素材（``audio_tracks`` 里 kind=bgm 且文件存在）。"""

    if not isinstance(settings, dict):
        return []
    tracks = settings.get("audio_tracks")
    if not isinstance(tracks, list):
        return []
    result: list[dict[str, Any]] = []
    for track in tracks:
        if not isinstance(track, dict):
            continue
        if str(track.get("kind") or "bgm") != "bgm":
            continue
        raw_path = str(track.get("path") or "").strip()
        if not raw_path:
            continue
        candidate = (clip_dir / raw_path).resolve()
        if clip_dir.resolve() not in candidate.parents:
            continue  # 拒绝穿越：BGM 必须落在该 clip 目录内
        if candidate.is_file():
            result.append({**track, "_file": candidate})
    return result


def _audio_effects_requested(settings: dict[str, Any] | None, bgm: list[dict[str, Any]]) -> bool:
    if not isinstance(settings, dict):
        return False
    if bool(settings.get("muted")):
        return True
    try:
        voice_volume = float(settings.get("voice_volume", 1.0))
    except (TypeError, ValueError):
        voice_volume = 1.0
    if abs(voice_volume - 1.0) > 1e-6:
        return True
    return bool(bgm)


# ---------------------------------------------------------------------------
# 音频滤镜图
# ---------------------------------------------------------------------------

def build_audio_filter_graph(
    *,
    settings: dict[str, Any],
    bgm_tracks: list[dict[str, Any]],
    duration_s: float,
    has_voice: bool,
) -> tuple[list[str], str, bool]:
    """构建音频分支的 filter_complex 片段。

    返回 ``(fragments, output_label, shortest)``：

    * 原声 ``[0:a]volume=<voice_volume>``（muted → 只保留 BGM）；
    * BGM 每条 ``volume`` + ``afade`` 淡入淡出（来自既有 ``audio_tracks`` 字段）；
    * ducking 用 ``sidechaincompress``（原声做 sidechain），并 ``amix`` 回混。

    只允许在该函数的纯字符串层面单测；真正的混音由 ffmpeg 执行。
    """

    fragments: list[str] = []
    muted = bool(settings.get("muted"))
    try:
        voice_volume = max(0.0, min(2.0, float(settings.get("voice_volume", 1.0))))
    except (TypeError, ValueError):
        voice_volume = 1.0
    effects = settings.get("effects") if isinstance(settings.get("effects"), dict) else {}
    audio_effects = effects.get("audio") if isinstance(effects.get("audio"), dict) else {}
    ducking = bool((audio_effects or {}).get("ducking")) and bool(bgm_tracks) and has_voice and not muted
    try:
        ducking_db = float((audio_effects or {}).get("ducking_db", -12.0))
    except (TypeError, ValueError):
        ducking_db = -12.0
    ratio = max(2, min(20, int(round(abs(ducking_db) / 2)) or 6))

    voice_out: str | None = None
    sidechain: str | None = None
    if has_voice and not muted:
        fragments.append(f"[0:a]volume={_ff(voice_volume)}[voice]")
        if ducking:
            fragments.append("[voice]asplit=2[voice_mix][voice_sc]")
            voice_out, sidechain = "[voice_mix]", "[voice_sc]"
        else:
            voice_out = "[voice]"

    bgm_labels: list[str] = []
    for index, track in enumerate(bgm_tracks):
        input_index = 1 + index
        try:
            volume = max(0.0, min(2.0, float(track.get("volume", 1.0))))
        except (TypeError, ValueError):
            volume = 1.0
        chain = f"[{input_index}:a]volume={_ff(volume)}"
        fade_in = _fade_seconds(track.get("fade_in_s"))
        fade_out = _fade_seconds(track.get("fade_out_s"))
        if fade_in > 0:
            chain += f",afade=t=in:st=0:d={_ff(fade_in)}"
        if fade_out > 0 and duration_s > 0:
            chain += (f",afade=t=out:st={_ff(max(0.0, duration_s - fade_out))}"
                      f":d={_ff(fade_out)}")
        order = "major" if index == 0 else "minor"
        label = "[bgm]" if len(bgm_tracks) == 1 else f"[bgm{order}]"
        fragments.append(f"{chain}{label}")
        bgm_labels.append(label)

    if len(bgm_labels) == 1:
        bgm_mix = bgm_labels[0]
    else:
        fragments.append(
            f"{''.join(bgm_labels)}amix=inputs={len(bgm_labels)}:duration=longest:normalize=0[bgm_mix]"
        )
        bgm_mix = "[bgm_mix]"

    shortest = False
    if ducking and sidechain is not None and voice_out is not None:
        fragments.append(
            f"{bgm_mix}{sidechain}sidechaincompress=threshold=0.05:ratio={ratio}"
            f":attack=20:release=300[bgm_duck]"
        )
        bgm_final = "[bgm_duck]"
    else:
        bgm_final = bgm_mix

    if voice_out is not None:
        fragments.append(
            f"{voice_out}{bgm_final}amix=inputs=2:duration=first:normalize=0[a]"
        )
    else:
        # muted 或无原声轨：只输出 BGM，用 -shortest 对齐画面时长。
        fragments.append(f"{bgm_final}anull[a]")
        shortest = True
    return fragments, "[a]", shortest


def _fade_seconds(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        return 0.0
    return number


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------

def export_transformed_clip(
    *,
    task_id: str,
    clip_id: str,
    transform: Any = None,
    background_mode: str = "blur",
    cache_root: Path | None = None,
) -> Path:
    """Render one finished clip through a transform *and* its effects.

    ``transform`` may be ``None`` to fall back to the clip's saved transform
    (task JSON / sidecar).  A default (reset) transform with neutral effects
    and neutral audio short-circuits to the original clip — the compositions
    are identical by definition (control group) — **unless** the clip carries a
    subtitle burn source: deferred subtitles (sidecar SRT / workbench ASS
    revision) only exist after the canvas re-render, so that path always
    renders.  Renders are cached under
    ``cache_root`` (defaults to the clip's own folder); the task output and the
    original file are never modified.
    """

    if background_mode not in BACKGROUND_MODES:
        raise ValueError(f"background 只能是 {'/'.join(BACKGROUND_MODES)}，收到: {background_mode!r}")
    video = clip_video_path(task_id, clip_id)
    if transform is None:
        transform = read_clip_transform(task_id, clip_id)
    settings = read_clip_render_settings(task_id, clip_id)
    picture = _picture_from_settings(settings)
    transition = _transition_from_settings(settings)
    bgm_tracks = _bgm_tracks(video.parent, settings)
    has_video_effects = not is_neutral_picture(picture) or not is_neutral_transition(transition)
    has_audio_effects = _audio_effects_requested(settings, bgm_tracks)
    try:
        parsed = parse_transform(transform)
    except TransformValidationError as exc:
        raise ValueError(f"transform 非法: {exc}") from exc
    # 字幕烧录源（旧 burned 底片 → None）。存在烧录源时中性配置不再短路：
    # 底片本身没有字幕，必须经过画布重渲才能把字幕合成进去。
    burn = _subtitle_burn_spec(video)
    if (is_default_transform(parsed) and not has_video_effects
            and not has_audio_effects and burn is None):
        return video

    media = _probe_media(video)
    duration = float(media["duration"])
    has_voice = bool(media["has_audio"])
    cache_dir = (cache_root or video.parent) / task_id
    key = _cache_key(parsed.to_payload(), background_mode, video,
                     settings if (has_video_effects or has_audio_effects) else None,
                     burn=burn)
    output = cache_dir / f"{clip_id}_{key}.mp4"
    if output.is_file():
        return output

    plan = build_filter_chain(
        int(media["width"]), int(media["height"]), parsed,
        background_mode=background_mode,
        picture=picture, transition=transition, duration_s=duration,
    )
    # 字幕是画布叠加层：必须拼在 transform 链之后最后合成。
    burn_filter = _subtitle_burn_filter(burn) if burn else None
    wants_audio_graph = bool(bgm_tracks) and has_audio_effects
    use_complex = bool(plan.filter_complex) or wants_audio_graph

    command = ["ffmpeg", "-y", "-v", "error", "-i", str(video)]
    for track in bgm_tracks:
        command += ["-i", str(track["_file"])]

    shortest = False
    audio_filtered = False
    if use_complex:
        fragments: list[str] = []
        if plan.filter_complex:
            fragments.append(plan.filter_complex)
        else:
            fragments.append(f"[0:v]{plan.vf}[v]")
        # filter_complex 已以 [v] 结尾时，把烧录链成新输出标签 [vout]。
        video_label = "[v]"
        if burn_filter:
            if plan.filter_complex:
                fragments.append(f"[v]{burn_filter}[vout]")
                video_label = "[vout]"
            else:
                fragments[-1] = f"[0:v]{plan.vf},{burn_filter}[v]"
        if wants_audio_graph:
            audio_fragments, _label, shortest = build_audio_filter_graph(
                settings=settings or {}, bgm_tracks=bgm_tracks,
                duration_s=duration, has_voice=has_voice,
            )
            fragments.extend(audio_fragments)
            audio_filtered = True
        command += ["-filter_complex", ";".join(fragments), "-map", video_label]
        if audio_filtered:
            command += ["-map", "[a]"]
        else:
            command += ["-map", "0:a:0?"]
    else:
        vf_chain = f"{plan.vf},{burn_filter}" if (plan.vf and burn_filter) else (plan.vf or burn_filter or "null")
        command += ["-vf", vf_chain, "-map", "0:v:0"]
        muted = bool((settings or {}).get("muted"))
        try:
            voice_volume = float((settings or {}).get("voice_volume", 1.0))
        except (TypeError, ValueError):
            voice_volume = 1.0
        if muted or voice_volume <= 0:
            command += ["-an"]
        elif abs(voice_volume - 1.0) > 1e-6:
            command += ["-af", f"volume={_ff(voice_volume)}", "-map", "0:a:0?"]
            audio_filtered = True
        else:
            command += ["-map", "0:a:0?"]

    command += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
    ]
    command += ["-c:a", "aac", "-b:a", "192k"] if audio_filtered else ["-c:a", "copy"]
    if shortest or (wants_audio_graph and not has_voice):
        command += ["-shortest"]
    temporary = _temporary(output)
    command += ["-movflags", "+faststart", str(temporary)]

    from .capacity_guard import reserve_capacity, estimate_render_peak
    from .studio_access import heavy_operation, account_derived_output
    with heavy_operation(task_id=task_id, expected_bytes=estimate_render_peak(int(duration * 1000))) as usage, \
         reserve_capacity("transform_export", estimate_render_peak(int(duration * 1000)), path=cache_dir):
        cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            completed = subprocess.run(
                command, text=True, capture_output=True, encoding="utf-8", errors="replace",
                timeout=FFMPEG_TIMEOUT_SECONDS,
            )
            if completed.returncode or not temporary.is_file():
                detail = (completed.stderr or completed.stdout or "")[-800:]
                raise RuntimeError(f"效果导出渲染失败: {detail}")
            temporary.replace(output)
            account_derived_output(usage, output)
            return output
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("效果导出渲染超时") from exc
        finally:
            temporary.unlink(missing_ok=True)


def _temporary(output: Path) -> Path:
    return output.parent / f".{output.stem}.{uuid.uuid4().hex}.tmp.mp4"
