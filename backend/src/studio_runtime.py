"""Bounded file copies and process locks for the shared Studio workspace."""
from contextlib import contextmanager
from pathlib import Path
import fcntl
import os
import shutil
import tempfile


def copy_file(source: Path, target: Path) -> None:
    """Publish a complete copy atomically without buffering the media in RAM."""
    source, target = Path(source), Path(target)
    if source.resolve() == target.resolve():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as incoming:
            shutil.copyfileobj(incoming, output, length=1024 * 1024)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def process_lock(path: Path, *, blocking: bool = True):
    """Kernel releases this lock even on SIGKILL; never unlink the lock file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        fcntl.flock(handle, flags)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def terminate_with_parent(expected_parent: int) -> None:
    """Kill this isolated process group if its worker parent disappears."""
    import ctypes
    import signal

    if os.getpgrp() != os.getpid():
        raise RuntimeError("Job must have its own process group")

    def terminate_group(*_):
        os.killpg(os.getpgrp(), signal.SIGKILL)

    signal.signal(signal.SIGTERM, terminate_group)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        raise OSError(ctypes.get_errno(), "Cannot bind job to worker lifetime")
    # Parent may have died between Popen and prctl.
    if os.getppid() != expected_parent:
        terminate_group()
