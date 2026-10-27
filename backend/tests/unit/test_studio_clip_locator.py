"""隔离 staged/legacy 定位、侧车和路径失败场景；不运行真实渲染。"""
import os
from pathlib import Path
import pytest
from src import studio_clip_locator as locator
from src import studio_auto_edit_bridge as bridge
from src import studio_local_tasks as local
from src.studio_transform_export import clip_video_path, _cache_key

LEGACY = 'autoedit_aaaabbbbcccc'
STAGED = 'a' * 32
CLIP = 'clip_01'


@pytest.fixture
def runs(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge,'TASKS_ROOT',tmp_path)
    monkeypatch.setattr(local,'TASKS_ROOT',tmp_path)
    for run, staged in [(LEGACY,False),(STAGED,True)]:
        directory=tmp_path/'output'/run
        if staged:directory=directory/'clips'
        directory=directory/CLIP
        directory.mkdir(parents=True)
        (directory/'clip_final.mp4').write_bytes(run.encode())
        (directory/'subtitles.srt').write_text('fixture',encoding='utf-8')
    return tmp_path


@pytest.mark.parametrize('run',[LEGACY,STAGED])
def test_clip_and_sidecars_share_directory(runs,run):
    video=clip_video_path(run,CLIP)
    assert video.read_bytes()==run.encode()
    assert locator.clip_asset(runs,run,CLIP,'subtitles.srt').parent==video.parent
    assert bridge.auto_edit_clip_dir(run,CLIP)==video.parent
    assert bridge.resolve_auto_edit_artifact(run,f'output/{run}/{CLIP}/clip_final.mp4')==video
    assert bridge.resolve_auto_edit_artifact(run,f'output/{run}/clips/{CLIP}/subtitles.srt').parent==video.parent


def test_staged_transform_effects_and_local_tasks(runs,monkeypatch):
    bridge.save_clip_transform(STAGED,CLIP,{'scale':1.5})
    assert bridge.read_clip_transform(STAGED,CLIP)['scale']==1.5
    bridge.save_clip_render_settings(STAGED,CLIP,{'voice_volume':0.5})
    assert bridge.read_clip_render_settings(STAGED,CLIP)['voice_volume']==0.5
    monkeypatch.setattr(local.store,'entry_for',lambda *_:{'render_task_id':STAGED})
    _,directory,run=local._clip_paths({'project_id':'timeline_fixture','clip_id':CLIP})
    assert directory==locator.clip_dir(runs,STAGED,CLIP)
    assert run==STAGED
    assert '/clips/' in local._cover_path({},STAGED,CLIP)


def test_authoritative_staged_missing_does_not_fall_back(runs):
    canonical=locator.clip_video_path(runs,STAGED,CLIP)
    canonical.unlink()
    old=runs/'output'/STAGED/CLIP
    old.mkdir()
    (old/'clip_final.mp4').write_bytes(b'stale')
    with pytest.raises(FileNotFoundError):clip_video_path(STAGED,CLIP)
    with pytest.raises(FileNotFoundError):bridge.resolve_auto_edit_artifact(STAGED,f'{CLIP}/clip_final.mp4')


@pytest.mark.parametrize('run,clip,asset',[(LEGACY,'../other','clip_final.mp4'),('../other',CLIP,'clip_final.mp4'),(STAGED,CLIP,'../other/clip_final.mp4'),(STAGED,CLIP,'/etc/passwd')])
def test_traversal_rejected(runs,run,clip,asset):
    with pytest.raises(ValueError):locator.clip_asset(runs,run,clip,asset)


def test_cross_clip_symlink_rejected(runs):
    directory=locator.clip_dir(runs,STAGED,CLIP)
    (directory/'bad.srt').symlink_to(runs/'output'/LEGACY/CLIP/'subtitles.srt')
    with pytest.raises(ValueError):locator.clip_asset(runs,STAGED,CLIP,'bad.srt')
    (directory/'clip_final.mp4').unlink()
    (directory/'clip_final.mp4').symlink_to(runs/'output'/LEGACY/CLIP/'clip_final.mp4')
    with pytest.raises(ValueError):clip_video_path(STAGED,CLIP)


def test_cache_uses_source_bytes_not_size_mtime(runs):
    video=clip_video_path(STAGED,CLIP)
    key=_cache_key({},'blur',video)
    previous=video.stat()
    video.write_bytes(b'b'*previous.st_size)
    os.utime(video,ns=(previous.st_atime_ns,previous.st_mtime_ns))
    assert _cache_key({},'blur',video)!=key


def test_cache_uses_bgm_bytes(runs):
    video=clip_video_path(STAGED,CLIP)
    audio=video.parent/'bgm.wav'
    audio.write_bytes(b'first')
    settings={'audio_tracks':[{'kind':'bgm','path':'bgm.wav'}]}
    key=_cache_key({},'blur',video,settings)
    audio.write_bytes(b'other')
    assert _cache_key({},'blur',video,settings)!=key


def test_export_capacity_rejection_preserves_files(runs,tmp_path,monkeypatch):
    from src import capacity_guard as capacity, studio_transform_export as export
    monkeypatch.setattr(export,'_probe_media',lambda _:{'width':640,'height':360,'duration':3.,'has_audio':False})
    monkeypatch.setattr(capacity,'free_bytes',lambda _:0)
    video=clip_video_path(STAGED,CLIP)
    before=video.read_bytes()
    cache=tmp_path/'unused_cache'
    with pytest.raises(capacity.CapacityInsufficientError):
        export.export_transformed_clip(task_id=STAGED,clip_id=CLIP,transform={'scale':1.1},cache_root=cache)
    assert not cache.exists()
    assert video.read_bytes()==before


def test_legacy_task_write_validation_not_relaxed(runs):
    with pytest.raises(ValueError):bridge._task_paths(STAGED)
    with pytest.raises(FileNotFoundError):bridge.resolve_auto_edit_artifact(STAGED,f'output/{LEGACY}/{CLIP}/clip_final.mp4')
