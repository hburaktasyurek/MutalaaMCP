"""Per-user OS-held lock for a single ``mutalaamcp serve`` process."""

from __future__ import annotations

import errno
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, Self

from mutalaamcp.domain.errors import ErrorBody, ErrorCode, ErrorEnvelope

if TYPE_CHECKING:

    class _MsvcrtModule(Protocol):
        LK_NBLCK: int
        LK_UNLCK: int

        def locking(self, fd: int, mode: int, nbytes: int) -> None: ...

    class _FcntlModule(Protocol):
        LOCK_EX: int
        LOCK_NB: int
        LOCK_UN: int

        def flock(self, fd: int, operation: int) -> None: ...

    msvcrt: _MsvcrtModule
    fcntl: _FcntlModule
elif os.name == "nt":
    import msvcrt
else:
    import fcntl


_ALREADY_RUNNING_MESSAGE = (
    "Bu kullanıcı için bir MutalaaMCP serve işlemi zaten çalışıyor."
)


class AlreadyRunning(RuntimeError):
    """Raised when the per-user serve lock is held by another process."""

    code = ErrorCode.ALREADY_RUNNING

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        super().__init__(_ALREADY_RUNNING_MESSAGE)

    def envelope(self) -> ErrorEnvelope:
        return ErrorEnvelope(
            error=ErrorBody(
                code=ErrorCode.ALREADY_RUNNING,
                message=_ALREADY_RUNNING_MESSAGE,
                retryable=False,
            )
        )


class FileLock:
    """Exclusive lock released when this object, or the process, closes the fd.

    ``acquire`` is nonblocking. A collision maps to :class:`AlreadyRunning`.
    ``serve`` and mutation commands share this lock; mutations hold it for the
    entire operation via :func:`mutation_lock`.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    def locked(self) -> bool:
        return self._fd is not None

    def acquire(self) -> Self:
        if self._fd is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            _lock_exclusive_nonblocking(fd)
        except OSError as exc:
            os.close(fd)
            if _is_lock_collision(exc):
                raise AlreadyRunning(self.path) from exc
            raise
        self._fd = fd
        return self

    def release(self) -> None:
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            _unlock(fd)
        finally:
            os.close(fd)

    def __enter__(self) -> Self:
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()


def serve_lock_held(path: str | Path) -> bool:
    """Return True if any process currently holds the exclusive serve lock."""
    path = Path(path)
    if not path.exists():
        return False
    probe = FileLock(path)
    try:
        probe.acquire()
    except AlreadyRunning:
        return True
    probe.release()
    return False


@contextmanager
def mutation_lock(path: str | Path) -> Iterator[FileLock]:
    """Hold the per-user serve lock for one cache, update, or OCR mutation.

    Acquires the same exclusive :class:`FileLock` used by ``serve`` and keeps
    it until the caller's mutation (and any rollback) finishes. A collision
    raises :class:`AlreadyRunning`.
    """
    lock = FileLock(path)
    lock.acquire()
    try:
        yield lock
    finally:
        lock.release()


def _lock_exclusive_nonblocking(fd: int) -> None:
    if os.name == "nt":
        os.lseek(fd, 0, os.SEEK_SET)
        if os.fstat(fd).st_size < 1:
            os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        return
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    if os.name == "nt":
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    fcntl.flock(fd, fcntl.LOCK_UN)


def _is_lock_collision(exc: OSError) -> bool:
    if isinstance(exc, BlockingIOError):
        return True
    return exc.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK}
