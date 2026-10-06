"""Safe file primitives: atomic writes, per-file locks, restrictive permissions.

Locks are held on a sibling ``<name>.lock`` file with ``fcntl.flock`` so that the
server and the CLI (separate processes) never interleave writes, plus an
in-process lock because flock is per open file description.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path

PRIVATE_FILE_MODE = 0o600
PRIVATE_DIR_MODE = 0o700

_thread_locks: dict[Path, threading.RLock] = {}
_registry_lock = threading.Lock()


def _thread_lock(path: Path) -> threading.RLock:
    with _registry_lock:
        return _thread_locks.setdefault(path, threading.RLock())


def ensure_private_dir(path: Path) -> None:
    """Create ``path`` (and parents) with mode 0700 for the leaf directory."""
    path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    os.chmod(path, PRIVATE_DIR_MODE)


@contextlib.contextmanager
def locked(path: Path) -> Iterator[None]:
    """Exclusive lock for reading-modifying-writing ``path`` (threads and processes)."""
    path = path.resolve()
    with _thread_lock(path):
        lock_path = path.with_name(path.name + ".lock")
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, PRIVATE_FILE_MODE)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def atomic_write(path: Path, data: str | bytes, mode: int = PRIVATE_FILE_MODE) -> None:
    """Write via temp file + fsync + ``os.replace``; the file never appears half-written."""
    payload = data.encode("utf-8") if isinstance(data, str) else data
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
