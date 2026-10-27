"""封面替换 / 沿用 / 回滚通道（cover@2 工作台侧，轻量秒级，不整片重渲）。

背景与职责
----------
成片里的「封面」其实有两处：``{output}/{task_id}/{clip_id}/cover.jpg``（封面图，
资产卡与导出封面用）和 ``clip_final.mp4`` **前 0.8s 的烧录片头**（封面静帧 +
封面文字）。只换 jpg 会让视频片头还是旧封面，所以本模块把两处一起换，并保证
可一键回滚：

- :func:`replace_cover` —— 按封面风格（``realframe_v2`` / ``cutout_v3``）重出封面，
  再换掉封面图与 0.8s 片头；``realframe_v2`` 自 2026-10-18 起改走**本地**
  ``cover_realframe_v2`` 固化管线（真实视频帧 + Pillow 文字叠加，不调 video-brain、
  不调任何 AI 图片生成），只有 ``cutout_v3`` 仍调 video-brain ``/v1/tools/cover.render``；
- :func:`adopt_cover` —— 把同项目某条成片**已生成**的 ``cover.jpg`` 沿用到目标
  成片（纯复用，不调用生成链路）；
- :func:`restore_cover` —— 从 ``cover_backup/`` 一键回滚到改动前的原文件；
- :func:`read_cover_state` —— 读备份/替换台账，供 UI 展示与幂等判断。

三条硬约束（对应任务书 A/B 项）
------------------------------
1. **原文件先备份再动**：首次改动前把 ``cover.jpg`` 与 ``clip_final.mp4`` 复制到
   同目录 ``cover_backup/``（带 UTC 时间戳），台账写在 ``cover_replace.json``；
   任何一步失败都自动回滚并报 ``E_COVER_QC_FAILED`` 之类带码错误。
2. **片头替换不改时长/帧数**：片头用「新封面静帧片段 + 关键帧切点之后的流拷贝」
   三段 concat 完成——尾部帧与原文件**逐位一致**，总时长/总帧数与原片一致
   （±1 帧），不重编码整片。
3. **幂等**：同一 ``(task_id, clip_id, key)`` 重复执行不叠加备份、结果一致
   （第二次返回 ``idempotent=True`` 的 noop）。

片头切点为什么必须落在关键帧
----------------------------
``ffmpeg -ss T -c copy`` 只会 seek 到 T 之前最近的关键帧，落在非关键帧上会重复
播放若干帧、总帧数偏多。video-brain 渲染的成片本身就是「封面片头段 + 正片段」
concat 出来的，第二段必然以关键帧开头（实测 25fps 成片：帧 20 = 0.822969s 是
关键帧，正好等于 0.8s 片头）。所以正常情况下 ``bridge_frames == 0``，替换全程
无损；只有病态文件（片头边界不在关键帧上）才需要重编码最多一个 GOP 的桥接帧，
计量结果会如实写进 QC 的 ``bridge_frames``。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .studio_auto_edit_bridge import auto_edit_clip_dir

# ---------------------------------------------------------------------------
# 配置与错误码
# ---------------------------------------------------------------------------

VIDEO_BRAIN_URL = os.getenv("VIDEO_BRAIN_URL", "http://127.0.0.1:8940").rstrip("/")
VIDEO_BRAIN_HOME = Path(os.getenv("VIDEO_BRAIN_HOME", "/domain/video-brain")).resolve()
COVER_STYLES_CONFIG = Path(
    os.getenv("COVER_STYLES_CONFIG", str(VIDEO_BRAIN_HOME / "config" / "cover_styles.json"))
)

# 成员两版风格：可「按风格重新生成」。
SUPPORTED_COVER_STYLES: tuple[str, ...] = ("realframe_v2", "cutout_v3")
# 未指定风格时的缺省：第二版·真实帧大字封面（不依赖抠像权重，永远可用）。
DEFAULT_COVER_STYLE = "realframe_v2"
# 存量冻结风格：允许被「沿用/只读」引用，但不由本通道生成。
LEGACY_COVER_STYLE = "medical_big_text_v3"
# 风格路由用的兜底片头时长（配置文件读不到时）；与 config/cover_styles.json
# 的 intro.duration_s 及 video-brain render.COVER_DUR 保持一致。
DEFAULT_INTRO_DURATION_S = 0.8
DEFAULT_FADE_IN_S = 0.18

COVER_BACKUP_DIRNAME = "cover_backup"
COVER_STATE_FILENAME = "cover_replace.json"
# 封面修订快照：cover_revisions/rev-001/ …（v1 = 基线原封面，v2.. = 每次成功重制）
# 故意放在 cover_backup/ **之外**：cover_backup/ 的语义是「首次改动前的基线备份」，
# 它的条目数被既有契约当作「有没有叠加备份」的证据，不能被历史版本污染。
COVER_REVISIONS_DIRNAME = "cover_revisions"
COVER_REVISION_META = "revision.json"
# 固化封面规则（任务书 §三）：主钩子总长 ≤12 个汉字、最多两行。
HOOK_MAX_TOTAL_CHARS = 12
HOOK_MAX_LINES = 2
COVER_FILENAME = "cover.jpg"
COVER_LAYOUT_FILENAME = "cover_layout.json"
CLIP_FILENAME = "clip_final.mp4"

E_COVER_STYLE_INVALID = "E_COVER_STYLE_INVALID"
E_COVER_CLIP_MISSING = "E_COVER_CLIP_MISSING"
E_COVER_SOURCE_MISSING = "E_COVER_SOURCE_MISSING"
E_COVER_VB_UNAVAILABLE = "E_COVER_VB_UNAVAILABLE"
E_COVER_STYLE_UNAVAILABLE = "E_COVER_STYLE_UNAVAILABLE"
E_COVER_RENDER_FAILED = "E_COVER_RENDER_FAILED"
E_COVER_FFMPEG_MISSING = "E_COVER_FFMPEG_MISSING"
E_COVER_QC_FAILED = "E_COVER_QC_FAILED"
E_COVER_BACKUP_MISSING = "E_COVER_BACKUP_MISSING"
E_COVER_BAD_REQUEST = "E_COVER_BAD_REQUEST"
# 新版型封面工作流（2026-10-21 应用任务化）：没有待应用草稿时不能「应用」。
E_COVER_DRAFT_MISSING = "E_COVER_DRAFT_MISSING"

FFMPEG_TIMEOUT_SECONDS = 600
PROBE_TIMEOUT_SECONDS = 120
HTTP_TIMEOUT_SECONDS = 300
# QC 里逐帧 MD5 比对的最大帧数：尾部是 ``-c copy`` 直接搬的包，逐位一致由构造保证，
# 全片比对只是为了留证据。取 400 帧（25fps ≈ 16s）既够证据又不会让长成片的 QC
# 退化成正片两遍解码。真实端到端验收另行做全片比对（见交付报告的验证章节）。
QC_MD5_MAX_FRAMES = 400

# ffmpeg 解析顺序：显式环境变量 → PATH → 本机已知绝对路径。
# studio_server 由 supervisor 拉起，PATH 未必与交互式 shell 相同，因此不能只靠 PATH。
FFMPEG_CANDIDATES = (
    "/domain/dsh-home/bin/ffmpeg",
    "/domain/.ffbin/ffmpeg",
    "/usr/local/bin/ffmpeg",
    "/usr/bin/ffmpeg",
)
FFPROBE_CANDIDATES = (
    "/domain/dsh-home/bin/ffprobe",
    "/domain/.ffbin/ffprobe",
    "/usr/local/bin/ffprobe",
    "/usr/bin/ffprobe",
)


class CoverReplaceError(ValueError):
    """带 E_ 码的显式失败（沿用既有错误映射：ValueError → HTTP 400）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _atomic_copy(source: Path, destination: Path) -> None:
    """同目录原子替换：先写临时文件再 ``os.replace``，避免半截文件被前端读到。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    shutil.copy2(source, temporary)
    temporary.replace(destination)


# ---------------------------------------------------------------------------
# 外部工具解析与探测（纯逻辑，可单测）
# ---------------------------------------------------------------------------

def resolve_binary(env_key: str, name: str, candidates: tuple[str, ...]) -> str:
    """解析 ffmpeg/ffprobe：环境变量 → PATH → 已知绝对路径；都没有则显式报错。"""
    override = os.getenv(env_key)
    if override and Path(override).is_file():
        return override
    found = shutil.which(name)
    if found:
        return found
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    raise CoverReplaceError(
        E_COVER_FFMPEG_MISSING,
        f"找不到 {name}（尝试过 ${env_key}、PATH 与 {list(candidates)}）；"
        "请在标题栏「工具与帮助」提交系统依赖请求，或把路径写进 "
        f"{env_key} 环境变量",
    )


def resolve_ffmpeg() -> str:
    return resolve_binary("STUDIO_FFMPEG", "ffmpeg", FFMPEG_CANDIDATES)


def resolve_ffprobe() -> str:
    return resolve_binary("STUDIO_FFPROBE", "ffprobe", FFPROBE_CANDIDATES)


def _run(command: list[str], timeout: int, ffmpeg_env: dict[str, str] | None = None
         ) -> subprocess.CompletedProcess:
    return subprocess.run(
        command, text=True, capture_output=True, encoding="utf-8", errors="replace",
        timeout=timeout, env=ffmpeg_env,
    )


def probe_streams(path: Path) -> dict[str, Any]:
    """ffprobe 读成片参数；QC 与命令构造的唯一事实来源。

    关键帧列表只取到 ``frame_scan_limit`` 帧（默认 600，足够覆盖任何片头切点），
    避免长成片把帧级扫描拖成秒级。
    """
    ffprobe = resolve_ffprobe()
    command = [
        ffprobe, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", "-show_entries",
        "stream=index,codec_type,codec_name,profile,level,width,height,pix_fmt,"
        "r_frame_rate,avg_frame_rate,time_base,nb_frames,duration,sample_rate,"
        "channels,channel_layout,bit_rate",
        str(path),
    ]
    completed = _run(command, PROBE_TIMEOUT_SECONDS)
    if completed.returncode != 0:
        raise CoverReplaceError(
            E_COVER_QC_FAILED, f"ffprobe 读取成片参数失败: {(completed.stderr or '')[-300:]}")
    payload = json.loads(completed.stdout or "{}")
    video = next((s for s in payload.get("streams", [])
                  if s.get("codec_type") == "video"), None)
    audio = next((s for s in payload.get("streams", [])
                  if s.get("codec_type") == "audio"), None)
    if video is None:
        raise CoverReplaceError(E_COVER_QC_FAILED, f"成片没有视频流: {path.name}")
    fps = _parse_fraction(video.get("r_frame_rate") or video.get("avg_frame_rate") or "0/0")
    if fps <= 0:
        raise CoverReplaceError(E_COVER_QC_FAILED, f"无法解析成片帧率: {video.get('r_frame_rate')}")
    frames = int(video.get("nb_frames") or 0)
    if frames <= 0:
        frames = count_frames(path)
    duration = float(video.get("duration") or payload.get("format", {}).get("duration") or 0.0)
    return {
        "path": str(path),
        "fps": fps,
        "frames": frames,
        "duration_s": duration,
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "pix_fmt": video.get("pix_fmt") or "yuv420p",
        "vcodec": video.get("codec_name") or "h264",
        "profile": video.get("profile"),
        "level": video.get("level"),
        "has_audio": audio is not None,
        "acodec": (audio or {}).get("codec_name"),
        "sample_rate": int((audio or {}).get("sample_rate") or 44100),
        "channels": int((audio or {}).get("channels") or 1),
        "channel_layout": (audio or {}).get("channel_layout") or "mono",
        "audio_bit_rate": int((audio or {}).get("bit_rate") or 128000),
    }


def _parse_fraction(value: str) -> float:
    try:
        numerator, _, denominator = str(value).partition("/")
        if not denominator:
            return float(numerator)
        denominator_value = float(denominator)
        return float(numerator) / denominator_value if denominator_value else 0.0
    except (TypeError, ValueError):
        return 0.0


def count_frames(path: Path) -> int:
    """逐帧计数（只在容器缺 nb_frames 时兜底，代价较高）。"""
    ffprobe = resolve_ffprobe()
    completed = _run([
        ffprobe, "-v", "error", "-select_streams", "v:0", "-count_frames",
        "-show_entries", "stream=nb_read_frames", "-of", "default=nw=1:nk=1", str(path),
    ], PROBE_TIMEOUT_SECONDS)
    try:
        return int((completed.stdout or "0").strip() or 0)
    except ValueError:
        return 0


def keyframe_list(path: Path, limit: int = 600) -> list[dict[str, Any]]:
    """视频关键帧列表 ``[{"frame": 序号, "time_s": 真实 pts 时间}]``（只扫前 ``limit`` 帧）。

    **必须带真实 pts 时间**：成片常有几十毫秒的起始偏移（实测本机成片首帧 pts
    0.022969s、25fps），``ffmpeg -ss <frame/fps> -c copy`` 会 seek 到更早的关键帧
    （帧 0），把旧封面整段重新拷进尾部。用真实 pts（0.822969）才是精确落点。
    """
    ffprobe = resolve_ffprobe()
    completed = _run([
        ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "frame=key_frame,pts_time", "-of", "csv=p=0", str(path),
    ], PROBE_TIMEOUT_SECONDS)
    if completed.returncode != 0:
        return []
    rows = [row.strip() for row in (completed.stdout or "").splitlines() if row.strip()]
    keyframes: list[dict[str, Any]] = []
    for index, row in enumerate(rows[:limit]):
        parts = row.split(",")
        if parts[0].strip() != "1":
            continue
        try:
            time_s = float(parts[1]) if len(parts) > 1 and parts[1].strip() else -1.0
        except ValueError:
            time_s = -1.0
        keyframes.append({"frame": index, "time_s": time_s})
    return keyframes


def frame_md5_list(path: Path, start_time_s: float = 0.0, limit: int = 0) -> list[str]:
    """视频帧解码后的 MD5 列表（「逐位一致」的判据；只比视频，避开 AAC 帧边界噪声）。

    用 ``-ss``（解码寻址，帧精确）而不是 ``select`` 过滤器：切点本身就是关键帧，
    ``-ss`` 落点确定，且不受过滤器表达式转义影响。
    """
    ffmpeg = resolve_ffmpeg()
    command = [ffmpeg, "-v", "error"]
    if start_time_s > 0:
        command += ["-ss", f"{start_time_s:.6f}"]
    command += ["-i", str(path), "-map", "0:v:0", "-f", "framemd5", "-"]
    completed = _run(command, FFMPEG_TIMEOUT_SECONDS)
    hashes = [row.split(",")[-1].strip()
              for row in (completed.stdout or "").splitlines()
              if row.strip() and not row.startswith("#")]
    if limit > 0:
        hashes = hashes[:limit]
    return hashes


def _parse_ssim(output: str) -> float | None:
    """从 ffmpeg ssim 过滤器日志里取 ``All:`` 分数（形如 ``All:0.992164 (21.05)``）。"""
    match = re.search(r"All:\s*(-?\d+(?:\.\d+)?)", output or "")
    return float(match.group(1)) if match else None


def frame_similarity_to_image(video: Path, ts: float, image: Path, workdir: Path) -> float:
    """视频 ``ts`` 处帧 vs 图片的 SSIM（[0,1]，1 = 完全一致）。

    用于 QC 断言「新片头第 0.2s 的帧 == 新封面」。只用 ffmpeg 自带的 ``ssim``
    过滤器（灰度、统一缩放到 1080x1920），**不依赖 numpy/PIL** —— 后端 venv 里
    有这些库，但让封面通道只依赖 ffmpeg 更稳（沙箱/测试环境同样可跑）。

    实测判别力：同一封面 0.992；换风格后的旧封面 0.908；正片画面 0.783。
    阈值取 0.95，既能接受 h264 有损压缩的微小差异，又能识破「片头其实还是旧封面」。
    """
    ffmpeg = resolve_ffmpeg()
    workdir.mkdir(parents=True, exist_ok=True)
    frame_png = workdir / f"qc-frame-{uuid.uuid4().hex[:8]}.png"
    try:
        completed = _run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, ts):.3f}",
             "-i", str(video), "-frames:v", "1", str(frame_png)], FFMPEG_TIMEOUT_SECONDS)
        if completed.returncode != 0 or not frame_png.is_file():
            raise CoverReplaceError(
                E_COVER_QC_FAILED, f"抽帧比对失败: {(completed.stderr or '')[-200:]}")
        compared = _run([
            ffmpeg, "-hide_banner", "-i", str(frame_png), "-i", str(image),
            "-lavfi", "[0:v]scale=1080:1920,format=gray[a];"
                      "[1:v]scale=1080:1920,format=gray[b];[a][b]ssim",
            "-f", "null", "-",
        ], FFMPEG_TIMEOUT_SECONDS)
        score = _parse_ssim(f"{compared.stdout}\n{compared.stderr}")
        if score is None:
            raise CoverReplaceError(
                E_COVER_QC_FAILED,
                f"无法解析封面相似度（ssim 输出异常）: {(compared.stderr or '')[-200:]}")
        return round(score, 4)
    finally:
        frame_png.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 片头切点规划与 ffmpeg 命令构造（纯逻辑，可单测）
# ---------------------------------------------------------------------------

def plan_intro_split(fps: float, total_frames: int, keyframes: list[dict[str, Any]],
                     target_frames: int) -> dict[str, Any]:
    """决定片头替换的切点（不碰文件系统，便于单测）。

    - ``keyframes``：``[{"frame": 序号, "time_s": 真实 pts 时间}]``（见 :func:`keyframe_list`）；
    - ``target_frames``：要替换掉的片头帧数（默认 ``round(intro_s * fps)``）；
    - ``split_frame``：尾部流拷贝的起点 = 第一个 **≥ target_frames** 的关键帧；
    - ``split_time_s``：该关键帧的真实 pts（流拷贝 seek 用它，不用 frame/fps）；
    - ``bridge_frames = split_frame - target_frames``：需要重编码的过渡帧数
      （0 = 全程无损；>0 = 该文件片头边界不在关键帧上，最多一个 GOP）；
    - 若 target 之后没有关键帧（病态文件）：不再强求无损，``tail_copy=False``，
      尾部整体重编码，此时 ``split_frame = target_frames``。
    """
    if fps <= 0:
        raise CoverReplaceError(E_COVER_BAD_REQUEST, f"帧率非法: {fps}")
    if total_frames <= 0:
        raise CoverReplaceError(E_COVER_BAD_REQUEST, f"成片帧数非法: {total_frames}")
    if target_frames <= 0:
        raise CoverReplaceError(E_COVER_BAD_REQUEST, f"片头帧数必须 > 0，收到 {target_frames}")
    if target_frames >= total_frames:
        raise CoverReplaceError(
            E_COVER_BAD_REQUEST,
            f"片头 {target_frames} 帧 ≥ 成片总帧数 {total_frames}，拒绝把整片替换成封面静帧")
    candidates = sorted(
        ((int(item.get("frame") or 0), float(item.get("time_s") or -1.0))
         for item in keyframes if isinstance(item, dict) and int(item.get("frame") or 0) > 0),
        key=lambda pair: pair[0])
    picked = next((pair for pair in candidates if pair[0] >= target_frames), None)
    tail_copy = picked is not None
    if picked is None:
        split_frame, split_time = target_frames, target_frames / fps
    else:
        split_frame, split_time = picked
        if split_time <= 0:
            split_time = split_frame / fps
    return {
        "target_frames": int(target_frames),
        "head_frames": int(target_frames),
        "split_frame": int(split_frame),
        "split_time_s": round(float(split_time), 6),
        "bridge_frames": int(split_frame - target_frames),
        "tail_frames": int(total_frames - split_frame),
        "head_duration_s": round(target_frames / fps, 6),
        "mode": "exact_copy" if tail_copy and split_frame == target_frames
                else ("bridge_reencode" if tail_copy else "tail_reencode"),
        "tail_copy": bool(tail_copy),
    }


def _video_encode_args(params: dict[str, Any], crf: int, preset: str) -> list[str]:
    """与原片严格一致的视频编码参数（profile/level/pix_fmt 必须与尾部流拷贝匹配）。"""
    args = [
        "-c:v", "libx264",
        "-preset", preset,
        "-crf", str(crf),
        "-pix_fmt", params.get("pix_fmt") or "yuv420p",
    ]
    if params.get("profile"):
        args += ["-profile:v", _x264_profile(params["profile"])]
    if params.get("level"):
        args += ["-level:v", f"{float(params['level']) / 10:.1f}"]
    args += ["-r", _fps_arg(params.get("fps") or 25.0)]
    return args


def _audio_encode_args(params: dict[str, Any]) -> list[str]:
    codec = params.get("acodec") or "aac"
    bitrate = max(64000, int(params.get("audio_bit_rate") or 128000))
    return [
        "-c:a", codec,
        "-b:a", f"{round(bitrate / 1000)}k",
        "-ar", str(int(params.get("sample_rate") or 44100)),
        "-ac", str(int(params.get("channels") or 1)),
    ]


def _x264_profile(profile: str) -> str:
    lowered = str(profile).strip().lower()
    if "baseline" in lowered:
        return "baseline"
    if "main" in lowered:
        return "main"
    if "high 10" in lowered or "high10" in lowered:
        return "high10"
    return "high"


def _fps_arg(fps: float) -> str:
    """把浮点帧率写成可被 ffmpeg 精确解析的形式（29.97 → 30000/1001 近似不做，保留小数）。"""
    if float(fps).is_integer():
        return str(int(fps))
    return f"{fps:.6f}".rstrip("0").rstrip(".")


def build_head_command(ffmpeg: str, cover_jpg: Path, source: Path, out: Path,
                       params: dict[str, Any], plan: dict[str, Any], *,
                       crf: int = 20, preset: str = "veryfast",
                       fade_in_s: float = DEFAULT_FADE_IN_S) -> list[str]:
    """新封面静帧片段：``head_frames`` 帧、时长 = ``head_duration_s``，带原片同款淡入。

    音频：有音轨时取原片头对应时段（``-ss 0 -t dur -i source``）——「保留原片头
    对应时段的音频」；无音轨时补静音（``anullsrc``），保证 concat 各段流结构一致。
    """
    duration = plan["head_duration_s"]
    command = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-loop", "1", "-i", str(cover_jpg),
    ]
    filters = [f"fade=t=in:st=0:d={fade_in_s}", f"format={params.get('pix_fmt') or 'yuv420p'}"]
    if params.get("has_audio"):
        command += ["-ss", "0", "-t", f"{duration:.6f}", "-i", str(source)]
        command += ["-map", "0:v:0", "-map", "1:a:0", "-af", "apad"]
    else:
        command += [
            "-f", "lavfi", "-t", f"{duration:.6f}",
            "-i", f"anullsrc=r={int(params.get('sample_rate') or 44100)}"
                  f":cl={params.get('channel_layout') or 'mono'}",
        ]
        command += ["-map", "0:v:0", "-map", "1:a:0", "-af", "apad"]
    command += ["-vf", ",".join(filters), "-frames:v", str(plan["head_frames"])]
    command += _video_encode_args(params, crf, preset)
    command += _audio_encode_args(params)
    command += ["-t", f"{duration:.6f}", "-shortest", "-movflags", "+faststart", str(out)]
    return command


def build_bridge_command(ffmpeg: str, source: Path, out: Path, params: dict[str, Any],
                         plan: dict[str, Any], *, crf: int = 20,
                         preset: str = "veryfast") -> list[str]:
    """过渡片段：原片 ``[target_frames, split_frame)`` 的重编码版（≤1 个 GOP）。

    只有片头边界不落在关键帧上时才需要；正常成片 ``bridge_frames == 0``，这条命令
    根本不会被构造。
    """
    duration = round((plan["split_frame"] - plan["target_frames"]) / float(params["fps"]), 6)
    command = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{plan['head_duration_s']:.6f}", "-i", str(source),
        "-map", "0:v:0",
    ]
    if params.get("has_audio"):
        command += ["-map", "0:a:0", "-af", "apad"]
    command += ["-vf", f"format={params.get('pix_fmt') or 'yuv420p'}",
                "-frames:v", str(plan["bridge_frames"])]
    command += _video_encode_args(params, crf, preset)
    command += _audio_encode_args(params)
    command += ["-t", f"{duration:.6f}", "-shortest", "-movflags", "+faststart", str(out)]
    return command


def build_tail_command(ffmpeg: str, source: Path, out: Path, params: dict[str, Any],
                       plan: dict[str, Any]) -> list[str]:
    """尾部片段：``-c copy`` 精确切割（关键帧起点 → 无重编码，帧逐位一致）。

    无音轨但需要补静音时退化为「视频流拷贝 + 静音 AAC」：视频帧仍然逐位一致，
    只有音频是合成的。
    """
    command = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{plan['split_time_s']:.6f}", "-i", str(source),
    ]
    if not plan.get("tail_copy"):
        # 病态文件：切点之后没有关键帧，只能重编码尾部（QC 会记录 tail_reencoded）
        command += ["-map", "0:v:0"]
        if params.get("has_audio"):
            command += ["-map", "0:a:0"]
        command += ["-vf", f"format={params.get('pix_fmt') or 'yuv420p'}"]
        command += _video_encode_args(params, 20, "veryfast")
        if params.get("has_audio"):
            command += _audio_encode_args(params)
    elif params.get("has_audio"):
        command += ["-map", "0", "-c", "copy"]
    else:
        command += [
            "-map", "0:v:0", "-c:v", "copy",
            "-f", "lavfi", "-t",
            f"{plan['tail_frames'] / float(params['fps']):.6f}",
            "-i", f"anullsrc=r={int(params.get('sample_rate') or 44100)}"
                  f":cl={params.get('channel_layout') or 'mono'}",
            "-map", "1:a:0",
        ]
        command += _audio_encode_args(params)
        command += ["-shortest"]
    command += ["-movflags", "+faststart", str(out)]
    return command


def build_concat_command(ffmpeg: str, list_path: Path, out: Path) -> list[str]:
    """concat demuxer + ``-c copy``：整片不重编码，只有片头是新的。"""
    return [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(list_path),
        "-c", "copy", "-movflags", "+faststart", str(out),
    ]


# ---------------------------------------------------------------------------
# 风格配置与 video-brain 联动
# ---------------------------------------------------------------------------

def load_style_config() -> dict[str, Any]:
    """读 cover_styles.json；缺失/损坏时回退内置缺省（服务不挂）。"""
    try:
        return json.loads(COVER_STYLES_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"default_style_id": LEGACY_COVER_STYLE, "styles": {}}


def style_intro_duration(style_id: str | None) -> float:
    """风格的 ``intro.duration_s``（片头时长；读不到时 0.8s）。"""
    config = load_style_config()
    resolved = style_id or config.get("default_style_id") or LEGACY_COVER_STYLE
    style = (config.get("styles") or {}).get(resolved) or {}
    intro = style.get("intro") or {}
    try:
        return float(intro.get("duration_s", DEFAULT_INTRO_DURATION_S))
    except (TypeError, ValueError):
        return DEFAULT_INTRO_DURATION_S


def style_fade_in(style_id: str | None) -> float:
    config = load_style_config()
    resolved = style_id or config.get("default_style_id") or LEGACY_COVER_STYLE
    style = (config.get("styles") or {}).get(resolved) or {}
    try:
        return float((style.get("intro") or {}).get("fade_in_s", DEFAULT_FADE_IN_S))
    except (TypeError, ValueError):
        return DEFAULT_FADE_IN_S


def _chain_style(purpose: str) -> str:
    """按链路读清单缺省风格（读不到时退回代码兜底）。"""
    try:
        from . import studio_workflow
        return studio_workflow.cover_style_choice(purpose=purpose)[0]
    except Exception:  # noqa: BLE001
        return DEFAULT_COVER_STYLE


def style_availability() -> dict[str, Any]:
    """各风格可用性（含 cutout_v3 缺权重时的**明确原因**与安装路径提示）。"""
    info: dict[str, Any] = {
        "realframe_v2": {"available": True, "requires_weights": False,
                         "label": "第二版·真实帧大字封面", "reason": ""},
        "cutout_v3": {"available": True, "requires_weights": True,
                      "label": "第三版·人物抠像大字封面（需 u2net 权重）",
                      "weights_path": str(Path.home() / ".u2net" / "u2net.onnx"),
                      "reason": ""},
    }
    ok, err = cutout_v3_available()
    if not ok:
        info["cutout_v3"].update({
            "available": False,
            "reason": err,
            "install_hint": ("uv pip install --python /domain/video-brain/.venv/bin/python "
                            "rembg onnxruntime；离线环境把 u2net.onnx 预置到 "
                            "~/.u2net/u2net.onnx"),
        })
    return info


def cutout_weights_path() -> Path:
    """u2net 权重路径（与 video-brain 的 ``~/.u2net/u2net.onnx`` 同一约定）。"""
    home = os.environ.get("U2NET_HOME")
    base = Path(home) if home else (Path.home() / ".u2net")
    return base / "u2net.onnx"


def cutout_v3_available() -> tuple[bool, str]:
    """cutout_v3 是否可用：探测抠像后端 + u2net 权重（显式原因，不静默降级）。

    权重缺失是**最常见**的不可用原因，所以先报 ``E_CUTOUT_WEIGHTS_MISSING``
    （带可安装路径），再报后端缺失。
    """
    weights = cutout_weights_path()
    if not weights.is_file():
        return False, (f"E_CUTOUT_WEIGHTS_MISSING: u2net 权重未就绪（{weights} 不存在）；"
                       "首次推理需外网下载，离线环境请预置 u2net.onnx")
    backend = Path("/domain/video-brain/.venv/lib/python3.11/site-packages/rembg")
    if not backend.is_dir():
        try:
            import importlib.util
            if importlib.util.find_spec("rembg") is None:
                return False, ("E_CUTOUT_BACKEND_UNAVAILABLE: 封面服务缺少 rembg 抠像后端"
                               "（请在 video-brain 的虚拟环境安装 rembg + onnxruntime）")
        except Exception as exc:  # noqa: BLE001
            return False, f"E_CUTOUT_BACKEND_UNAVAILABLE: 抠像后端探测失败（{exc}）"
    return True, ""


def regenerate_default_style() -> tuple[str, str]:
    """「用户主动重新制作封面」缺省风格 → ``(style_id, source)``。

    读成员规则清单 ``cover_rules.style_for_regenerate``（缺省 cutout_v3·人物抠像）；
    清单不可读时退回 ``DEFAULT_COVER_STYLE``。**只读规则，不在这里写死风格**。
    """
    try:
        from . import studio_workflow
        return studio_workflow.cover_style_choice(purpose="regenerate")
    except Exception:  # noqa: BLE001 — 规则层不可读时不让封面通道整体失败
        return DEFAULT_COVER_STYLE, "code_fallback"


def validate_style_id(style_id: str | None, *, allow_legacy: bool = False) -> str:
    """风格白名单校验；返回规范化后的 style_id。

    ``None`` → 缺省取规则清单 ``cover_rules.style_for_regenerate``（当前为
    ``cutout_v3``·第三版人物抠像大字封面）；该风格需要 u2net 权重，权重缺失时
    下游**显式失败**（``E_COVER_STYLE_UNAVAILABLE`` / ``E_CUTOUT_WEIGHTS_MISSING``），
    绝不静默换成 v2。
    """
    if style_id is None or str(style_id).strip() == "":
        return regenerate_default_style()[0]
    value = str(style_id).strip()
    allowed = set(SUPPORTED_COVER_STYLES) | ({LEGACY_COVER_STYLE} if allow_legacy else set())
    if value not in allowed:
        raise CoverReplaceError(
            E_COVER_STYLE_INVALID,
            f"不支持的封面风格 {value!r}；可用: {sorted(allowed)}",
        )
    return value


# ---------------------------------------------------------------------------
# 固化封面文案规则：主钩子 ≤12 汉字、最多两行
# ---------------------------------------------------------------------------

def _cjk_len(text: str) -> int:
    """封面规则按**汉字**计数：空白与标点不计入上限（排版符号不占主题额度）。"""
    return sum(1 for char in str(text or "") if not char.isspace() and not _is_punct(char))


def _is_punct(char: str) -> bool:
    return char in "，。、；：！？…—·「」『』（）()《》〈〉“”\"'’‘-,.;:!?/\\|~`@#$%^&*+=[]{}<>"


def _split_two_lines(text: str, max_total: int) -> tuple[str, str]:
    """把一段文字按「两行、总长 ≤ max_total、每行 ≥2」切开。"""
    per_line = max(2, max_total // 2)
    head = text[:per_line]
    tail = text[per_line:max_total]
    if len(tail) < 2:                       # 第二行不足 2 字：往回匀一个字
        head, tail = text[:max(2, per_line - 1)], text[max(2, per_line - 1):max_total]
    return head, tail


def clamp_cover_copy(cover_copy: dict[str, Any], *,
                     max_hook_chars: int = HOOK_MAX_TOTAL_CHARS,
                     max_lines: int = HOOK_MAX_LINES) -> tuple[dict[str, Any], dict[str, Any]]:
    """按固化规则把封面文案的主钩子压到 ``max_hook_chars`` 个汉字、最多 ``max_lines`` 行。

    返回 ``(新文案, 报告)``；报告里 ``changed``/``reason``/``before``/``after`` 供 UI 与
    任务结果如实展示（「主题太长 → 已压到 12 字内」必须对用户可见）。

    约束来源：``CoverCopy`` 要求 ``headline_line_1/2`` 各 2~16 字、``top_theme`` 2~6 字、
    且每个字段都要有证据段 id。因此压缩只动钩子两行，**逐字段证据原样保留**；
    两行之和已 ≤12 时一字不改（幂等）。
    """
    copy_ = dict(cover_copy or {})
    line_1 = str(copy_.get("headline_line_1") or "")
    line_2 = str(copy_.get("headline_line_2") or "")
    lines = [line for line in (line_1, line_2) if line.strip()][:max_lines]
    total = _cjk_len("".join(lines))
    report: dict[str, Any] = {
        "before": {"line_1": line_1, "line_2": line_2, "total_chars": total},
        "changed": False,
        "max_hook_chars": max_hook_chars,
        "max_lines": max_lines,
        "reason": None,
    }
    if total <= max_hook_chars and len(lines) <= max_lines:
        report["after"] = {"line_1": line_1, "line_2": line_2, "total_chars": total}
        return copy_, report

    merged = "".join(lines)
    if len(merged) < 4:
        # 不足两行各 2 字的硬下限（CoverCopy schema 要求每行 ≥2 字），无法安全压缩——
        # 不猜、不截断，保持原样并显式说明，交由上游（LLM/人工）改写。
        report["reason"] = "文案不足 4 字，无法同时满足两行各 ≥2 字；未压缩"
        report["after"] = {"line_1": line_1, "line_2": line_2, "total_chars": total}
        return copy_, report
    head, tail = _split_two_lines(merged, max_hook_chars)
    copy_["headline_line_1"] = head
    copy_["headline_line_2"] = tail
    report["changed"] = True
    report["reason"] = f"主钩子超过 {max_hook_chars} 字，已压缩到「{head}{tail}」"
    report["after"] = {"line_1": head, "line_2": tail,
                       "total_chars": _cjk_len(head) + _cjk_len(tail)}
    return copy_, report


def _http_json(method: str, path: str, payload: dict[str, Any] | None = None,
               timeout: float = HTTP_TIMEOUT_SECONDS) -> tuple[int, Any]:
    """调 video-brain；HTTP 错误也返回响应体（错误码在 detail 里），网络错误显式抛出。"""
    request = urllib.request.Request(
        f"{VIDEO_BRAIN_URL}{path}",
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            return response.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body).get("detail")
        except (ValueError, AttributeError):
            detail = body[:400]
        return exc.code, {"detail": detail}
    except urllib.error.URLError as exc:
        raise CoverReplaceError(
            E_COVER_VB_UNAVAILABLE,
            f"固定剪辑流程（video-brain）不可达 {VIDEO_BRAIN_URL}：{exc.reason}；"
            "请确认 8940 服务在运行",
        ) from exc


def _video_brain_task_id(task_id: str, clip_dir: Path) -> str:
    """从成片产物 ``clip_plan.json`` 里取该任务的 video-brain 任务号。"""
    plan_path = clip_dir.parent / "clip_plan.json"
    if plan_path.is_file():
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            value = str(plan.get("video_brain_task_id") or "").strip()
            if value:
                return value
        except (OSError, ValueError):
            pass
    raise CoverReplaceError(
        E_COVER_VB_UNAVAILABLE,
        f"任务 {task_id} 的产地里没有 video_brain_task_id，无法按风格重新生成封面",
    )


def _render_cover_realframe(task_id: str, clip_id: str, clip_dir: Path,
                            cover_copy_override: dict[str, Any] | None = None) -> dict[str, Any]:
    """realframe_v2 专用本地渲染：真实帧 + 文字叠加（不调 video-brain）。

    - 帧源 = 该切片成片 ``clip_final.mp4`` 本身（观众看到的真实画面），切片窗 =
      整条成片 ``[0, duration]``；
    - 文案优先级：``cover_copy_override``（UPDATE_TITLE 显式改写）→ 本地
      ``cover_copy.json`` 既有最终文案 → ``clip_plan.json`` topic 的规则回退；
    - 幂等/版本由 :mod:`cover_realframe_v2` 台账承担，本通道的备份/回滚/revision
      机制照旧（封面图与片头替换流程不变）。
    """
    from . import cover_realframe_v2

    clip_final = clip_dir / CLIP_FILENAME
    if not clip_final.is_file():
        raise CoverReplaceError(E_COVER_CLIP_MISSING, f"成片不存在：{clip_final}")
    probe = probe_streams(clip_final)
    duration_ms = int(round(float(probe["duration_s"]) * 1000))

    topic_hint, keywords = _clip_topic_hint(task_id, clip_id, clip_dir)
    override: dict[str, Any] | None = None
    local_copy = _read_local_final_copy(clip_dir)
    if isinstance(cover_copy_override, dict) and cover_copy_override:
        override = _override_to_topic_hook(cover_copy_override, topic_hint)
    elif isinstance(local_copy, dict) and local_copy.get("topic"):
        override = {"topic": str(local_copy["topic"]), "hook": str(local_copy.get("hook") or ""),
                    "segment_ids": list(local_copy.get("segment_ids") or [])}

    result = cover_realframe_v2.build_cover(
        clip_final, project_id=str(task_id), clip_id=clip_id,
        clip_start_ms=0, clip_end_ms=max(1, duration_ms), segments=[],
        out_dir=clip_dir, analysis_keywords=keywords, topic_hint=topic_hint,
        copy_override=override, force=True,
    )
    layout_path = clip_dir / COVER_LAYOUT_FILENAME
    cover_copy = result.get("cover_copy") or {}
    return {
        "cover_path": Path(result["cover_path"]),
        "layout_path": Path(result["cover_layout_path"]),
        "layout": json.loads(layout_path.read_text(encoding="utf-8")) if layout_path.is_file() else {},
        "video_brain_task_id": None,                 # 本地管线：无 video-brain 任务号
        "frame_ts_in_base": float(result.get("frame_time_rel_s") or 0.0),
        "base_video": str(clip_final),
        "response": {"renderer": "cover_realframe_v2/local",
                     "cover_revision": result.get("cover_revision"),
                     "copy_source": result.get("copy_source"),
                     "products": result.get("products") or {}},
        "cover_copy": {"top_theme": cover_copy.get("topic"),
                       "headline_line_1": cover_copy.get("hook"),
                       "headline_line_2": ""},
        "copy_report": None,
    }


def _render_cover_template(task_id: str, clip_id: str, clip_dir: Path,
                           template_id: str,
                           cover_copy_override: dict[str, Any] | None = None) -> dict[str, Any]:
    """封面版型本地渲染（2026-10-21 阶段）：真实帧 + 版面契约排版，**不调用模型**。

    - 版面契约来自版本化注册表（``cover_templates.template_render_spec``），
      未知版型 → ``E_TEMPLATE_UNKNOWN``；
    - 帧选择/裁切/人脸避让/文案回退链（copy_override → cover_copy.json →
      clip_plan topic）与 realframe_v2 完全同源；
    - 产物（cover.jpg/cover_layout.json/…）与 revision 台账由 build_cover 统一管理。
    """
    from . import cover_realframe_v2, cover_templates

    spec = cover_templates.template_render_spec(template_id)  # 未知 → E_TEMPLATE_UNKNOWN
    clip_final = clip_dir / CLIP_FILENAME
    if not clip_final.is_file():
        raise CoverReplaceError(E_COVER_CLIP_MISSING, f"成片不存在：{clip_final}")
    probe = probe_streams(clip_final)
    duration_ms = int(round(float(probe["duration_s"]) * 1000))

    topic_hint, keywords = _clip_topic_hint(task_id, clip_id, clip_dir)
    override: dict[str, Any] | None = None
    local_copy = _read_local_final_copy(clip_dir)
    if isinstance(cover_copy_override, dict) and cover_copy_override:
        override = _override_to_topic_hook(cover_copy_override, topic_hint)
    elif isinstance(local_copy, dict) and local_copy.get("topic"):
        override = {"topic": str(local_copy["topic"]), "hook": str(local_copy.get("hook") or ""),
                    "segment_ids": list(local_copy.get("segment_ids") or [])}

    result = cover_realframe_v2.build_cover(
        clip_final, project_id=str(task_id), clip_id=clip_id,
        clip_start_ms=0, clip_end_ms=max(1, duration_ms), segments=[],
        out_dir=clip_dir, analysis_keywords=keywords, topic_hint=topic_hint,
        copy_override=override, force=True, template=spec,
    )
    layout_path = clip_dir / COVER_LAYOUT_FILENAME
    cover_copy = result.get("cover_copy") or {}
    return {
        "cover_path": Path(result["cover_path"]),
        "layout_path": Path(result["cover_layout_path"]),
        "layout": json.loads(layout_path.read_text(encoding="utf-8")) if layout_path.is_file() else {},
        "video_brain_task_id": None,
        "frame_ts_in_base": float(result.get("frame_time_rel_s") or 0.0),
        "base_video": str(clip_final),
        "response": {"renderer": "cover_template/local", "template": spec,
                     "cover_revision": result.get("cover_revision"),
                     "copy_source": result.get("copy_source")},
        "cover_copy": {"top_theme": cover_copy.get("topic"),
                       "headline_line_1": cover_copy.get("hook"),
                       "headline_line_2": ""},
        "copy_report": None,
    }


def _clip_topic_hint(task_id: str, clip_id: str, clip_dir: Path) -> tuple[str | None, list[str]]:
    """从 clip_plan.json 取该切片 topic 与分析关键词（读不到就返回空，规则回退兜底）。"""
    keywords: list[str] = []
    topic: str | None = None
    for path in (clip_dir.parent / "clip_plan.json",):
        if not path.is_file():
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if topic is None:
            for item in document.get("clips") or []:
                if isinstance(item, dict) and str(item.get("clip_id")) == clip_id:
                    topic = str(item.get("topic") or "") or None
                    break
        if not keywords and isinstance(document.get("keywords"), list):
            keywords = [str(k) for k in document["keywords"] if str(k).strip()]
        if topic is not None and keywords:
            break
    return topic, keywords


def _read_local_final_copy(clip_dir: Path) -> dict[str, Any] | None:
    """读本通道（或本地管线）已生成的 cover_copy.json 最终文案（只换版面不换文案）。"""
    path = clip_dir / "cover_copy.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    final = payload.get("final") if isinstance(payload.get("final"), dict) else payload
    if isinstance(final, dict) and final.get("topic"):
        return final
    return None


def _override_to_topic_hook(override: dict[str, Any], topic_hint: str | None) -> dict[str, Any]:
    """把 UPDATE_TITLE 的 ``headline_line_1/2`` 覆盖折算成本地管线的 ``topic/hook``。"""
    line_1 = str(override.get("headline_line_1") or "")
    line_2 = str(override.get("headline_line_2") or "")
    hook = f"{line_1}{line_2}".strip() or str(override.get("hook") or "")
    topic = str(override.get("top_theme") or topic_hint or hook[:6] or "")
    return {"topic": topic, "hook": hook, "segment_ids": []}


def _render_cover_via_video_brain(task_id: str, clip_id: str, clip_dir: Path,
                                  style_id: str,
                                  cover_copy_override: dict[str, Any] | None = None) -> dict[str, Any]:
    """按风格渲染封面（渲染缝；历史函数名保留，测试与调用方以本函数为替换点）。

    ``realframe_v2`` → 本地 :func:`_render_cover_realframe`（真实视频帧 + Pillow
    文字叠加，**不再路由 video-brain**）；其余风格（``cutout_v3``）照旧调
    video-brain ``/v1/tools/cover.render``。
    ``cover_copy_override``：本次要上封面的文案（UPDATE_TITLE / 用户改写主题时传入）。
    video-brain 路径下无论文案来自哪里，都先过一遍 :func:`clamp_cover_copy`
    （固化规则：主钩子 ≤12 汉字、最多两行），并把压缩报告随结果带回。
    """
    if style_id == "realframe_v2":
        return _render_cover_realframe(task_id, clip_id, clip_dir, cover_copy_override)
    if isinstance(style_id, str) and style_id.startswith("template:"):
        return _render_cover_template(task_id, clip_id, clip_dir,
                                      style_id.split(":", 1)[1], cover_copy_override)
    vb_task = _video_brain_task_id(task_id, clip_dir)
    vb_clip_dir = VIDEO_BRAIN_HOME / "data" / "tasks" / vb_task / "clips" / clip_id
    resolved_copy, frame_ts = _resolve_cover_copy(vb_task, clip_id, vb_clip_dir)
    if isinstance(cover_copy_override, dict) and cover_copy_override:
        resolved_copy = {**resolved_copy, **cover_copy_override}
    resolved_copy, copy_report = clamp_cover_copy(resolved_copy)
    start = _clip_start_seconds(vb_task, clip_id, clip_dir)
    frame_ts_rel = max(0.0, float(frame_ts) - float(start))
    status, payload = _http_json("POST", "/v1/tools/cover.render", {
        "task_id": vb_task, "clip_id": clip_id, "cover_copy": resolved_copy,
        "frame_ts": frame_ts_rel, "style_id": style_id,
    })
    if status != 200:
        detail = str((payload or {}).get("detail") or payload or "")
        if "E_CUTOUT" in detail:
            raise CoverReplaceError(E_COVER_STYLE_UNAVAILABLE, detail)
        if "E_COVER_STYLE_UNKNOWN" in detail:
            raise CoverReplaceError(E_COVER_STYLE_INVALID, detail)
        raise CoverReplaceError(
            E_COVER_RENDER_FAILED, f"video-brain 封面渲染失败（HTTP {status}）：{detail[:400]}")
    cover_path = Path(str((payload or {}).get("cover_jpg_path") or ""))
    if not cover_path.is_file():
        raise CoverReplaceError(
            E_COVER_RENDER_FAILED, "video-brain 返回的 cover.jpg 不存在，无法替换封面")
    return {
        "cover_path": cover_path,
        "layout_path": Path(str((payload or {}).get("cover_layout_path") or "")),
        "layout": (payload or {}).get("layout") or {},
        "video_brain_task_id": vb_task,
        "frame_ts_in_base": frame_ts_rel,
        "base_video": (payload or {}).get("base_video") or "",
        "response": payload or {},
        "cover_copy": resolved_copy,
        "copy_report": copy_report,
    }


def _resolve_cover_copy(vb_task: str, clip_id: str,
                        vb_clip_dir: Path) -> tuple[dict[str, Any], float]:
    """封面文案与源帧时间：优先复用该切片已生成/已确认的 ``cover_copy.json``。

    只换风格、不重写文案——「按两版封面风格重制封面」的语义就是同一份已确认文案
    换一套版面。兜底走 ``/v1/tools/cover.generate`` 现场生成（任务缺 cover_copy 时）。
    """
    copy_path = vb_clip_dir / "cover_copy.json"
    if copy_path.is_file():
        try:
            payload = json.loads(copy_path.read_text(encoding="utf-8"))
            copy_ = payload.get("cover_copy")
            if isinstance(copy_, dict) and copy_.get("top_theme"):
                return copy_, float(payload.get("frame_ts") or 0.0)
        except (OSError, ValueError):
            pass
    status, payload = _http_json("POST", "/v1/tools/cover.generate", {"task_id": vb_task})
    if status != 200:
        raise CoverReplaceError(
            E_COVER_RENDER_FAILED,
            f"无法取得封面文案（cover_copy.json 缺失且 cover.generate 返回 "
            f"HTTP {status}）：{str((payload or {}).get('detail'))[:300]}")
    covers = (payload or {}).get("covers") or []
    item = next((c for c in covers if str(c.get("clip_id")) == clip_id), None) or (covers[0] if covers else None)
    if not item or not isinstance(item.get("cover_copy"), dict):
        raise CoverReplaceError(E_COVER_RENDER_FAILED, "cover.generate 未返回可用封面文案")
    return item["cover_copy"], float(item.get("frame_ts") or 0.0)


def _clip_start_seconds(vb_task: str, clip_id: str, clip_dir: Path) -> float:
    """切片在源视频里的起点（用于把绝对 frame_ts 折成基片内相对时间）。"""
    candidates = [
        VIDEO_BRAIN_HOME / "data" / "tasks" / vb_task / "clip_plan.json",
        clip_dir.parent / "clip_plan.json",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for item in document.get("clips") or document.get("highlights") or []:
            if isinstance(item, dict) and str(item.get("clip_id")) == clip_id:
                try:
                    return float(item.get("start") or 0.0)
                except (TypeError, ValueError):
                    return 0.0
    return 0.0


# ---------------------------------------------------------------------------
# 备份 / 台账 / 回滚
# ---------------------------------------------------------------------------

def _backup_dir(clip_dir: Path) -> Path:
    return clip_dir / COVER_BACKUP_DIRNAME


def _state_path(clip_dir: Path) -> Path:
    return clip_dir / COVER_STATE_FILENAME


def _load_state(clip_dir: Path) -> dict[str, Any]:
    path = _state_path(clip_dir)
    if not path.is_file():
        return {"task_id": clip_dir.parent.name, "clip_id": clip_dir.name,
                "baseline": None, "records": [], "active": None}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    state.setdefault("task_id", clip_dir.parent.name)
    state.setdefault("clip_id", clip_dir.name)
    state.setdefault("baseline", None)
    state.setdefault("records", [])
    state.setdefault("active", None)
    return state


def _ensure_baseline(clip_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
    """首次改动前建立基线备份；已存在则原样复用（不叠加备份）。

    2026-10-21 修（首次制作封面）：成片**从来没有封面**时（``cover.jpg`` 不存在，
    例如自动剪辑只出了片、用户还没做过封面）也允许建立基线——基线里
    ``cover_jpg=None`` 表示「原始状态就是无封面」，回滚语义 = 删掉本通道生成的封面。
    此前这里硬性要求 cover.jpg 存在，导致「选择版型 → 制作封面」直接 400
    ``E_COVER_SOURCE_MISSING``，首次封面永远做不出来。视频 ``clip_final.mp4`` 仍是硬前提。
    """
    baseline = state.get("baseline")
    if isinstance(baseline, dict):
        backup_root = _backup_dir(clip_dir)
        cover_name = str(baseline.get("cover_jpg") or "")
        cover_backup = (backup_root / cover_name) if cover_name else None
        clip_backup = backup_root / str(baseline.get("clip_final") or "")
        if clip_backup.is_file() and (cover_backup is None or cover_backup.is_file()):
            return baseline

    cover = clip_dir / COVER_FILENAME
    video = clip_dir / CLIP_FILENAME
    if not video.is_file():
        raise CoverReplaceError(
            E_COVER_SOURCE_MISSING, f"成片缺少 {CLIP_FILENAME}，无法建立备份基线")
    stamp = _stamp()
    backup_root = _backup_dir(clip_dir)
    backup_root.mkdir(parents=True, exist_ok=True)
    clip_backup = f"{CLIP_FILENAME}.{stamp}.bak"
    shutil.copy2(video, backup_root / clip_backup)
    # 原始封面可能在（正常情况）也可能不在（首次制作封面）：不在就把基线记成 None，
    # 不伪造一张封面，也不因此拒绝生成第一版封面。
    cover_backup: str | None = None
    cover_sha: str | None = None
    cover_bytes: int | None = None
    if cover.is_file():
        cover_backup = f"{COVER_FILENAME}.{stamp}.bak"
        shutil.copy2(cover, backup_root / cover_backup)
        cover_sha = _sha256(cover)
        cover_bytes = cover.stat().st_size
    # cover_layout.json 是本通道新增的产物（原成片目录里通常没有）：有就一起备份，
    # 没有就把基线记成 None —— 回滚时把它删掉，避免留下「layout 说自己是大字风格、
    # cover.jpg 却已经回到旧封面」的自相矛盾状态。
    layout = clip_dir / COVER_LAYOUT_FILENAME
    layout_backup: str | None = None
    if layout.is_file():
        layout_backup = f"{COVER_LAYOUT_FILENAME}.{stamp}.bak"
        shutil.copy2(layout, backup_root / layout_backup)
    probe = probe_streams(video)
    baseline = {
        "created_at": _now_iso(),
        "cover_jpg": cover_backup,
        "clip_final": clip_backup,
        "cover_layout": layout_backup,
        "sha256": {"cover_jpg": cover_sha, "clip_final": _sha256(video)},
        "frames": probe["frames"],
        "duration_s": round(probe["duration_s"], 6),
        "fps": probe["fps"],
        "has_audio": probe["has_audio"],
        "bytes": {"cover_jpg": cover_bytes, "clip_final": video.stat().st_size},
    }
    state["baseline"] = baseline
    _atomic_json(_state_path(clip_dir), state)
    return baseline


def _find_record(state: dict[str, Any], key: str) -> dict[str, Any] | None:
    for record in reversed(state.get("records") or []):
        if isinstance(record, dict) and record.get("key") == key:
            return record
    return None


def _current_hashes(clip_dir: Path) -> dict[str, str]:
    return {
        "cover_jpg": _sha256(clip_dir / COVER_FILENAME),
        "clip_final": _sha256(clip_dir / CLIP_FILENAME),
    }


def _is_noop(state: dict[str, Any], key: str, replace_intro: bool,
             clip_dir: Path) -> dict[str, Any] | None:
    """幂等判定：同 key 且产物哈希与上次一致 → 直接返回上次结果，不叠加备份。"""
    record = _find_record(state, key)
    if not record or bool(record.get("replace_intro")) != bool(replace_intro):
        return None
    expected = record.get("result_sha256") or {}
    try:
        current = _current_hashes(clip_dir)
    except OSError:
        return None
    for name, digest in expected.items():
        if current.get(name) != digest:
            return None
    return record


def _restore_files(clip_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
    """把基线文件写回成片目录（原子替换），返回校验结果。

    基线 ``cover_jpg=None`` 表示「原始状态就是无封面」（首次制作封面场景）：
    回滚 = 删掉本通道生成的 cover.jpg / cover_layout.json，而不是伪造一张旧封面。
    """
    baseline = state.get("baseline") or {}
    backup_root = _backup_dir(clip_dir)
    cover_name = str(baseline.get("cover_jpg") or "")
    clip_name = str(baseline.get("clip_final") or "")
    cover_backup = (backup_root / cover_name) if cover_name else None
    clip_backup = (backup_root / clip_name) if clip_name else None
    if clip_backup is None or not clip_backup.is_file():
        raise CoverReplaceError(
            E_COVER_BACKUP_MISSING,
            f"备份文件缺失（{cover_name or 'cover.jpg(基线无封面)'} / {clip_name or 'clip_final.mp4'}），无法回滚")
    if cover_backup is not None and not cover_backup.is_file():
        raise CoverReplaceError(
            E_COVER_BACKUP_MISSING,
            f"备份文件缺失（{cover_name}），无法回滚")
    cover_target = clip_dir / COVER_FILENAME
    if cover_backup is not None:
        _atomic_copy(cover_backup, cover_target)
    else:
        cover_target.unlink(missing_ok=True)
    _atomic_copy(clip_backup, clip_dir / CLIP_FILENAME)
    # cover_layout.json 是派生记录：基线里没有它（原成片目录本就没有）就删掉本通道新增的，
    # 有就还原回基线版本，保证封面图与 layout 记录始终描述同一版封面。
    layout = clip_dir / COVER_LAYOUT_FILENAME
    if "cover_layout" in baseline:
        layout_backup = baseline.get("cover_layout")
        if layout_backup:
            candidate = backup_root / str(layout_backup)
            if candidate.is_file():
                _atomic_copy(candidate, layout)
        else:
            layout.unlink(missing_ok=True)
    elif cover_backup is None:
        layout.unlink(missing_ok=True)
    verified = {}
    for name, path in (("cover_jpg", cover_target),
                       ("clip_final", clip_dir / CLIP_FILENAME)):
        expected = (baseline.get("sha256") or {}).get(name)
        if expected:
            verified[name] = path.is_file() and _sha256(path) == expected
        else:
            # 基线就没有封面：回滚后必须确实没有封面才算一致（而不是「跳过校验」）
            verified[name] = not path.is_file()
    return {"restored": all(verified.values()), "sha256_match": verified,
            "cover_restored": cover_backup is not None,
            "frames": baseline.get("frames"), "duration_s": baseline.get("duration_s")}


# ---------------------------------------------------------------------------
# 结果组装
# ---------------------------------------------------------------------------

def _clip_artifact_paths(task_id: str, clip_dir: Path) -> dict[str, str]:
    """成片目录里两个产物的任务相对路径（``resolve_auto_edit_artifact`` 可直接吃）。"""
    prefix = f"output/{task_id}/{clip_dir.name}"
    return {"cover_path": f"{prefix}/{COVER_FILENAME}",
            "video_path": f"{prefix}/{CLIP_FILENAME}"}


def _result(task_id: str, clip_id: str, clip_dir: Path, *, kind: str,
            style_id: str | None, replace_intro: bool, qc: dict[str, Any] | None,
            idempotent: bool, source: dict[str, Any] | None = None,
            extra: dict[str, Any] | None = None) -> dict[str, Any]:
    paths = _clip_artifact_paths(task_id, clip_dir)
    payload: dict[str, Any] = {
        "task_id": task_id,
        "clip_id": clip_id,
        "kind": kind,                       # generated | adopted | restore
        "style_id": style_id,
        "replace_intro": bool(replace_intro),
        "idempotent": bool(idempotent),
        "cover_path": paths["cover_path"],
        "video_path": paths["video_path"],
        "cover_abs_path": str(clip_dir / COVER_FILENAME),
        "video_abs_path": str(clip_dir / CLIP_FILENAME),
        "cover_url": f"/studio/auto-edit/{task_id}/artifact"
                     f"?path={paths['cover_path']}&v={_file_version(clip_dir / COVER_FILENAME)}",
        "backup_dir": str(_backup_dir(clip_dir)),
        "state_path": str(_state_path(clip_dir)),
        "qc": qc,
        "source": source,
    }
    if extra:
        payload.update(extra)
    return payload


def _file_version(path: Path) -> str:
    """缓存击穿用版本号（mtime+size）：封面文件路径不变，前端必须靠它刷新。"""
    try:
        stat = path.stat()
        return f"{int(stat.st_mtime)}-{stat.st_size}"
    except OSError:
        return "0"


# ---------------------------------------------------------------------------
# 片头替换
# ---------------------------------------------------------------------------

def splice_intro(clip_path: Path, cover_jpg: Path, *, intro_duration_s: float,
                 fade_in_s: float, crf: int = 20, preset: str = "veryfast",
                 workdir: Path | None = None) -> dict[str, Any]:
    """把成片首 N 秒替换为「新封面静帧等长片段」，其余帧保持逐位一致。

    实现：``head``（静帧片头）+ ``bridge``（仅当切点不在关键帧上）+ ``tail``
    （关键帧起 ``-c copy``）→ concat demuxer ``-c copy``。输出先写临时文件，
    QC 通过后才 ``os.replace`` 覆盖原成片（失败时原文件毫发无损）。
    """
    ffmpeg = resolve_ffmpeg()
    before = probe_streams(clip_path)
    target_frames = max(1, int(round(float(intro_duration_s) * before["fps"])))
    plan = plan_intro_split(before["fps"], before["frames"],
                            keyframe_list(clip_path), target_frames)
    owns_temp = workdir is None
    temp_root = workdir or (clip_path.parent / f".cover_tmp_{uuid.uuid4().hex[:8]}")
    temp_root.mkdir(parents=True, exist_ok=True)
    head_path = temp_root / "head.mp4"
    bridge_path = temp_root / "bridge.mp4"
    tail_path = temp_root / "tail.mp4"
    concat_list = temp_root / "concat.txt"
    merged = temp_root / "merged.mp4"
    try:
        steps = [("head", build_head_command(ffmpeg, cover_jpg, clip_path, head_path,
                                             before, plan, crf=crf, preset=preset,
                                             fade_in_s=fade_in_s))]
        segments = [head_path]
        if plan["bridge_frames"] > 0:
            steps.append(("bridge", build_bridge_command(ffmpeg, clip_path, bridge_path,
                                                         before, plan, crf=crf,
                                                         preset=preset)))
            segments.append(bridge_path)
        steps.append(("tail", build_tail_command(ffmpeg, clip_path, tail_path,
                                                 before, plan)))
        segments.append(tail_path)
        for name, command in steps:
            completed = _run(command, FFMPEG_TIMEOUT_SECONDS)
            if completed.returncode != 0:
                raise CoverReplaceError(
                    E_COVER_RENDER_FAILED,
                    f"片头替换（{name} 段）失败: {(completed.stderr or '')[-400:]}")
        concat_list.write_text(
            "".join(f"file '{segment.as_posix()}'\n" for segment in segments),
            encoding="utf-8")
        completed = _run(build_concat_command(ffmpeg, concat_list, merged),
                         FFMPEG_TIMEOUT_SECONDS)
        if completed.returncode != 0:
            raise CoverReplaceError(
                E_COVER_RENDER_FAILED, f"片头 concat 失败: {(completed.stderr or '')[-400:]}")

        after = probe_streams(merged)
        qc = intro_qc(clip_path, merged, cover_jpg, before, after, plan, temp_root)
        if not qc["passed"]:
            raise CoverReplaceError(E_COVER_QC_FAILED, "；".join(qc["failures"]))

        os.replace(merged, clip_path)
        qc["mode"] = plan["mode"]
        qc["bridge_frames"] = plan["bridge_frames"]
        qc["target_frames"] = plan["target_frames"]
        qc["split_frame"] = plan["split_frame"]
        qc["split_time_s"] = plan["split_time_s"]
        qc["head_duration_s"] = plan["head_duration_s"]
        qc["intro_duration_s"] = intro_duration_s
        qc["fade_in_s"] = fade_in_s
        qc["preset"] = preset
        qc["crf"] = crf
        qc["commands"] = {"head": steps[0][1], "concat": build_concat_command(
            ffmpeg, concat_list, merged)}
        if plan["bridge_frames"] > 0:
            qc["commands"]["bridge"] = steps[1][1]
        qc["commands"]["tail"] = steps[-1][1]
        qc["backup_dir"] = str(clip_path.parent / COVER_BACKUP_DIRNAME)
        return qc
    finally:
        if owns_temp:
            shutil.rmtree(temp_root, ignore_errors=True)


def intro_qc(original: Path, merged: Path, cover_jpg: Path, before: dict[str, Any],
             after: dict[str, Any], plan: dict[str, Any],
             workdir: Path) -> dict[str, Any]:
    """替换后质检：时长/帧数/音轨/首帧/首个 I 帧/尾部逐位一致。

    全部断言都写成可读的 ``checks`` + ``failures``，任何 FAIL 都会让调用方自动回滚。
    """
    fps = before["fps"]
    tolerance = max(1, int(round(fps * 0.04)))       # ±1 帧
    checks: list[dict[str, Any]] = []
    failures: list[str] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append({"check": name, "passed": bool(passed), "detail": detail})
        if not passed:
            failures.append(f"{name}: {detail}")

    frame_delta = abs(after["frames"] - before["frames"])
    check("total_frames_unchanged", frame_delta <= tolerance,
          f"{after['frames']} vs {before['frames']}（差 {frame_delta} 帧，容差 ±{tolerance}）")
    duration_delta = abs(after["duration_s"] - before["duration_s"])
    check("duration_unchanged", duration_delta <= tolerance / fps + 1e-3,
          f"{after['duration_s']:.6f}s vs {before['duration_s']:.6f}s"
          f"（差 {duration_delta:.6f}s，容差 {tolerance / fps:.3f}s）")
    check("resolution_unchanged",
          (after["width"], after["height"]) == (before["width"], before["height"]),
          f"{after['width']}x{after['height']} vs {before['width']}x{before['height']}")
    check("pix_fmt_unchanged", after["pix_fmt"] == before["pix_fmt"],
          f"{after['pix_fmt']} vs {before['pix_fmt']}")
    check("fps_unchanged", abs(after["fps"] - fps) < 1e-6,
          f"{after['fps']} vs {fps}")

    if before["has_audio"]:
        check("audio_stream_preserved", after["has_audio"] and after["acodec"] == before["acodec"],
              f"codec={after['acodec']} vs {before['acodec']}")
        check("audio_params_preserved",
              (after["sample_rate"], after["channels"]) == (before["sample_rate"], before["channels"]),
              f"{after['sample_rate']}Hz/{after['channels']}ch vs "
              f"{before['sample_rate']}Hz/{before['channels']}ch")
    else:
        check("audio_absent_kept", True, "原片无音轨，替换后同样不引入音轨")

    keyframes = keyframe_list(merged)
    first_iframe = keyframes[0]["frame"] if keyframes else None
    check("first_frame_is_iframe", first_iframe == 0,
          f"首个 I 帧序号={first_iframe if first_iframe is not None else 'None'}（应为 0）")

    # 封面文字窗不得越出片头：封面文字烧在 cover.jpg 上，只存在 head 段内，
    # 而 head 段时长 = 风格 intro.duration_s（文字窗 ≤ intro 的结构性保证）。
    seconds = min(max(plan["head_duration_s"] / 2, 0.05), 0.2)
    similarity = frame_similarity_to_image(merged, seconds, cover_jpg, workdir)
    check("intro_frame_matches_new_cover", similarity >= 0.95,
          f"{seconds:.2f}s 处帧与新封面 SSIM {similarity}（阈值 0.95）")
    check("cover_text_window_within_intro", plan["head_duration_s"] <= plan["target_frames"] / fps + 1e-6,
          f"封面文字窗 {plan['head_duration_s']:.3f}s ≤ 片头 "
          f"{plan['target_frames'] / fps:.3f}s")

    if plan["bridge_frames"] == 0 and plan["tail_copy"]:
        original_tail = frame_md5_list(original, start_time_s=plan["split_time_s"],
                                       limit=QC_MD5_MAX_FRAMES)
        merged_tail = frame_md5_list(merged, start_time_s=plan["split_time_s"],
                                     limit=QC_MD5_MAX_FRAMES)
        mismatches = sum(1 for a, b in zip(original_tail, merged_tail) if a != b)
        mismatches += abs(len(original_tail) - len(merged_tail))
        check("tail_frames_bit_identical", mismatches == 0 and bool(original_tail),
              f"第 {plan['split_frame']} 帧（{plan['split_time_s']:.3f}s）起逐帧 MD5 比对："
              f"抽验 {len(original_tail)} 帧，不一致 {mismatches} 帧"
              f"（尾部为流拷贝，其余帧逐位一致由构造保证）")
    else:
        check("tail_frames_bit_identical", False,
              f"片头边界不在关键帧上（bridge_frames={plan['bridge_frames']}，"
              f"tail_copy={plan['tail_copy']}），尾部无法全程逐位一致")

    return {
        "passed": not failures,
        "failures": failures,
        "checks": checks,
        "before": {"frames": before["frames"], "duration_s": before["duration_s"],
                   "has_audio": before["has_audio"], "fps": before["fps"]},
        "after": {"frames": after["frames"], "duration_s": after["duration_s"],
                  "has_audio": after["has_audio"], "fps": after["fps"]},
        "cover_similarity": similarity,
        "first_iframe": first_iframe,
    }


# ---------------------------------------------------------------------------
# 对外主流程
# ---------------------------------------------------------------------------

def _clip_dir_or_raise(task_id: str, clip_id: str) -> Path:
    try:
        clip_dir = auto_edit_clip_dir(task_id, clip_id)
    except ValueError as exc:
        raise CoverReplaceError(E_COVER_BAD_REQUEST, str(exc)) from exc
    if not (clip_dir / CLIP_FILENAME).is_file():
        raise CoverReplaceError(
            E_COVER_CLIP_MISSING, f"成片不存在：{task_id}/{clip_id}/{CLIP_FILENAME}")
    return clip_dir



# ---------------------------------------------------------------------------
# 封面版型工作流：草稿（未应用）→ 应用（烧录片头+替换成片）/ 删除（仅草稿）
# ---------------------------------------------------------------------------

def _template_pending(clip_dir: Path) -> dict[str, Any] | None:
    """读取草稿状态（state.template_pending）：{revision, style, applied} 或 None。"""
    state = _load_state(clip_dir)
    pending = state.get("template_pending")
    return pending if isinstance(pending, dict) and pending.get("revision") else None


def mark_template_pending(task_id: str, clip_id: str, revision: int, style_id: str | None) -> dict[str, Any]:
    """Make 成功后把当前 revision 标记为「待应用草稿」（replace_intro=False，成片未动）。"""
    clip_dir = _clip_dir_or_raise(task_id, clip_id)
    state = _load_state(clip_dir)
    state["template_pending"] = {"revision": int(revision), "style": str(style_id or ""),
                                 "applied": False, "updated_at": _now_iso()}
    state["updated_at"] = _now_iso()
    _atomic_json(_state_path(clip_dir), state)
    return dict(state["template_pending"])


def apply_template_cover(task_id: str, clip_id: str, *,
                         previous_intro_s: float = 0.0) -> dict[str, Any]:
    """应用草稿：把版型封面作为片头**加到正片前面**并原子替换当前成片；失败保留原成片。

    - 只允许对「未应用的版型草稿」执行（无草稿/已应用 → E_COVER_BAD_REQUEST）；
    - 渲染 = :func:`cover_realframe_v2.splice_intro_prepend`：封面静帧 ``intro`` 秒 +
      正片全部内容；音频用 adelay 整体后移 → **片头静音、正片音频从片头之后开始**，
      已烧录字幕随正片后移（不再出现「封面窗内播原声」的覆盖问题）；
      ``previous_intro_s`` 指定旧片头长度：重复应用时先裁掉旧片头再加新封面，不会叠加；
    - QC（时长/分辨率/帧率/音轨/片头静音/封面相似度）通过才 ``os.replace``；
      失败时 state 不变、成片保持原样（不落半程产物）；
    - 成功后草稿转正（applied=True），封面版本快照元数据同步。
    """
    clip_dir = _clip_dir_or_raise(task_id, clip_id)
    pending = _template_pending(clip_dir)
    if not pending:
        raise CoverReplaceError(E_COVER_BAD_REQUEST, "当前切片没有待应用的版型封面草稿")
    if pending.get("applied"):
        raise CoverReplaceError(E_COVER_BAD_REQUEST, "该版型封面已经应用过，请先生成新的封面")
    state = _load_state(clip_dir)
    try:
        from . import cover_realframe_v2
        qc = cover_realframe_v2.splice_intro_prepend(
            clip_dir / CLIP_FILENAME, clip_dir / COVER_FILENAME,
            intro_duration_s=style_intro_duration("realframe_v2"),
            previous_intro_s=float(previous_intro_s or 0.0),
            fade_in_s=style_fade_in("realframe_v2"), fade_out_s=0.18)
    except Exception as exc:
        raise CoverReplaceError(
            E_COVER_RENDER_FAILED,
            f"片头渲染失败，已保留原成片：{str(exc)[:200]}") from exc
    pending["applied"] = True
    pending["updated_at"] = _now_iso()
    state["template_pending"] = pending
    state["records"] = (state.get("records") or [])[-19:] + [{
        "key": f"template-apply:v{pending.get('revision')}", "kind": "template_apply",
        "applied_at": _now_iso(), "revision": pending.get("revision"),
        "style": pending.get("style"), "qc": qc,
    }]
    state["updated_at"] = _now_iso()
    _atomic_json(_state_path(clip_dir), state)
    return {"task_id": task_id, "clip_id": clip_id, "applied": True,
            "style": pending.get("style"), "revision": pending.get("revision"), "qc": qc}


def delete_template_cover(task_id: str, clip_id: str) -> dict[str, Any]:
    """删除未应用的版型封面草稿：只清这份封面素材文件，切片/成片/其它数据不动。

    - 归属与引用检查：state 里必须有匹配的未应用草稿记录（revision 存在于本
      clip_dir 的快照目录内）；已应用的草稿不可删（先制作新的）；
    - 删除 = 恢复上一版封面（install_revision，把 cover.jpg/layout 原子换回）+
      删除该草稿的 rev-NNN 快照目录；成片文件不动（草稿从未烧录）。
    """
    clip_dir = _clip_dir_or_raise(task_id, clip_id)
    pending = _template_pending(clip_dir)
    if not pending:
        raise CoverReplaceError(E_COVER_BAD_REQUEST, "当前切片没有可删除的版型封面草稿")
    if pending.get("applied"):
        raise CoverReplaceError(E_COVER_BAD_REQUEST, "该封面已应用到成片，不能删除；请先生成新封面替换（或撤销封面）后再删除")
    revision = int(pending.get("revision") or 0)
    target_dir = revision_dir(clip_dir, revision)
    # 归属检查：快照目录必须落在本 clip 目录之内（防路径逃逸）
    try:
        target_dir.resolve().relative_to(clip_dir.resolve())
    except ValueError as exc:
        raise CoverReplaceError(E_COVER_BAD_REQUEST, "封面草稿目录不属于当前成片") from exc
    state = _load_state(clip_dir)
    restored = None
    # 恢复上一版：从草稿版本往下找最近的有快照的 revision（install_revision 原子换回）；
    # 一个都没有 → 回到基线（restore_cover：原始自动剪辑封面与成片）。
    for n in range(revision - 1, 1, -1):
        if (revision_dir(clip_dir, n) / COVER_REVISION_META).is_file():
            verification = install_revision(clip_dir, n)
            restored = {"revision": n, "verification": verification}
            break
    if restored is None:
        restore_cover(task_id, clip_id)
        restored = {"revision": 1, "restored_baseline": True}
    import shutil
    shutil.rmtree(target_dir, ignore_errors=True)
    state["records"] = [record for record in (state.get("records") or [])
                        if int(record.get("revision") or 0) != revision]
    if int(state.get("active_revision") or 0) == revision:
        state["active_revision"] = None
    state.pop("template_pending", None)
    state["updated_at"] = _now_iso()
    _atomic_json(_state_path(clip_dir), state)
    return {"task_id": task_id, "clip_id": clip_id, "deleted_revision": revision,
            "restored_previous": restored, "current_revision": current_revision(clip_dir)}


def cover_template_state_for_card(clip_dir: Path) -> dict[str, Any]:
    """卡片载荷用的版型封面状态（刷新恢复的事实源）：封面在位 + 待应用标记。"""
    state = _load_state(clip_dir)
    pending = state.get("template_pending") if isinstance(state.get("template_pending"), dict) else None
    cover = clip_dir / COVER_FILENAME
    return {
        "has_cover": cover.is_file(),
        "pending": bool(pending and not pending.get("applied")),
        "applied": bool(pending and pending.get("applied")),
        "revision": int(state.get("active_revision") or current_revision(clip_dir) or 0) or None,
        "style": (pending or {}).get("style"),
    }


def template_pending_state(task_id: str, clip_id: str) -> dict[str, Any] | None:
    """读当前切片的版型封面草稿状态（应用任务的前置校验用）。

    返回 ``{"revision": N, "style": "template:xxx", "applied": bool, ...}``；
    没有草稿返回 ``None``。封面草稿是唯一事实源，绝不按 ledger 猜。
    """
    clip_dir = _clip_dir_or_raise(task_id, clip_id)
    return _template_pending(clip_dir)


def apply_cover_duration_s() -> float:
    """应用封面时的片头时长（沿用既有片头规则：realframe_v2 的 intro.duration_s，缺省 0.8s）。"""
    return float(style_intro_duration("realframe_v2"))


def replace_cover(task_id: str, clip_id: str, style_id: str | None,
                  *, replace_intro: bool = True,
                  cover_copy_override: dict[str, Any] | None = None,
                  template_id: str | None = None) -> dict[str, Any]:
    """按封面风格重制该成片的封面（图 + 0.8s 片头），可回滚。**不新建 clip、不重剪正片。**

    ``style_id``：``realframe_v2``（缺省）/ ``cutout_v3``；v3 权重缺失时显式失败
    （``E_COVER_STYLE_UNAVAILABLE`` + 安装指引），不静默降级。
    ``cover_copy_override``：本次要上封面的文案（UPDATE_TITLE / 用户改写主题时传入），
    一律经 :func:`clamp_cover_copy` 压到「主钩子 ≤12 汉字、最多两行」再出图。
    ``replace_intro=False``：只换封面图（片头不烧录封面的场景），成片画面逐帧不动。
    """
    if template_id is not None:
        if style_id and str(style_id).strip():
            raise CoverReplaceError(
                E_COVER_BAD_REQUEST, "template_id 与 style_id 只能二选一")
        # 封面版型（2026-10-21 阶段）：版型即风格——统一映射成 template:{id} 风格号。
        # 该风格号已过注册表校验（未知版型在此 404 级报错），绕过风格白名单；
        # 备份/回滚/片头/revision 快照链路与既有风格完全同源。
        from . import cover_templates
        style_id = f"template:{cover_templates.validate_template_id(template_id)}"
    explicit_style = bool(style_id and str(style_id).strip())
    resolved_style = (str(style_id).strip()
                      if str(style_id or "").startswith("template:")
                      else validate_style_id(style_id))
    style_source = ("user_specified" if explicit_style
                    else regenerate_default_style()[1])
    clip_dir = _clip_dir_or_raise(task_id, clip_id)
    state = _load_state(clip_dir)
    key = f"generated:{resolved_style}"
    # 文案被显式改写时不能走「同风格即 noop」的幂等短路——否则改了标题却没有任何变化。
    if not cover_copy_override:
        cached = _is_noop(state, key, replace_intro, clip_dir)
        if cached:
            return _result(task_id, clip_id, clip_dir, kind="generated",
                           style_id=resolved_style, replace_intro=replace_intro,
                           qc=cached.get("qc"), idempotent=True,
                           source={"type": "generated"},
                           extra={"applied_at": cached.get("applied_at"),
                                  "cover_revision": current_revision(clip_dir),
                                  "note": "同风格封面已生效且产物未变，未重复生成/备份"})

    baseline = _ensure_baseline(clip_dir, state)
    rendered = _render_cover_via_video_brain(task_id, clip_id, clip_dir, resolved_style,
                                             cover_copy_override)
    _atomic_copy(rendered["cover_path"], clip_dir / COVER_FILENAME)
    if rendered["layout_path"].is_file():
        _atomic_copy(rendered["layout_path"], clip_dir / COVER_LAYOUT_FILENAME)

    # 版型草稿（template:*，2026-10-21 第二版）：只生成封面素材，**不烧片头不动成片**；
    # 应用（烧片头+替换成片）走 apply_template_cover。
    if resolved_style.startswith("template:"):
        replace_intro = False

    try:
        qc = None if resolved_style.startswith("template:") else _apply_intro_if_needed(clip_dir, resolved_style, replace_intro)
    except Exception:
        _restore_files(clip_dir, state)                  # 失败自动回滚
        raise

    record = {
        "key": key, "kind": "generated", "style_id": resolved_style,
        # 风格来源可追溯：regenerate_default / user_specified（配置化缺省）
        "style_id_source": style_source,
        "applied_at": _now_iso(), "replace_intro": bool(replace_intro),
        "source": {"type": "generated", "video_brain_task_id": rendered["video_brain_task_id"],
                   "frame_ts_in_base": rendered["frame_ts_in_base"],
                   "base_video": rendered["base_video"]},
        "cover_copy": rendered.get("cover_copy"),
        "copy_report": rendered.get("copy_report"),
        "qc": qc, "result_sha256": _current_hashes(clip_dir),
        "baseline_sha256": (baseline or {}).get("sha256"),
    }
    _upsert_record(state, record, key)
    _atomic_json(_state_path(clip_dir), state)
    revision = _snapshot_new_revision(clip_dir, record, replace_intro=replace_intro,
                                      kind="generated", style_id=resolved_style)
    return _result(task_id, clip_id, clip_dir, kind="generated", style_id=resolved_style,
                   replace_intro=replace_intro, qc=qc, idempotent=False,
                   source=record["source"],
                   extra={"applied_at": record["applied_at"],
                          "cover_revision": revision,
                          # 本次风格与实际来源（regenerate_default / user_specified）
                          "style_id_source": style_source,
                          "cover_copy": rendered.get("cover_copy"),
                          "copy_report": rendered.get("copy_report")})


def _snapshot_new_revision(clip_dir: Path, record: dict[str, Any], *, replace_intro: bool,
                           kind: str, style_id: str | None,
                           instruction: str | None = None) -> int:
    """成功变更后落一份 rev-NNN 快照，返回这一版的版本号（v1 是原封面）。"""
    revision = max(1, current_revision(clip_dir)) + 1
    cover = clip_dir / COVER_FILENAME
    snapshot_revision(clip_dir, revision, {
        "revision": revision,
        "kind": kind,
        "style_id": style_id,
        "replace_intro": bool(replace_intro),
        "instruction": instruction,
        "cover_sha256": _sha256(cover) if cover.is_file() else None,
        "clip_final_sha256": None,
        "source": record.get("source"),
        "cover_copy": record.get("cover_copy"),
    })
    return revision


def undo_cover_revision(task_id: str, clip_id: str, revision: int = 1) -> dict[str, Any]:
    """撤销封面到历史里的第 ``revision`` 版（默认 v1 = 原封面），原地生效、可再来一次。"""
    clip_dir = _clip_dir_or_raise(task_id, clip_id)
    target = int(revision)
    if target < 1:
        raise CoverReplaceError(E_COVER_BAD_REQUEST, f"非法的封面版本号 v{revision}")
    current = current_revision(clip_dir)
    if target >= current and current > 0:
        raise CoverReplaceError(
            E_COVER_BAD_REQUEST,
            f"v{target} 不是历史版本（当前已是 v{current}）；只能撤销到更早的版本")
    verification = install_revision(clip_dir, target)
    state = _load_state(clip_dir)
    state["active"] = None
    state["active_revision"] = target
    state["updated_at"] = _now_iso()
    state["records"] = (state.get("records") or [])[-19:] + [{
        "key": f"undo:v{target}", "kind": "undo", "applied_at": _now_iso(),
        "revision": target, "verification": verification,
    }]
    _atomic_json(_state_path(clip_dir), state)
    return _result(task_id, clip_id, clip_dir, kind="undo", style_id=None,
                   replace_intro=False,
                   qc={"undo_verification": verification}, idempotent=False, source=None,
                   extra={"undo_revision": target, "cover_revision": target,
                          "restored_video": verification.get("restored_video", False)})


def activate_cover_revision(task_id: str, clip_id: str, revision: int) -> dict[str, Any]:
    """把历史里任意一版**设为当前封面**（封面版本管理页的「设为当前封面」）。

    与 :func:`undo_cover_revision` 的差别：不限于「往回撤」——v2↔v3 互切都允许，
    唯一禁止的是切到「已是当前」的版本（那是空操作）。物理生效走同一个
    :func:`install_revision`（封面图 + 片头画面 + layout 原子替换、逐字节校验）。
    """
    clip_dir = _clip_dir_or_raise(task_id, clip_id)
    target = int(revision)
    if target < 1:
        raise CoverReplaceError(E_COVER_BAD_REQUEST, f"非法的封面版本号 v{target}")
    state = _load_state(clip_dir)
    # 「当前使用中」看 active_revision（撤销/激活会偏离最新版本号），不是最大快照号
    active_now = int(state.get("active_revision") or current_revision(clip_dir) or 0)
    if active_now and target == active_now:
        raise CoverReplaceError(E_COVER_BAD_REQUEST, f"v{target} 已是当前使用中的封面")
    verification = install_revision(clip_dir, target)
    state["active"] = None
    state["active_revision"] = target
    state["updated_at"] = _now_iso()
    state["records"] = (state.get("records") or [])[-19:] + [{
        "key": f"activate:v{target}", "kind": "activate", "applied_at": _now_iso(),
        "revision": target, "verification": verification,
    }]
    _atomic_json(_state_path(clip_dir), state)
    return _result(task_id, clip_id, clip_dir, kind="activate", style_id=None,
                   replace_intro=False,
                   qc={"activate_verification": verification}, idempotent=False, source=None,
                   extra={"activated_revision": target, "cover_revision": target,
                          "restored_video": verification.get("restored_video", False)})


def delete_cover_revision(task_id: str, clip_id: str, revision: int) -> dict[str, Any]:
    """删除**未被当前使用**的历史封面版本（封面版本管理页的「删除版本」）。

    - v1（自动剪辑首版基线）不删：它是物理回滚锚点（备份目录里），删了无法回原点；
    - 当前使用中的版本不删（按 state.active_revision 判定，撤销后可能与最新版不同）；
    - 只删 rev-NNN 快照目录 + 状态记录；台账历史由调用方（studio_local_tasks）同步。
    """
    clip_dir = _clip_dir_or_raise(task_id, clip_id)
    target = int(revision)
    if target < 2:
        raise CoverReplaceError(
            E_COVER_BAD_REQUEST, "v1 是自动剪辑的首版基线，保留为回滚锚点，不可删除")
    state = _load_state(clip_dir)
    target_dir = revision_dir(clip_dir, target)
    if not (target_dir / COVER_REVISION_META).is_file():
        raise CoverReplaceError(
            E_COVER_BACKUP_MISSING, f"v{target} 没有可删除的快照")
    active_now = int(state.get("active_revision") or current_revision(clip_dir) or 0)
    if active_now and target == active_now:
        raise CoverReplaceError(
            E_COVER_BAD_REQUEST, f"v{target} 是当前使用中的封面，不能删除；先切换到其它版本")
    import shutil
    shutil.rmtree(target_dir)
    state["records"] = [record for record in (state.get("records") or [])
                        if int(record.get("revision") or 0) != target]
    if int(state.get("active_revision") or 0) == target:
        state["active_revision"] = None
    state["updated_at"] = _now_iso()
    _atomic_json(_state_path(clip_dir), state)
    return {"task_id": task_id, "clip_id": clip_id, "deleted_revision": target,
            "current_revision": current_revision(clip_dir)}


def adopt_cover(task_id: str, clip_id: str, source_task_id: str, source_clip_id: str,
                *, replace_intro: bool = False) -> dict[str, Any]:
    """把某条成片**已生成**的 ``cover.jpg`` 沿用为目标成片的封面（不调用生成链路）。

    ``replace_intro`` 默认 False：只换封面图（资产卡/导出封面立即变），不动片头——
    沿用场景下强行重做片头没有收益，默认保持成片画面不变。
    """
    clip_dir = _clip_dir_or_raise(task_id, clip_id)
    source_dir = _clip_dir_or_raise(source_task_id, source_clip_id)
    source_cover = source_dir / COVER_FILENAME
    if not source_cover.is_file():
        raise CoverReplaceError(
            E_COVER_SOURCE_MISSING,
            f"来源成片 {source_task_id}/{source_clip_id} 没有已生成的 {COVER_FILENAME}")
    key = f"adopted:{source_task_id}/{source_clip_id}"
    state = _load_state(clip_dir)
    cached = _is_noop(state, key, replace_intro, clip_dir)
    if cached:
        return _result(task_id, clip_id, clip_dir, kind="adopted", style_id=None,
                       replace_intro=replace_intro, qc=cached.get("qc"), idempotent=True,
                       source={"type": "adopted", "task_id": source_task_id,
                               "clip_id": source_clip_id},
                       extra={"applied_at": cached.get("applied_at"),
                              "note": "同一来源封面已生效且产物未变，未重复拷贝/备份"})

    baseline = _ensure_baseline(clip_dir, state)
    _atomic_copy(source_cover, clip_dir / COVER_FILENAME)
    source_layout = source_dir / COVER_LAYOUT_FILENAME
    if source_layout.is_file():
        _atomic_copy(source_layout, clip_dir / COVER_LAYOUT_FILENAME)

    try:
        qc = _apply_intro_if_needed(clip_dir, None, replace_intro)
    except Exception:
        _restore_files(clip_dir, state)
        raise

    record = {
        "key": key, "kind": "adopted", "style_id": None,
        "applied_at": _now_iso(), "replace_intro": bool(replace_intro),
        "source": {"type": "adopted", "task_id": source_task_id, "clip_id": source_clip_id,
                   "cover_sha256": _sha256(source_cover)},
        "qc": qc, "result_sha256": _current_hashes(clip_dir),
        "baseline_sha256": (baseline or {}).get("sha256"),
    }
    _upsert_record(state, record, key)
    _atomic_json(_state_path(clip_dir), state)
    revision = _snapshot_new_revision(clip_dir, record, replace_intro=replace_intro,
                                      kind="adopted", style_id=None)
    return _result(task_id, clip_id, clip_dir, kind="adopted", style_id=None,
                   replace_intro=replace_intro, qc=qc, idempotent=False,
                   source=record["source"],
                   extra={"applied_at": record["applied_at"], "cover_revision": revision})


def _apply_intro_if_needed(clip_dir: Path, style_id: str | None,
                           replace_intro: bool) -> dict[str, Any] | None:
    """按需替换片头；``replace_intro=False`` 时只留封面图（返回 None）。

    ``realframe_v2`` 路由补充（2026-10-18）：staged 成片（clip_render 单次编码）
    的 0.8s 边界不在关键帧上，无损三段拼接的 QC（尾部逐位一致）必然失败——
    此时回退到本地管线 :func:`cover_realframe_v2.splice_intro_reencode`
    （等长重编码、时长/分辨率/音轨不变、QC 后原子替换）；其余风格保持原语义
    （失败即回滚保旧）。
    """
    if not replace_intro:
        return None
    intro_s = style_intro_duration(style_id)
    fade_s = style_fade_in(style_id)
    # 封面版型（template:*，2026-10-21）与 realframe_v2 同走重编码感知路由：
    # staged 成片的 0.8s 边界不在关键帧上，无损拼接 QC 必失败。
    if style_id and str(style_id).startswith("template:"):
        style_id = "realframe_v2"  # 片头机制与 realframe_v2 同源（产物封面已按版型出图）
    if style_id != "realframe_v2":
        return splice_intro(clip_dir / CLIP_FILENAME, clip_dir / COVER_FILENAME,
                            intro_duration_s=intro_s, fade_in_s=fade_s)
    # realframe_v2 路由（2026-10-18）：先用既有 plan_intro_split 判定切点模式——
    # staged 成片（clip_render 单次编码）的 0.8s 边界不在关键帧上，无损三段拼接的
    # QC（尾部逐位一致）必然失败，此时走本地管线 :func:`cover_realframe_v2.
    # splice_intro_reencode`（等长重编码、时长/分辨率/音轨不变、QC 后原子替换）；
    # 关键帧对齐（legacy 成片）仍走既有 splice_intro 全程流拷贝。probe/plan 失败
    # （成片损坏等）按原语义抛 E_COVER_QC_FAILED → 调用方回滚保旧。
    before = probe_streams(clip_dir / CLIP_FILENAME)
    plan = plan_intro_split(before["fps"], before["frames"],
                            keyframe_list(clip_dir / CLIP_FILENAME),
                            max(1, int(round(intro_s * before["fps"]))))
    if plan["mode"] == "exact_copy":
        return splice_intro(clip_dir / CLIP_FILENAME, clip_dir / COVER_FILENAME,
                            intro_duration_s=intro_s, fade_in_s=fade_s)
    from . import cover_realframe_v2
    return cover_realframe_v2.splice_intro_reencode(
        clip_dir / CLIP_FILENAME, clip_dir / COVER_FILENAME,
        intro_duration_s=intro_s, fade_in_s=fade_s, fade_out_s=0.18)


def _upsert_record(state: dict[str, Any], record: dict[str, Any], key: str) -> None:
    records = [r for r in (state.get("records") or [])
               if not (isinstance(r, dict) and r.get("key") == key)]
    records.append(record)
    state["records"] = records[-20:]                 # 台账只留最近 20 条
    state["active"] = key
    state["updated_at"] = _now_iso()


def restore_cover(task_id: str, clip_id: str) -> dict[str, Any]:
    """一键回滚到本通道首次改动前的封面图与成片（备份目录保留，可重复回滚）。"""
    clip_dir = _clip_dir_or_raise(task_id, clip_id)
    state = _load_state(clip_dir)
    if not state.get("baseline"):
        raise CoverReplaceError(
            E_COVER_BACKUP_MISSING,
            f"成片 {task_id}/{clip_id} 没有本通道建立的备份基线，无需/无法回滚")
    verification = _restore_files(clip_dir, state)
    if not verification["restored"]:
        raise CoverReplaceError(
            E_COVER_QC_FAILED, f"回滚后校验不一致: {verification['sha256_match']}")
    previous = state.get("active")
    state["active"] = None
    state["updated_at"] = _now_iso()
    state["records"] = (state.get("records") or [])[-19:] + [{
        "key": "restore", "kind": "restore", "applied_at": _now_iso(),
        "restored_from": previous, "verification": verification,
    }]
    _atomic_json(_state_path(clip_dir), state)
    return _result(task_id, clip_id, clip_dir, kind="restore", style_id=None,
                   replace_intro=False, qc={"restore_verification": verification},
                   idempotent=False, source=None,
                   extra={"restored_from": previous,
                          "baseline": state.get("baseline")})


def read_cover_state(task_id: str, clip_id: str) -> dict[str, Any]:
    """读封面替换台账（UI 展示「当前封面来源 / 版本号 / 可否撤销」）。"""
    clip_dir = _clip_dir_or_raise(task_id, clip_id)
    state = _load_state(clip_dir)
    baseline = state.get("baseline") or {}
    cover = clip_dir / COVER_FILENAME
    active_record = _find_record(state, str(state.get("active") or ""))
    revision = current_revision(clip_dir)
    active_revision = int(state.get("active_revision") or revision or 0)
    return {
        "task_id": task_id,
        "clip_id": clip_id,
        "cover_path": _clip_artifact_paths(task_id, clip_dir)["cover_path"],
        "cover_url": f"/studio/auto-edit/{task_id}/artifact"
                     f"?path={_clip_artifact_paths(task_id, clip_dir)['cover_path']}"
                     f"&v={_file_version(cover)}",
        "has_cover": cover.is_file(),
        "has_baseline": bool(baseline),
        "can_restore": bool(baseline and _backup_ready(clip_dir, baseline)),
        "active": state.get("active"),
        "active_kind": (active_record or {}).get("kind"),
        "active_style_id": (active_record or {}).get("style_id"),
        "cover_revision": revision,
        "active_revision": active_revision,
        "cover_asset_id": f"cover-{clip_id}-v{active_revision}" if active_revision else None,
        "can_undo": revision > 1 or active_revision > 1,
        "undo_revision": 1 if (revision > 1 or active_revision > 1) else None,
        "cover_sha256": _sha256(cover) if cover.is_file() else None,
        "revisions": [
            {"revision": int(r.get("revision") or 0), "kind": r.get("kind"),
             "style_id": r.get("style_id"), "created_at": r.get("created_at"),
             "cover_sha256": r.get("cover_sha256"),
             "saved_clip_final": bool(r.get("saved_clip_final"))}
            for r in list_revisions(clip_dir)
        ],
        "baseline": {
            "created_at": baseline.get("created_at"),
            "frames": baseline.get("frames"),
            "duration_s": baseline.get("duration_s"),
        } if baseline else None,
        "records": [
            {"key": r.get("key"), "kind": r.get("kind"), "style_id": r.get("style_id"),
             "applied_at": r.get("applied_at"), "replace_intro": r.get("replace_intro"),
             "source": r.get("source"), "copy_report": r.get("copy_report")}
            for r in (state.get("records") or []) if isinstance(r, dict)
        ],
        "supported_styles": list(SUPPORTED_COVER_STYLES),
        "default_style_id": DEFAULT_COVER_STYLE,
        # 两条链路的缺省风格（配置化：manifest cover_rules）
        "style_for_auto_edit": _chain_style("auto_edit"),
        "style_for_regenerate": regenerate_default_style()[0],
        "style_for_regenerate_source": regenerate_default_style()[1],
        # cutout_v3 可用性：缺权重时的明确原因（前端据此提示「需权重」而不是只显示“失败”）
        "style_availability": style_availability(),
    }


def _backup_ready(clip_dir: Path, baseline: dict[str, Any]) -> bool:
    """基线备份是否可用于回滚：成片备份必须有；封面备份只有基线记了 cover_jpg 才要求。"""
    backup_root = _backup_dir(clip_dir)
    clip_name = str(baseline.get("clip_final") or "")
    if not clip_name or not (backup_root / clip_name).is_file():
        return False
    cover_name = str(baseline.get("cover_jpg") or "")
    if not cover_name:
        return True   # 基线本来就是「无封面」：回滚=删除封面，不需要封面备份
    return (backup_root / cover_name).is_file()


# ---------------------------------------------------------------------------
# 封面修订版本（v1 = 原封面，v2.. = 每次成功重制）——「撤销到 v1」的载体
# ---------------------------------------------------------------------------

def revisions_root(clip_dir: Path) -> Path:
    return clip_dir / COVER_REVISIONS_DIRNAME


def revision_dir(clip_dir: Path, revision: int) -> Path:
    return revisions_root(clip_dir) / f"rev-{int(revision):03d}"


def current_revision(clip_dir: Path) -> int:
    """当前封面版本号：有快照就取最大编号，否则基线存在记 1，都没有记 0。"""
    root = revisions_root(clip_dir)
    highest = 0
    if root.is_dir():
        for entry in root.iterdir():
            match = re.fullmatch(r"rev-(\d{3,})", entry.name)
            if match and entry.is_dir():
                highest = max(highest, int(match.group(1)))
    if highest:
        return highest
    state = _load_state(clip_dir)
    return 1 if state.get("baseline") else 0


def snapshot_revision(clip_dir: Path, revision: int, meta: dict[str, Any]) -> dict[str, Any]:
    """把**当前**封面图/成片/版面记录整份快照成 ``rev-NNN``（幂等：已存在则原样复用）。

    只在一次成功的封面变更之后调用，因此快照内容就是该版本的最终产物；重复执行不会
    叠加备份（同一 revision 目录已存在即直接返回），满足「幂等」硬约束。
    """
    target = revision_dir(clip_dir, revision)
    manifest_path_ = target / COVER_REVISION_META
    if manifest_path_.is_file():
        try:
            existing = json.loads(manifest_path_.read_text(encoding="utf-8"))
            if isinstance(existing, dict) and existing.get("cover_sha256"):
                return existing
        except (OSError, ValueError):
            pass
    target.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "revision": int(revision),
        "created_at": _now_iso(),
        "cover_jpg": COVER_FILENAME,
        "clip_final": CLIP_FILENAME,
        "cover_sha256": None,
        "clip_final_sha256": None,
        "saved_clip_final": False,
    }
    cover = clip_dir / COVER_FILENAME
    if cover.is_file():
        _atomic_copy(cover, target / COVER_FILENAME)
        payload["cover_sha256"] = _sha256(target / COVER_FILENAME)
    video = clip_dir / CLIP_FILENAME
    if video.is_file():
        # 成片很大（每条 ~7-20MB）：只在片头真被换过时才连带快照，
        # 否则撤销封面图就够了，避免每个版本白占一份磁盘。
        save_video = bool(meta.get("replace_intro"))
        payload["saved_clip_final"] = save_video
        if save_video:
            _atomic_copy(video, target / CLIP_FILENAME)
            payload["clip_final_sha256"] = _sha256(target / CLIP_FILENAME)
    layout = clip_dir / COVER_LAYOUT_FILENAME
    if layout.is_file():
        _atomic_copy(layout, target / COVER_LAYOUT_FILENAME)
        payload["cover_layout"] = COVER_LAYOUT_FILENAME
    payload.update({key: value for key, value in (meta or {}).items() if key not in payload})
    _atomic_json(manifest_path_, payload)
    return payload


def list_revisions(clip_dir: Path) -> list[dict[str, Any]]:
    """按版本号升序列出全部快照（v1 若来自基线也一并呈现，供 UI 撤销）。"""
    root = revisions_root(clip_dir)
    revisions: list[dict[str, Any]] = []
    if root.is_dir():
        for entry in sorted(root.iterdir()):
            meta = entry / COVER_REVISION_META
            if meta.is_file():
                try:
                    payload = json.loads(meta.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(payload, dict):
                    revisions.append(payload)
    if not revisions or revisions[0].get("revision") != 1:
        state = _load_state(clip_dir)
        baseline = state.get("baseline")
        if isinstance(baseline, dict):
            revisions.insert(0, {
                "revision": 1,
                "created_at": baseline.get("created_at"),
                "kind": "auto_edit",
                "cover_sha256": (baseline.get("sha256") or {}).get("cover_jpg"),
                "clip_final_sha256": (baseline.get("sha256") or {}).get("clip_final"),
                "saved_clip_final": True,
                "from_baseline": True,
                "baseline_cover_jpg": baseline.get("cover_jpg"),
            })
    return revisions


def install_revision(clip_dir: Path, revision: int) -> dict[str, Any]:
    """把某一版封面写回成片目录（原子替换 + 逐字节校验）；v1 走基线回滚。"""
    state = _load_state(clip_dir)
    target = revision_dir(clip_dir, revision)
    meta_path = target / COVER_REVISION_META
    if not meta_path.is_file():
        if int(revision) == 1 and state.get("baseline"):
            verification = _restore_files(clip_dir, state)
            return {"revision": 1, **verification, "from_baseline": True}
        raise CoverReplaceError(
            E_COVER_BACKUP_MISSING, f"封面历史里没有 v{int(revision)} 的快照，无法撤销")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    cover_backup = target / str(meta.get("cover_jpg") or COVER_FILENAME)
    if not cover_backup.is_file():
        raise CoverReplaceError(
            E_COVER_BACKUP_MISSING, f"v{int(revision)} 的封面快照缺失，无法撤销")
    _atomic_copy(cover_backup, clip_dir / COVER_FILENAME)
    restored_video = False
    if meta.get("saved_clip_final"):
        clip_backup = target / str(meta.get("clip_final") or CLIP_FILENAME)
        if clip_backup.is_file():
            _atomic_copy(clip_backup, clip_dir / CLIP_FILENAME)
            restored_video = True
    layout_backup = target / str(meta.get("cover_layout") or "")
    if layout_backup and layout_backup.is_file():
        _atomic_copy(layout_backup, clip_dir / COVER_LAYOUT_FILENAME)
    verified = _sha256(clip_dir / COVER_FILENAME) == meta.get("cover_sha256")
    if not verified:
        raise CoverReplaceError(
            E_COVER_QC_FAILED, f"撤销到 v{int(revision)} 后封面哈希不一致")
    return {
        "revision": int(revision),
        "restored": True,
        "restored_video": restored_video,
        "cover_sha256": meta.get("cover_sha256"),
        "duration_s": meta.get("duration_s"),
        "frames": meta.get("frames"),
    }


def list_project_clips(task_id: str) -> list[dict[str, Any]]:
    """该项目所有成片的封面摘要（供「沿用已生成封面」的来源选择）。"""
    from .studio_auto_edit_bridge import read_auto_edit_task

    view = read_auto_edit_task(task_id)
    results: list[dict[str, Any]] = []
    for clip in view.get("clips") or []:
        clip_id = str(clip.get("clip_id") or "")
        if not clip_id:
            continue
        try:
            state = read_cover_state(task_id, clip_id)
        except CoverReplaceError:
            continue
        results.append({
            "clip_id": clip_id, "name": clip.get("name"), "topic": clip.get("topic"),
            "cover_path": clip.get("cover_path"), "has_cover": state["has_cover"],
            "active": state["active"], "active_kind": state["active_kind"],
            "can_restore": state["can_restore"], "cover_url": state["cover_url"],
        })
    return results
