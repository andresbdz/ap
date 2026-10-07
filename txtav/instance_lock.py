"""One txtav process at a time may use the portal session.

Two would double the request rate that was agreed, and nothing in the portal's
reply would tell either of them. Chromium's own profile lock is not enough: on
2026-10-07 a second headless launch on the same profile succeeded outright,
and a headed one is refused only while a browser is actually open. The
supervisor closes its browser while idle and every few hours to replace it, so
a second process started in one of those gaps would get in.

The lock is an OS file lock held for the life of the process. The operating
system releases it when the process exits, however it exits, so a crash never
leaves a stale lock for a person to find and delete.
"""

from __future__ import annotations

import os
from pathlib import Path


class AlreadyRunning(RuntimeError):
    pass


class InstanceLock:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._handle = None

    def acquire(self) -> None:
        """Take the lock or raise AlreadyRunning. Never waits."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+")  # noqa: SIM115 - held until release()
        try:
            _lock(handle)
        except OSError as exc:
            handle.close()
            raise AlreadyRunning(
                f"another txtav process holds {self.path} and is using the portal "
                f"session. Two at once would double the agreed request rate."
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            _unlock(self._handle)
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "InstanceLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


if os.name == "nt":  # pragma: no cover - exercised on Windows only
    import msvcrt

    def _lock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
