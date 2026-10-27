"""成片与侧车的唯一目录选择：staged runs 与 legacy 共用，不跨 clip 回退。"""
from __future__ import annotations

from pathlib import Path
import re

TASK_ID = re.compile(r"^(?:autoedit_[0-9a-f]{12}|[0-9a-f]{32})$")
CLIP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def output_root(tasks_root: Path, task_id: str) -> Path:
    if not TASK_ID.fullmatch(str(task_id or '')):
        raise ValueError('Invalid render task id')
    root = Path(tasks_root).resolve()
    path = root / 'output' / task_id
    if path.resolve() != path or not path.resolve().is_relative_to(root):
        raise ValueError('Unsafe render task path')
    return path


def clip_dir(tasks_root: Path, task_id: str, clip_id: str, *, require_video: bool = False) -> Path:
    if not CLIP_ID.fullmatch(str(clip_id or '')):
        raise ValueError('Invalid clip id')
    run = output_root(tasks_root, task_id)
    # staged 布局一旦存在就作为本 run 的权威布局；缺片不能退回同名旧文件。
    staged = run / 'clips'
    if staged.is_symlink():
        raise ValueError('Unsafe staged clip root')
    path = (staged if staged.exists() else run) / clip_id
    if path.resolve() != path or not path.resolve().is_relative_to(run):
        raise ValueError('Unsafe clip directory')
    video = path / 'clip_final.mp4'
    if video.is_symlink():
        raise ValueError('Unsafe clip video')
    if require_video and not video.is_file():
        raise FileNotFoundError('Artifact not found')
    return path


def clip_video_path(tasks_root: Path, task_id: str, clip_id: str) -> Path:
    return clip_dir(tasks_root, task_id, clip_id, require_video=True) / 'clip_final.mp4'


def clip_asset(tasks_root: Path, task_id: str, clip_id: str, relative: str, *, required: bool = True) -> Path:
    asset = Path(relative)
    if not relative or asset.is_absolute() or '..' in asset.parts:
        raise ValueError('Invalid clip asset path')
    directory = clip_dir(tasks_root, task_id, clip_id)
    path = directory / asset
    if path.resolve() != path or not path.resolve().is_relative_to(directory):
        raise ValueError('Unsafe clip asset path')
    if required and not path.is_file():
        raise FileNotFoundError('Artifact not found')
    return path
