"""``timeline_audio`` 单元测试：轨模型校验、纯函数变换、peaks 缓存与真实解码。

默认不依赖真实媒体（``extract_peaks`` 被替换成计数/合成打桩）；
只有标了 ``skipif`` 的用例才会调真实 ffmpeg（本容器有 ffmpeg，会真跑 1 秒正弦）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import wave
from pathlib import Path

import pytest

from src import clip_audio as ca
from src import timeline_audio as ta

FFMPEG = shutil.which("ffmpeg")


def _revision(tracks: list[dict] | None = None, **extra) -> dict:
    return {"ranges": [{"id": "t1"}], "audio_tracks": list(tracks or []), **extra}


def _bgm(track_id: str = "bgm_1", **overrides) -> dict:
    track = {
        "track_id": track_id, "kind": "bgm", "asset_id": "BGM1", "path": "/tmp/bgm1.mp3",
        "source_start_s": 0.0, "source_end_s": 5.0, "record_start_s": 1.0,
        "volume": 0.8, "muted": False, "fade_in_s": 0.0, "fade_out_s": 0.0,
    }
    track.update(overrides)
    return track


def _write_sine(path: Path, seconds: float = 1.0) -> Path:
    subprocess.run(
        [FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
         f"sine=frequency=440:duration={seconds}", str(path)],
        check=True, timeout=60)
    return path


def _write_wav_silence(path: Path) -> Path:
    """不依赖 ffmpeg 的合法 WAV 文件（打桩 extract_peaks 时用）。"""

    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * 800)
    return path


# ---------------------------------------------------------------------------
# 轨模型校验
# ---------------------------------------------------------------------------

def test_track_defaults():
    track = ta.AudioTrack(track_id="bgm_1")
    payload = track.model_dump(mode="json")
    assert payload["kind"] == "bgm"
    assert payload["source_start_s"] == 0.0
    assert payload["volume"] == 1.0
    assert payload["muted"] is False
    assert payload["fade_in_s"] == 0.0 and payload["fade_out_s"] == 0.0
    assert payload["origin"] == "user"
    assert payload["trim"] is None
    assert payload["asset_id"] is None and payload["path"] is None


@pytest.mark.parametrize("kind", ["voice", "bgm", "sfx"])
def test_track_accepts_all_kinds(kind):
    assert ta.AudioTrack(track_id="x", kind=kind).kind == kind


@pytest.mark.parametrize("kind", ["music", "", None, 5])
def test_track_rejects_bad_kind(kind):
    with pytest.raises(Exception):
        ta.AudioTrack(track_id="x", kind=kind)


@pytest.mark.parametrize("track_id", ["", "   ", None, 5])
def test_track_requires_track_id(track_id):
    with pytest.raises(Exception):
        ta.AudioTrack(track_id=track_id)


def test_track_rejects_negative_seconds():
    for field in ("source_start_s", "source_end_s", "record_start_s",
                  "fade_in_s", "fade_out_s"):
        with pytest.raises(ta.TimelineAudioError) as excinfo:
            ta.normalise_tracks([_bgm(**{field: -1.0})])
        assert excinfo.value.code == ta.E_TIMELINE_AUDIO_TRACK_INVALID
        assert ta.E_TIMELINE_AUDIO_TRACK_INVALID in str(excinfo.value)


@pytest.mark.parametrize("volume", [-0.1, 1.1, 2])
def test_track_rejects_volume_out_of_range(volume):
    with pytest.raises(ta.TimelineAudioError) as excinfo:
        ta.normalise_tracks([_bgm(volume=volume)])
    assert excinfo.value.code == ta.E_TIMELINE_AUDIO_TRACK_INVALID


@pytest.mark.parametrize("volume", [0, 0.5, 1, 1.0])
def test_track_accepts_volume_range(volume):
    assert ta.normalise_tracks([_bgm(volume=volume)])[0]["volume"] == float(volume)


@pytest.mark.parametrize("muted", ["yes", 1, 0])
def test_track_rejects_non_bool_muted(muted):
    with pytest.raises(ta.TimelineAudioError):
        ta.normalise_tracks([_bgm(muted=muted)])


def test_track_trim_is_reserved_dict_only():
    assert ta.normalise_tracks([_bgm(trim={"in_s": 1.0})])[0]["trim"] == {"in_s": 1.0}
    with pytest.raises(ta.TimelineAudioError):
        ta.normalise_tracks([_bgm(trim="in_s")])


def test_trim_todo_is_documented():
    assert "TODO" in ta.TRIM_TODO


def test_normalise_tracks_handles_none_and_rejects_non_list():
    assert ta.normalise_tracks(None) == []
    with pytest.raises(ta.TimelineAudioError) as excinfo:
        ta.normalise_tracks({"track_id": "x"})
    assert excinfo.value.code == ta.E_TIMELINE_AUDIO_TRACK_INVALID


def test_normalise_tracks_rejects_duplicate_ids():
    with pytest.raises(ta.TimelineAudioError) as excinfo:
        ta.normalise_tracks([_bgm("dup"), _bgm("dup")])
    assert "重复" in str(excinfo.value)


def test_normalise_tracks_requires_ordered_interval():
    with pytest.raises(ta.TimelineAudioError) as excinfo:
        ta.normalise_tracks([_bgm(source_start_s=5.0, source_end_s=2.0)])
    assert "有序" in str(excinfo.value)
    with pytest.raises(ta.TimelineAudioError):
        ta.normalise_tracks([_bgm(source_start_s=3.0, source_end_s=3.0)])


def test_normalise_tracks_clamps_to_asset_duration():
    """越界策略 = 钳制（需要调用方提供素材时长）。"""

    result = ta.normalise_tracks(
        [_bgm(source_start_s=1.0, source_end_s=99.0)], asset_durations={"BGM1": 8.0})
    assert result[0]["source_start_s"] == 1.0
    assert result[0]["source_end_s"] == 8.0


def test_normalise_tracks_no_clamp_without_duration():
    result = ta.normalise_tracks([_bgm(source_start_s=1.0, source_end_s=99.0)])
    assert result[0]["source_end_s"] == 99.0


def test_normalise_tracks_clamp_allows_path_keyed_duration():
    result = ta.normalise_tracks(
        [_bgm(asset_id=None, path="/tmp/x.mp3", source_end_s=50.0)],
        asset_durations={"/tmp/x.mp3": 4.0})
    assert result[0]["source_end_s"] == 4.0


def test_normalise_tracks_empty_interval_after_clamp_raises():
    with pytest.raises(ta.TimelineAudioError) as excinfo:
        ta.normalise_tracks([_bgm(source_start_s=30.0, source_end_s=40.0)],
                            asset_durations={"BGM1": 5.0})
    assert "为空" in str(excinfo.value)


def test_normalise_tracks_clamps_fades_to_span():
    result = ta.normalise_tracks([_bgm(source_start_s=0.0, source_end_s=2.0,
                                       fade_in_s=5.0, fade_out_s=5.0)])
    assert result[0]["fade_in_s"] == 2.0
    assert result[0]["fade_out_s"] == 2.0


def test_normalise_tracks_keeps_extra_keys():
    result = ta.normalise_tracks([_bgm(color="#fff")])
    assert result[0]["color"] == "#fff"


# ---------------------------------------------------------------------------
# 原声自动进入 / ＋ 菜单守卫
# ---------------------------------------------------------------------------

def test_source_auto_track_is_marked_source():
    track = ta.source_auto_track(asset_id="A001", path="/tmp/a.mp4", duration_s=30.0)
    assert track["origin"] == "source"
    assert track["kind"] == "voice"
    assert track["source_start_s"] == 0.0 and track["source_end_s"] == 30.0
    assert track["record_start_s"] == 0.0
    assert ta.is_source_track(track) is True
    assert ta.is_source_track(_bgm()) is False


def test_seed_source_tracks_is_idempotent():
    track = ta.source_auto_track(asset_id="A001", duration_s=30.0)
    revision = ta.seed_source_tracks(_revision(), [track])
    assert len(revision["audio_tracks"]) == 1
    again = ta.seed_source_tracks(revision, [track])
    assert len(again["audio_tracks"]) == 1


def test_seed_source_tracks_skips_same_asset():
    first = ta.source_auto_track(track_id="voice_a", asset_id="A001", duration_s=30.0)
    second = ta.source_auto_track(track_id="voice_b", asset_id="A001", duration_s=30.0)
    revision = ta.seed_source_tracks(_revision(), [first, second])
    assert ta.track_ids(revision) == ["voice_a"]


def test_seed_source_tracks_forces_source_origin():
    user_track = _bgm("bgm_x")
    revision = ta.seed_source_tracks(_revision(), [user_track])
    assert revision["audio_tracks"][0]["origin"] == "source"


def test_seed_source_tracks_rejects_non_list():
    with pytest.raises(ta.TimelineAudioError):
        ta.seed_source_tracks(_revision(), {"track_id": "x"})


def test_add_track_refuses_source_track():
    revision = ta.seed_source_tracks(
        _revision(), [ta.source_auto_track(asset_id="A001", duration_s=30.0)])
    with pytest.raises(ta.TimelineAudioError) as excinfo:
        ta.add_track(revision, ta.source_auto_track(asset_id="A001", duration_s=30.0))
    assert excinfo.value.code == ta.E_TIMELINE_AUDIO_SOURCE_TRACK_RESERVED
    assert ta.E_TIMELINE_AUDIO_SOURCE_TRACK_RESERVED in str(excinfo.value)


def test_add_track_refuses_same_asset_voice_even_as_user():
    revision = ta.seed_source_tracks(
        _revision(), [ta.source_auto_track(asset_id="A001", duration_s=30.0)])
    duplicate = {"track_id": "voice_user", "kind": "voice", "asset_id": "A001",
                 "source_start_s": 0.0, "source_end_s": 30.0, "record_start_s": 0.0}
    with pytest.raises(ta.TimelineAudioError) as excinfo:
        ta.add_track(revision, duplicate)
    assert excinfo.value.code == ta.E_TIMELINE_AUDIO_SOURCE_TRACK_RESERVED


def test_add_track_allows_new_bgm():
    revision = ta.add_track(_revision(), _bgm())
    assert ta.track_ids(revision) == ["bgm_1"]


def test_add_track_rejects_duplicate_id():
    revision = ta.add_track(_revision(), _bgm())
    with pytest.raises(ta.TimelineAudioError) as excinfo:
        ta.add_track(revision, _bgm())
    assert "已存在" in str(excinfo.value)


def test_add_track_rejects_invalid_track():
    with pytest.raises(ta.TimelineAudioError):
        ta.add_track(_revision(), _bgm(volume=3))


# ---------------------------------------------------------------------------
# 纯函数变换
# ---------------------------------------------------------------------------

def test_new_track_id_prefix_and_uniqueness():
    assert ta.new_track_id("sfx").startswith("sfx_")
    assert ta.new_track_id("bogus").startswith("bgm_")
    assert ta.new_track_id("bgm") != ta.new_track_id("bgm")


def test_remove_track():
    revision = ta.add_track(ta.add_track(_revision(), _bgm("a")), _bgm("b"))
    result = ta.remove_track(revision, "a")
    assert ta.track_ids(result) == ["b"]


def test_remove_track_missing_raises():
    with pytest.raises(ta.TimelineAudioError) as excinfo:
        ta.remove_track(_revision(), "nope")
    assert excinfo.value.code == ta.E_TIMELINE_AUDIO_TRACK_NOT_FOUND
    assert ta.E_TIMELINE_AUDIO_TRACK_NOT_FOUND in str(excinfo.value)


def test_move_track_changes_record_start():
    revision = ta.add_track(_revision(), _bgm())
    moved = ta.move_track(revision, "bgm_1", record_start_s=9.5)
    assert moved["audio_tracks"][0]["record_start_s"] == 9.5
    # 纯函数：入参未被修改。
    assert revision["audio_tracks"][0]["record_start_s"] == 1.0


def test_move_track_source_start_keeps_span():
    revision = ta.add_track(_revision(), _bgm())
    moved = ta.move_track(revision, "bgm_1", source_start_s=2.0)
    track = moved["audio_tracks"][0]
    assert track["source_start_s"] == 2.0
    assert track["source_end_s"] == 7.0  # 5s 长度不变


def test_move_track_requires_a_target():
    revision = ta.add_track(_revision(), _bgm())
    with pytest.raises(ta.TimelineAudioError):
        ta.move_track(revision, "bgm_1")


def test_move_track_rejects_negative_record_start():
    revision = ta.add_track(_revision(), _bgm())
    with pytest.raises(ta.TimelineAudioError):
        ta.move_track(revision, "bgm_1", record_start_s=-1.0)


def test_move_track_missing_raises():
    with pytest.raises(ta.TimelineAudioError) as excinfo:
        ta.move_track(_revision(), "nope", record_start_s=1.0)
    assert excinfo.value.code == ta.E_TIMELINE_AUDIO_TRACK_NOT_FOUND


def test_set_volume_and_immutability():
    revision = ta.add_track(_revision(), _bgm(volume=0.8))
    quiet = ta.set_volume(revision, "bgm_1", 0.25)
    assert quiet["audio_tracks"][0]["volume"] == 0.25
    assert revision["audio_tracks"][0]["volume"] == 0.8
    assert quiet["ranges"] == revision["ranges"]


@pytest.mark.parametrize("volume", [-0.5, 1.5])
def test_set_volume_out_of_range(volume):
    revision = ta.add_track(_revision(), _bgm())
    with pytest.raises(ta.TimelineAudioError):
        ta.set_volume(revision, "bgm_1", volume)


def test_set_muted():
    revision = ta.add_track(_revision(), _bgm())
    assert ta.set_muted(revision, "bgm_1", True)["audio_tracks"][0]["muted"] is True
    with pytest.raises(ta.TimelineAudioError):
        ta.set_muted(revision, "bgm_1", "yes")


def test_set_fades():
    revision = ta.add_track(_revision(), _bgm())
    faded = ta.set_fades(revision, "bgm_1", fade_in_s=0.5, fade_out_s=1.0)
    track = faded["audio_tracks"][0]
    assert track["fade_in_s"] == 0.5 and track["fade_out_s"] == 1.0
    with pytest.raises(ta.TimelineAudioError):
        ta.set_fades(revision, "bgm_1")
    with pytest.raises(ta.TimelineAudioError):
        ta.set_fades(revision, "bgm_1", fade_in_s=-1.0)


def test_update_track_cannot_change_track_id():
    revision = ta.add_track(_revision(), _bgm())
    updated = ta.update_track(revision, "bgm_1", {"track_id": "hacked", "label": "BGM"})
    assert ta.track_ids(updated) == ["bgm_1"]
    assert updated["audio_tracks"][0]["label"] == "BGM"


def test_update_track_missing_raises_and_requires_dict():
    revision = ta.add_track(_revision(), _bgm())
    with pytest.raises(ta.TimelineAudioError) as excinfo:
        ta.update_track(revision, "nope", {"volume": 0.5})
    assert excinfo.value.code == ta.E_TIMELINE_AUDIO_TRACK_NOT_FOUND
    with pytest.raises(ta.TimelineAudioError):
        ta.update_track(revision, "bgm_1", ["volume"])


def test_find_track_and_track_ids():
    revision = ta.add_track(ta.add_track(_revision(), _bgm("a")), _bgm("b"))
    assert ta.track_ids(revision) == ["a", "b"]
    assert ta.find_track(revision, "b")["track_id"] == "b"
    assert ta.find_track(revision, "zz") is None
    # 返回副本，改不动原对象。
    ta.find_track(revision, "a")["volume"] = 0.0
    assert revision["audio_tracks"][0]["volume"] == 0.8


def test_transforms_accept_timeline_revision_like_object():
    class _Model:
        def model_dump(self, mode: str = "json"):
            return _revision()

    result = ta.add_track(_Model(), _bgm())
    assert ta.track_ids(result) == ["bgm_1"]


def test_transforms_reject_bad_revision():
    with pytest.raises(ta.TimelineAudioError) as excinfo:
        ta.add_track(["ranges"], _bgm())
    assert excinfo.value.code == ta.E_TIMELINE_AUDIO_REVISION_INVALID


def test_transforms_survive_history_roundtrip(tmp_path: Path):
    """纯函数产物可直接 push 进历史栈（Phase B 的接线方式）。"""

    from src import timeline_history as th

    revision = ta.seed_source_tracks(
        _revision(), [ta.source_auto_track(asset_id="A001", duration_s=30.0)])
    base = th.push(tmp_path, revision, label="导入")
    with_bgm = ta.add_track(revision, _bgm())
    th.push(tmp_path, with_bgm, label="添加背景音乐")
    assert th.can_undo(tmp_path) is True
    rolled = th.undo(tmp_path)
    assert rolled.revision == base.revision
    assert rolled.audio_tracks[0]["origin"] == "source"
    redone = th.redo(tmp_path)
    assert len(redone.audio_tracks) == 2


def test_tracks_of_rejects_non_list():
    with pytest.raises(ta.TimelineAudioError):
        ta.track_ids({"audio_tracks": "nope"})


# ---------------------------------------------------------------------------
# peaks：缓存键 / 复用 / 错误透传（不依赖 ffmpeg，打桩 extract_peaks）
# ---------------------------------------------------------------------------

def test_peaks_cache_key_depends_on_size_mtime_buckets(tmp_path: Path):
    asset = _write_wav_silence(tmp_path / "a.wav")
    key = ta.peaks_cache_key(asset, 100)
    assert key == ta.peaks_cache_key(asset, 100)

    assert ta.peaks_cache_key(asset, 200) != key  # 桶数变化
    other = _write_wav_silence(tmp_path / "b.wav")  # 同名不同名 → 不同键
    assert ta.peaks_cache_key(other, 100) != key

    asset.write_bytes(asset.read_bytes() + b"\x00\x00")  # 大小变化
    assert ta.peaks_cache_key(asset, 100) != key


def test_peaks_cache_path_uses_timeline_dir(tmp_path: Path):
    assert ta.peaks_cache_dir(tmp_path) == tmp_path / "timeline" / "peaks"


def test_peaks_for_asset_calls_clip_audio_and_caches(tmp_path: Path, monkeypatch):
    asset = _write_wav_silence(tmp_path / "a.wav")
    calls: list[tuple] = []

    def fake_extract(path, points=ca.MAX_PEAKS):
        calls.append((Path(path), points))
        return [0.1, 0.5, 0.9]

    monkeypatch.setattr(ta.clip_audio, "extract_peaks", fake_extract)
    monkeypatch.setattr(ta.clip_audio, "probe_duration", lambda path: 12.5)

    first = ta.peaks_for_asset(tmp_path, asset, buckets=3, asset_id="BGM1")
    second = ta.peaks_for_asset(tmp_path, asset, buckets=3, asset_id="BGM1")

    assert len(calls) == 1, "第二次必须命中缓存，不再调 ffmpeg"
    assert first["cached"] is False and second["cached"] is True
    assert first["peaks"] == second["peaks"] == [0.1, 0.5, 0.9]
    assert first["peak_count"] == 3
    assert first["max_peaks"] == ca.MAX_PEAKS
    assert first["truncated"] is False
    assert first["duration_s"] == 12.5
    assert first["asset_id"] == "BGM1"


def test_peaks_cache_file_is_json_on_disk(tmp_path: Path, monkeypatch):
    asset = _write_wav_silence(tmp_path / "a.wav")
    monkeypatch.setattr(ta.clip_audio, "extract_peaks", lambda path, points=800: [0.25])
    monkeypatch.setattr(ta.clip_audio, "probe_duration", lambda path: 1.0)
    result = ta.peaks_for_asset(tmp_path, asset, buckets=1)

    cache_file = Path(result["cache_path"])
    assert cache_file.parent == ta.peaks_cache_dir(tmp_path)
    assert cache_file.name == f"{result['cache_key']}.json"
    stored = json.loads(cache_file.read_text(encoding="utf-8"))
    assert stored["document"] == "timeline_peaks"
    assert stored["kind"] == "bgm"  # peaks 载荷里的音轨种类
    assert stored["source"] == "clip_audio.extract_peaks"
    assert stored["peaks"] == [0.25]
    assert stored["asset"]["bytes"] == asset.stat().st_size
    assert [item.name for item in cache_file.parent.iterdir()
            if item.suffix == ".tmp"] == []


def test_peaks_cache_recomputes_when_buckets_change(tmp_path: Path, monkeypatch):
    asset = _write_wav_silence(tmp_path / "a.wav")
    calls: list[int] = []

    def fake_extract(path, points=ca.MAX_PEAKS):
        calls.append(points)
        return [0.5] * points

    monkeypatch.setattr(ta.clip_audio, "extract_peaks", fake_extract)
    monkeypatch.setattr(ta.clip_audio, "probe_duration", lambda path: 1.0)
    ta.peaks_for_asset(tmp_path, asset, buckets=10)
    ta.peaks_for_asset(tmp_path, asset, buckets=20)
    assert calls == [10, 20]


def test_peaks_cache_recomputes_when_file_changes(tmp_path: Path, monkeypatch):
    asset = _write_wav_silence(tmp_path / "a.wav")
    calls: list[Path] = []

    def fake_extract(path, points=ca.MAX_PEAKS):
        calls.append(Path(path))
        return [0.5]

    monkeypatch.setattr(ta.clip_audio, "extract_peaks", fake_extract)
    monkeypatch.setattr(ta.clip_audio, "probe_duration", lambda path: 1.0)
    ta.peaks_for_asset(tmp_path, asset, buckets=1)
    asset.write_bytes(asset.read_bytes() + b"\x01\x00")
    ta.peaks_for_asset(tmp_path, asset, buckets=1)
    assert len(calls) == 2


def test_peaks_cache_corrupt_file_falls_back(tmp_path: Path, monkeypatch):
    asset = _write_wav_silence(tmp_path / "a.wav")
    monkeypatch.setattr(ta.clip_audio, "extract_peaks", lambda path, points=800: [0.4])
    monkeypatch.setattr(ta.clip_audio, "probe_duration", lambda path: 1.0)
    first = ta.peaks_for_asset(tmp_path, asset, buckets=1)
    Path(first["cache_path"]).write_text("{broken", encoding="utf-8")

    again = ta.peaks_for_asset(tmp_path, asset, buckets=1)
    assert again["cached"] is False
    assert again["peaks"] == [0.4]


def test_peaks_cache_rejects_mismatched_key(tmp_path: Path, monkeypatch):
    asset = _write_wav_silence(tmp_path / "a.wav")
    monkeypatch.setattr(ta.clip_audio, "extract_peaks", lambda path, points=800: [0.4])
    monkeypatch.setattr(ta.clip_audio, "probe_duration", lambda path: 1.0)
    result = ta.peaks_for_asset(tmp_path, asset, buckets=1)
    payload = json.loads(Path(result["cache_path"]).read_text(encoding="utf-8"))
    payload["cache_key"] = "deadbeef"
    Path(result["cache_path"]).write_text(json.dumps(payload), encoding="utf-8")
    assert ta.peaks_for_asset(tmp_path, asset, buckets=1)["cached"] is False


def test_peaks_cache_rejects_empty_peaks(tmp_path: Path, monkeypatch):
    """静音/解不出峰值时 extract_peaks 返回 []，此时不写缓存也不炸。"""

    asset = _write_wav_silence(tmp_path / "a.wav")
    monkeypatch.setattr(ta.clip_audio, "extract_peaks", lambda path, points=800: [])
    monkeypatch.setattr(ta.clip_audio, "probe_duration", lambda path: 1.0)
    result = ta.peaks_for_asset(tmp_path, asset, buckets=1)
    assert result["peaks"] == []
    assert result["cached"] is False


def test_peaks_for_asset_clamps_buckets_to_max_peaks(tmp_path: Path, monkeypatch):
    asset = _write_wav_silence(tmp_path / "a.wav")
    calls: list[int] = []
    monkeypatch.setattr(ta.clip_audio, "extract_peaks",
                        lambda path, points=800: calls.append(points) or [0.1])
    monkeypatch.setattr(ta.clip_audio, "probe_duration", lambda path: 1.0)
    result = ta.peaks_for_asset(tmp_path, asset, buckets=5000)
    assert calls == [ca.MAX_PEAKS]
    assert result["buckets"] == ca.MAX_PEAKS


def test_peaks_for_asset_bad_buckets(tmp_path: Path):
    asset = _write_wav_silence(tmp_path / "a.wav")
    with pytest.raises(ta.TimelineAudioError):
        ta.peaks_for_asset(tmp_path, asset, buckets="many")


def test_peaks_for_asset_missing_file(tmp_path: Path):
    with pytest.raises(ta.TimelineAudioError) as excinfo:
        ta.peaks_for_asset(tmp_path, tmp_path / "nope.wav")
    assert excinfo.value.code == ta.E_TIMELINE_AUDIO_ASSET_MISSING


def test_peaks_for_asset_rejects_bad_kind(tmp_path: Path):
    asset = _write_wav_silence(tmp_path / "a.wav")
    with pytest.raises(ta.TimelineAudioError) as excinfo:
        ta.peaks_for_asset(tmp_path, asset, kind="music")
    assert excinfo.value.code == ta.E_TIMELINE_AUDIO_TRACK_INVALID


def test_peaks_for_asset_passes_through_clip_audio_error_code(tmp_path: Path, monkeypatch):
    asset = _write_wav_silence(tmp_path / "a.wav")

    def boom(path, points=800):
        raise ca.ClipAudioError(ca.E_AUDIO_PEAKS_FAILED, "ffmpeg 没了")

    monkeypatch.setattr(ta.clip_audio, "extract_peaks", boom)
    with pytest.raises(ta.TimelineAudioError) as excinfo:
        ta.peaks_for_asset(tmp_path, asset)
    assert excinfo.value.code == ca.E_AUDIO_PEAKS_FAILED  # 既有机器码原样透传
    assert "ffmpeg" in str(excinfo.value)


def test_peaks_payload_shape_matches_clip_audio(tmp_path: Path, monkeypatch):
    """前端载荷字段必须与 ``clip_audio.peaks_payload`` 一致（复用而非重写）。"""

    asset = _write_wav_silence(tmp_path / "a.wav")
    monkeypatch.setattr(ta.clip_audio, "extract_peaks", lambda path, points=800: [0.2, 0.8])
    monkeypatch.setattr(ta.clip_audio, "probe_duration", lambda path: 3.0)
    result = ta.peaks_for_asset(tmp_path, asset, buckets=2, kind="sfx", asset_id="S1")
    reference = ca.peaks_payload({"id": result["id"], "asset_id": "S1", "kind": "bgm",
                                  "duration_s": 3.0, "peaks": [0.2, 0.8]})
    for key in ("id", "length" if False else "peak_count", "max_peaks", "truncated",
                "duration_s", "peaks"):
        assert result[key] == reference[key]
    assert result["kind"] == "sfx"  # 真实种类（适配层改回）


def test_peaks_truncated_flag_from_clip_audio(tmp_path: Path, monkeypatch):
    asset = _write_wav_silence(tmp_path / "a.wav")
    monkeypatch.setattr(ta.clip_audio, "extract_peaks",
                        lambda path, points=800: [0.1] * (ca.MAX_PEAKS + 50))
    monkeypatch.setattr(ta.clip_audio, "probe_duration", lambda path: 1.0)
    result = ta.peaks_for_asset(tmp_path, asset, buckets=ca.MAX_PEAKS)
    assert result["truncated"] is True
    assert result["peak_count"] == ca.MAX_PEAKS


def test_clear_peaks_cache(tmp_path: Path, monkeypatch):
    asset = _write_wav_silence(tmp_path / "a.wav")
    monkeypatch.setattr(ta.clip_audio, "extract_peaks", lambda path, points=800: [0.3])
    monkeypatch.setattr(ta.clip_audio, "probe_duration", lambda path: 1.0)
    ta.peaks_for_asset(tmp_path, asset, buckets=1)
    ta.peaks_for_asset(tmp_path, asset, buckets=2)
    assert ta.clear_peaks_cache(tmp_path) == 2
    assert list(ta.peaks_cache_dir(tmp_path).glob("*.json")) == []
    assert ta.clear_peaks_cache(tmp_path) == 0


def test_asset_durations_from_peaks(tmp_path: Path, monkeypatch):
    asset = _write_wav_silence(tmp_path / "a.wav")
    monkeypatch.setattr(ta.clip_audio, "extract_peaks", lambda path, points=800: [0.3])
    monkeypatch.setattr(ta.clip_audio, "probe_duration", lambda path: 7.0)
    payload = ta.peaks_for_asset(tmp_path, asset, buckets=1, asset_id="BGM1")
    durations = ta.asset_durations_from_peaks(payload)
    assert durations["BGM1"] == 7.0
    assert durations["a.wav"] == 7.0
    assert ta.asset_durations_from_peaks({"duration_s": 0}) == {}
    assert ta.asset_durations_from_peaks({}) == {}


# ---------------------------------------------------------------------------
# 真实 ffmpeg（1 秒正弦）：解码 + 缓存复用
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not FFMPEG, reason="本机没有 ffmpeg，跳过真实峰值解码")
def test_peaks_for_asset_real_ffmpeg_and_cache_reuse(tmp_path: Path):
    asset = _write_sine(tmp_path / "sine.wav", 1.0)
    calls: list[int] = []
    real_extract = ta.clip_audio.extract_peaks

    def counting_extract(path, points=ca.MAX_PEAKS):
        calls.append(points)
        return real_extract(path, points)

    ta.clip_audio.extract_peaks = counting_extract
    try:
        first = ta.peaks_for_asset(tmp_path, asset, buckets=200, asset_id="SINE")
        second = ta.peaks_for_asset(tmp_path, asset, buckets=200, asset_id="SINE")
    finally:
        ta.clip_audio.extract_peaks = real_extract

    assert first["cached"] is False and second["cached"] is True
    assert calls == [200]
    assert first["peak_count"] == 200
    assert len(first["peaks"]) == 200
    assert all(0.0 <= value <= 1.0 for value in first["peaks"])
    assert max(first["peaks"]) > 0.05  # 真的解出了正弦（不是全零）
    assert first["duration_s"] == pytest.approx(1.0, abs=0.05)
    assert first["peaks"] == second["peaks"]


@pytest.mark.skipif(not FFMPEG, reason="本机没有 ffmpeg，跳过真实峰值解码")
def test_peaks_for_asset_real_ffmpeg_voice_kind(tmp_path: Path):
    asset = _write_sine(tmp_path / "sine.wav", 0.5)
    result = ta.peaks_for_asset(tmp_path, asset, buckets=50, kind="voice")
    assert result["kind"] == "voice"
    assert result["peak_count"] == 50
