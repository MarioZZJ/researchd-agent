"""Exclusive write lock for the researchd data directory.

Enforces the "only `researchd service` writes the database" invariant across
processes: the service holds the lock for its lifetime; `researchd migrate`
must acquire it (and fails with a clear message if the service is running);
`researchctl doctor` opens the database read-only (mode=ro) and never writes.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path


class DataDirLockedError(Exception):
    pass


class DataDirLock:
    def __init__(self, data_dir: str | Path):
        self.path = Path(data_dir) / "researchd.lock"
        self._fd: int | None = None

    def acquire(self, *, block: bool = False) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | (0 if block else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            os.close(self._fd)
            self._fd = None
            raise DataDirLockedError(
                f"data dir {self.path.parent} is locked by another researchd process "
                "(service or migrate); stop it first"
            ) from exc

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "DataDirLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:  # noqa: ANN002
        self.release()


def is_locked(data_dir: str | Path) -> bool:
    lock = DataDirLock(data_dir)
    try:
        lock.acquire()
    except DataDirLockedError:
        return True
    lock.release()
    return False
