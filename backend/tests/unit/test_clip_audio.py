"""clip_audio 单元测试：峰值数学、audio_tracks 校验、BGM 落盘/删除。

默认**不依赖真实媒体文件**：``shutil.which`` 与 ``subprocess.run`` 被打桩，喂进
合成 PCM 字节直接断言峰值数学。只有标了 skipif 的用例才会调真实 ffmpeg
（本容器有 ffmpeg，所以它们会真的跑一遍 1 秒正弦 WAV）。
"""

from __future__ import annotations

import array
import hashlib
import json
import shutil
import subprocess
import wave
from pathlib import Path

import pytest

from src import clip_audio as ca

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


# ---------------------------------------------------------------------------
# 打桩工具：假 ffmpeg
# ---------------------------------------------------------------------------

class _Completed:
    def __init__(self, stdout=b"", returncode=0, stderr=b""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def _pcm(samples: list[int]) -> bytes:
    return array.array("h", samples).tobytes()


def _fake_ffmpeg(monkeypatch, payload: bytes = b"", *, returncode: int = 0,
                 stderr: bytes = b"", which=None) -> dict:
    """把 ``shutil.which`` / ``subprocess.run`` 换成可控版本，返回调用记录。"""

    calls: dict = {"commands": []}
    monkeypatch.setattr(ca.shutil, "which", which or (lambda name: f"/usr/bin/{name}"))

    def fake_run(command, **kwargs):
        calls["commands"].append(command)
        return _Completed(stdout=payload, returncode=returncode, stderr=stderr)

    monkeypatch.setattr(ca.subprocess, "run", fake_run)
    return calls


def _write_wav(path: Path, samples: list[int], rate: int = 8000) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(array.array("h", samples).tobytes())
    return path


# ---------------------------------------------------------------------------
# 1. extract_peaks（合成 PCM，不碰真实文件）
# ---------------------------------------------------------------------------

def test_silent_buffer_yields_all_zero_peaks(tmp_path, monkeypatch):
    _fake_ffmpeg(monkeypatch, _pcm([0] * 8000))
    assert ca.extract_peaks(tmp_path / "silent.wav", points=8) == [0.0] * 8


def test_loud_second_half_is_mirrored_in_peaks(tmp_path, monkeypatch):
    _fake_ffmpeg(monkeypatch, _pcm([0] * 4000 + [32767] * 4000))
    peaks = ca.extract_peaks(tmp_path / "ramp.wav", points=8)
    assert peaks == [0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0]


def test_peaks_are_normalised_and_length_is_capped(tmp_path, monkeypatch):
    _fake_ffmpeg(monkeypatch, _pcm([-32768] * 100))
    assert ca.extract_peaks(tmp_path / "full.wav", points=4) == [1.0, 1.0, 1.0, 1.0]

    _fake_ffmpeg(monkeypatch, _pcm([1000] * 8000))
    peaks = ca.extract_peaks(tmp_path / "a.wav")
    assert len(peaks) == ca.MAX_PEAKS == 800
    assert peaks[0] == round(1000 / 32768.0, 4)
    # 超过硬上限的请求被钳到 800（契约要求 peaks ≤ 800）。
    assert len(ca.extract_peaks(tmp_path / "a.wav", points=5000)) == ca.MAX_PEAKS


def test_extract_peaks_resamples_short_audio_to_requested_points(tmp_path, monkeypatch):
    _fake_ffmpeg(monkeypatch, _pcm([0, 0, 32767, 32767]))
    peaks = ca.extract_peaks(tmp_path / "tiny.wav", points=4)
    assert peaks == [0.0, 0.0, 1.0, 1.0]
    # 采样点比 points 少时线性插值补齐（仍是 points 个）。
    assert len(ca.extract_peaks(tmp_path / "tiny.wav", points=8)) == 8


def test_extract_peaks_passes_expected_ffmpeg_arguments(tmp_path, monkeypatch):
    calls = _fake_ffmpeg(monkeypatch, _pcm([1] * 10))
    ca.extract_peaks(tmp_path / "x.mp3", points=5)
    command = calls["commands"][0]
    assert command[0] == "/usr/bin/ffmpeg"
    assert "-ac" in command and command[command.index("-ac") + 1] == "1"
    assert "-ar" in command and command[command.index("-ar") + 1] == "8000"
    assert command[command.index("-f") + 1] == "s16le"
    assert command[-1] == "-"
    assert command[command.index("-t") + 1] == str(ca.MAX_ANALYZE_SECONDS)


def test_extract_peaks_without_ffmpeg_raises_readable_error(tmp_path, monkeypatch):
    _fake_ffmpeg(monkeypatch, which=lambda name: None)
    with pytest.raises(ca.ClipAudioError) as excinfo:
        ca.extract_peaks(tmp_path / "x.wav")
    assert excinfo.value.code == "E_AUDIO_PEAKS_FAILED"
    assert "ffmpeg" in str(excinfo.value)


def test_extract_peaks_failed_run_reports_stderr(tmp_path, monkeypatch):
    _fake_ffmpeg(monkeypatch, returncode=1, stderr=b"Invalid data found when processing input")
    with pytest.raises(ca.ClipAudioError) as excinfo:
        ca.extract_peaks(tmp_path / "broken.wav")
    assert excinfo.value.code == "E_AUDIO_PEAKS_FAILED"
    assert "Invalid data found" in str(excinfo.value)


def test_extract_peaks_empty_output_is_empty_list(tmp_path, monkeypatch):
    _fake_ffmpeg(monkeypatch, b"")
    assert ca.extract_peaks(tmp_path / "empty.wav") == []


def test_module_import_does_not_require_ffmpeg():
    """import 期不做二进制检查：模块级常量与纯函数在无 ffmpeg 时也可用。"""

    assert ca.MAX_PEAKS == 800
    assert ca.SUPPORTED_BGM_EXTENSIONS[0] == ".mp3"
    assert ca.audio_dir(Path("/tmp/clip")) == Path("/tmp/clip/audio")


# ---------------------------------------------------------------------------
# 2. 真实 ffmpeg 冒烟（有 ffmpeg 才跑；本容器会真的执行）
# ---------------------------------------------------------------------------

@pytest.mark.skipif(FFMPEG is None, reason="本机没有 ffmpeg，跳过真实音频 smoke")
def test_extract_peaks_on_real_sine_wav(tmp_path):
    wav = tmp_path / "sine.wav"
    subprocess.run(
        [FFMPEG, "-y", "-v", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=1", str(wav)],
        check=True, timeout=120,
    )
    peaks = ca.extract_peaks(wav, points=50)
    assert len(peaks) == 50
    assert all(0.0 <= peak <= 1.0 for peak in peaks)
    # 440Hz 正弦：每个 20ms 窗口都含有近乎满幅的采样点，各窗口峰值几乎一致
    # （ffmpeg 的 sine 滤镜在 s16 下幅度是 1/8 满幅，所以只做相对断言）。
    assert min(peaks) > 0.1
    assert max(peaks) - min(peaks) < 0.01


@pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="本机没有 ffmpeg/ffprobe，跳过时长探测")
def test_probe_duration_on_real_wav(tmp_path):
    wav = tmp_path / "sine.wav"
    subprocess.run(
        [FFMPEG, "-y", "-v", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=1", str(wav)],
        check=True, timeout=120,
    )
    duration = ca.probe_duration(wav)
    assert duration is not None and abs(duration - 1.0) < 0.05


def test_probe_duration_returns_none_when_ffprobe_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(ca.shutil, "which", lambda name: None)
    assert ca.probe_duration(tmp_path / "x.wav") is None


def test_probe_duration_returns_none_on_failure(tmp_path, monkeypatch):
    _fake_ffmpeg(monkeypatch, returncode=1, stderr=b"boom")
    assert ca.probe_duration(tmp_path / "x.wav") is None


# ---------------------------------------------------------------------------
# 3. audio_tracks 校验
# ---------------------------------------------------------------------------

def test_normalize_track_fills_defaults():
    track = ca.normalize_track({"asset_id": "bgm-12345678", "path": "audio/bgm-12345678.mp3"})
    assert track["kind"] == "bgm"
    assert track["volume"] == 1.0
    assert track["fade_in_s"] == 0.0 and track["fade_out_s"] == 0.0
    assert track["duration_s"] == 0.0
    assert track["peaks"] == [] and track["bytes"] == 0 and track["sha256"] is None
    assert set(track) == {"id", "kind", "asset_id", "path", "filename", "volume", "fade_in_s",
                          "fade_out_s", "duration_s", "peaks", "sha256", "bytes", "created_at"}


def test_normalize_track_keeps_valid_values_and_truncates_peaks():
    track = ca.normalize_track({
        "id": "bgm-1", "kind": "bgm", "asset_id": "bgm-1", "path": "audio/bgm-1.wav",
        "filename": "背景乐.wav", "volume": 0.3, "fade_in_s": 1.5, "fade_out_s": 2,
        "duration_s": 42.5, "peaks": [0.0, 0.5, 1.0], "sha256": "abc", "bytes": 12,
        "created_at": "2026-10-13T00:00:00Z", "未知键": "会被丢掉",
    })
    assert track["volume"] == 0.3 and track["fade_in_s"] == 1.5 and track["duration_s"] == 42.5
    assert track["filename"] == "背景乐.wav"
    assert "未知键" not in track

    long_track = ca.normalize_track({"peaks": [0.1] * (ca.MAX_PEAKS + 50)})
    assert len(long_track["peaks"]) == ca.MAX_PEAKS


@pytest.mark.parametrize("raw", [
    "not-a-dict",
    {"volume": 1.5},
    {"volume": -0.1},
    {"volume": "half"},
    {"volume": True},
    {"fade_in_s": -1},
    {"fade_out_s": "2"},
    {"duration_s": float("nan")},
    {"peaks": "0123"},
    {"peaks": [0.5, 1.2]},
    {"peaks": [True]},
    {"peaks": ["0.5"]},
    {"kind": "voice"},
    {"kind": 1},
    {"filename": 7},
    {"bytes": -3},
    {"bytes": 1.5},
    {"sha256": 12},
])
def test_normalize_track_rejects_invalid_values(raw):
    with pytest.raises(ca.ClipAudioError) as excinfo:
        ca.normalize_track(raw)
    assert excinfo.value.code == "E_AUDIO_TRACK_INVALID"


def test_validate_tracks_accepts_none_and_rejects_non_list():
    assert ca.validate_tracks(None) == []
    assert ca.validate_tracks([]) == []
    assert len(ca.validate_tracks([{"asset_id": "a"}, {"asset_id": "b"}])) == 2
    for raw in ("tracks", {}, 5):
        with pytest.raises(ca.ClipAudioError) as excinfo:
            ca.validate_tracks(raw)
        assert excinfo.value.code == "E_AUDIO_TRACK_INVALID"


def test_peaks_payload_flags_truncation():
    payload = ca.peaks_payload({"asset_id": "bgm-1", "duration_s": 12.0,
                                "peaks": [0.25, 0.5]})
    assert payload["asset_id"] == "bgm-1"
    assert payload["peaks"] == [0.25, 0.5] and payload["peak_count"] == 2
    assert payload["truncated"] is False

    truncated = ca.peaks_payload({"peaks": [0.1] * (ca.MAX_PEAKS + 1)})
    assert truncated["peak_count"] == ca.MAX_PEAKS and truncated["truncated"] is True


# ---------------------------------------------------------------------------
# 4. save_bgm / delete_bgm
# ---------------------------------------------------------------------------

def test_save_bgm_rejects_unsupported_extension_before_copying(tmp_path, monkeypatch):
    _fake_ffmpeg(monkeypatch, _pcm([1] * 100))
    source = _write_wav(tmp_path / "song.wav", [0] * 100)
    target = tmp_path / "clip"

    with pytest.raises(ca.ClipAudioError) as excinfo:
        ca.save_bgm(target, clip_id="clip-1", filename="cover.mp4", source_path=source,
                    max_bytes=10_000)
    assert excinfo.value.code == "E_AUDIO_UNSUPPORTED_FORMAT"
    assert not ca.audio_dir(target).exists()

    with pytest.raises(ca.ClipAudioError) as no_ext:
        ca.save_bgm(target, clip_id="clip-1", filename="cover", source_path=source, max_bytes=10_000)
    assert no_ext.value.code == "E_AUDIO_UNSUPPORTED_FORMAT"


def test_save_bgm_enforces_max_bytes(tmp_path, monkeypatch):
    _fake_ffmpeg(monkeypatch, _pcm([1] * 100))
    source = _write_wav(tmp_path / "song.wav", [0] * 8000)
    with pytest.raises(ca.ClipAudioError) as excinfo:
        ca.save_bgm(tmp_path / "clip", clip_id="clip-1", filename="song.wav",
                    source_path=source, max_bytes=16)
    assert excinfo.value.code == "E_AUDIO_FILE_TOO_LARGE"
    assert not ca.audio_dir(tmp_path / "clip").exists()


def test_save_bgm_missing_source_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        ca.save_bgm(tmp_path / "clip", clip_id="clip-1", filename="song.mp3",
                    source_path=tmp_path / "nope.mp3", max_bytes=10_000)


def test_save_bgm_writes_content_addressed_asset(tmp_path, monkeypatch):
    monkeypatch.setattr(ca, "probe_duration", lambda _path: 12.345)
    monkeypatch.setattr(ca, "extract_peaks", lambda _path, points=ca.MAX_PEAKS: [0.5, 1.0])
    source = _write_wav(tmp_path / "背景乐.wav", [0] * 800)
    clip_dir = tmp_path / "clip"

    track = ca.save_bgm(clip_dir, clip_id="clip-1", filename="背景乐.wav",
                        source_path=source, max_bytes=10_000_000)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    assert track["asset_id"] == f"bgm-{digest[:8]}"
    assert track["path"] == f"audio/bgm-{digest[:8]}.wav"
    assert track["sha256"] == digest
    assert track["bytes"] == source.stat().st_size
    assert track["duration_s"] == 12.345
    assert track["peaks"] == [0.5, 1.0]
    assert track["volume"] == 1.0 and track["kind"] == "bgm"

    stored = clip_dir / track["path"]
    assert stored.is_file() and stored.read_bytes() == source.read_bytes()
    assert ca.normalize_track(track) == track   # 返回的 track 本身就是合法条目

    # 同样的字节再传一次 → 同一个资产（内容寻址），不产生第二个文件。
    again = ca.save_bgm(clip_dir, clip_id="clip-1", filename="别名.wav",
                        source_path=source, max_bytes=10_000_000)
    assert again["asset_id"] == track["asset_id"]
    assert sorted(path.name for path in ca.audio_dir(clip_dir).iterdir()) == [stored.name]


def test_save_bgm_defaults_duration_to_zero_when_probe_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(ca, "probe_duration", lambda _path: None)
    monkeypatch.setattr(ca, "extract_peaks", lambda _path, points=ca.MAX_PEAKS: [])
    source = _write_wav(tmp_path / "song.wav", [0] * 100)
    track = ca.save_bgm(tmp_path / "clip", clip_id="clip-1", filename="song.wav",
                        source_path=source, max_bytes=10_000_000)
    assert track["duration_s"] == 0.0 and track["peaks"] == []


@pytest.mark.skipif(FFMPEG is None, reason="本机没有 ffmpeg，跳过真实 BGM 落盘 smoke")
def test_save_bgm_end_to_end_with_real_audio(tmp_path):
    source = tmp_path / "sine.wav"
    subprocess.run(
        [FFMPEG, "-y", "-v", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=1", str(source)],
        check=True, timeout=120,
    )
    clip_dir = tmp_path / "clip"
    track = ca.save_bgm(clip_dir, clip_id="clip-1", filename="sine.wav",
                        source_path=source, max_bytes=10_000_000)
    assert (clip_dir / track["path"]).is_file()
    assert len(track["peaks"]) == ca.MAX_PEAKS
    assert min(track["peaks"]) > 0.1 and max(track["peaks"]) <= 1.0
    if FFPROBE:  # ffprobe 在的话顺带校验时长
        assert abs(track["duration_s"] - 1.0) < 0.05


def test_delete_bgm_removes_file_and_is_idempotent(tmp_path):
    audio = ca.audio_dir(tmp_path / "clip")
    audio.mkdir(parents=True)
    target = audio / "bgm-deadbeef.mp3"
    target.write_bytes(b"BGM")

    assert ca.delete_bgm(tmp_path / "clip", "bgm-deadbeef") == {
        "removed": True, "asset_id": "bgm-deadbeef", "path": "audio/bgm-deadbeef.mp3"}
    assert not target.exists()

    assert ca.delete_bgm(tmp_path / "clip", "bgm-deadbeef") == {
        "removed": False, "asset_id": "bgm-deadbeef", "path": None}
    # 目录都没有时同样只是「没删到」，不抛错。
    assert ca.delete_bgm(tmp_path / "other", "bgm-00000000")["removed"] is False


@pytest.mark.parametrize("asset_id", ["", "  ", "../secret", "a/b", ".hidden", ".."])
def test_delete_bgm_rejects_unsafe_asset_id(tmp_path, asset_id):
    with pytest.raises(ca.ClipAudioError) as excinfo:
        ca.delete_bgm(tmp_path / "clip", asset_id)
    assert excinfo.value.code == "E_AUDIO_TRACK_INVALID"


def test_saved_track_is_json_serializable(tmp_path, monkeypatch):
    monkeypatch.setattr(ca, "probe_duration", lambda _path: 1.0)
    monkeypatch.setattr(ca, "extract_peaks", lambda _path, points=ca.MAX_PEAKS: [0.25])
    source = _write_wav(tmp_path / "song.wav", [0] * 100)
    track = ca.save_bgm(tmp_path / "clip", clip_id="clip-1", filename="song.wav",
                        source_path=source, max_bytes=10_000_000)
    assert json.loads(json.dumps(track)) == track
