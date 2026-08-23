"""Exclusive lock on the pipeline's output file (OS-level file locking).

Two concurrent runs (GUI + CLI, or two CLIs) pointed at the same
``output_path`` would otherwise interleave ``-y`` writes into the same
file and silently corrupt each other. The lock is an OS-level file
lock on a small sibling file (``<output>.lock``):

  * POSIX: ``fcntl.flock(fd, LOCK_EX | LOCK_NB)`` — advisory, tied to
    the locked inode;
  * Windows: ``msvcrt.locking(fd, LK_NBLCK, 1)`` — a byte-range lock
    on the first byte of the file.

Why OS locks instead of the previous ``O_EXCL`` + pid-liveness scheme
(audit round 24 P1): the kernel releases the lock automatically when
the owner dies (crash, ``kill -9``, BSOD, power loss), so no stale
lock can ever outlive its owner. The heuristic stack that could steal
a *live* lock disappears:

  * the pid-liveness probe (a dead-pid judgment could reclaim a
    genuine concurrent run's lock; a reused pid could keep a stale
    lock "alive" forever);
  * the quarantine reclaim (``os.rename(lock, quarantine)`` moved a
    pathname that could already belong to another run — between the
    rename and the restore a third process could take the free name
    with ``O_EXCL``, and the moved owner's lock no longer protected
    anything: the exact double-write the lock exists to prevent);
  * the 30 s acquire grace (a crashed owner's lock file looked like an
    in-progress acquire forever until its mtime aged out).

A leftover lock FILE is harmless: the OS lock is gone with the owner,
and the next acquirer takes the file immediately. The file content is
a diagnostic owner record (token + pid + resource) — no locking
decision ever reads it, so a corrupt or hand-edited record cannot
break acquire/release.

Race safety:

  * Acquire verifies the locked file is the file the path currently
    names (``stat(path)`` vs ``fstat(fd)`` — inode on POSIX, file
    index on Windows). Without the check, a waiter that opened the
    file just before a release unlinked it would lock an orphaned
    inode while a newcomer locks a fresh file at the same path — two
    "holders" of one lock.
  * Release unlinks the file BEFORE unlocking on POSIX, so no waiter
    can slip into the unlock→unlink gap and end up holding a lock on
    a file that is about to vanish (the acquire-side verification
    makes such a waiter re-open and see the real lock anyway).
  * Windows cannot unlink an open file: release unlocks, closes, then
    removes the file best-effort. The only residual is a fast
    retrying acquirer locking the file in the microseconds between
    our close and our unlink — that leaves an orphaned lock FILE
    behind, which the next acquirer takes immediately (the OS lock
    died with the owner); the scheme self-heals.
"""

import contextlib
import errno
import logging
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from stream2video.concat.errors import ConcatError

logger = logging.getLogger(__name__)


class ConcatLockError(ConcatError):
    """Raised when the lock for this output is held by another run."""


class LockCancelledError(ConcatError):
    """Raised when the caller cancels while waiting for a held lock.

    Distinct from ``ConcatLockError`` so hosts can map a user-cancelled
    wait to their cancellation exception instead of a lock failure.
    """


def _is_contention_error(e: OSError) -> bool:
    """True when ``e`` means "another process holds the OS lock".

    Only the platform's contention codes are retryable: flock reports
    EAGAIN/EWOULDBLOCK (and EACCES on some platforms), msvcrt.locking
    reports winerror 33 (ERROR_LOCK_VIOLATION). Anything else (EBADF,
    ENOLCK, an unsupported filesystem, a vanished file...) is a real
    fault that must surface immediately, not masquerade as "another
    run is holding the lock" until the timeout (audit round 25 P6).
    """
    if os.name == "nt":
        if getattr(e, "winerror", None) == 33:
            return True
        return e.errno in (errno.EACCES, errno.EAGAIN)
    return e.errno in (errno.EACCES, errno.EAGAIN)


def lock_path_for(output_path: Path) -> Path:
    return output_path.with_name(output_path.name + ".lock")


# Retry cadence while waiting for a concurrent acquirer to finish
# opening/locking the file (the OS lock is held from BEFORE the owner
# record is written, so contention at this granularity is a matter of
# milliseconds — the same window the old "no pid yet" grace covered).
_RETRY_SLEEP_SECONDS = 0.05
_DEFAULT_TIMEOUT_SECONDS = 60.0


@dataclass
class LockHandle:
    """An acquired OS lock, handed to :func:`release_output_lock`.

    ``token`` is diagnostic only (written into the lock file so a
    leftover file says what run created it) — ownership is the OS lock
    on ``fd``, which the kernel drops when the process dies. ``fd`` is
    ``-1`` once the handle has been released.
    """

    path: Path
    token: str
    # Human-readable resource the lock protects, for refusal messages.
    what: str = "output"
    fd: int = -1


def _os_lock_fd(fd: int) -> None:
    """Take the non-blocking OS lock on an open fd."""
    if os.name == "nt":
        # typeshed has no msvcrt stubs on POSIX and no fcntl stubs on
        # Windows, so neither module can be imported statically without
        # a platform-dependent ignore; importlib yields Any on every
        # platform and the platform guard keeps the branch unreachable
        # where the module does not exist.
        import importlib

        msvcrt = importlib.import_module("msvcrt")
        # msvcrt.locking locks bytes relative to the CURRENT position
        # and fails beyond the end of the file — make sure byte 0
        # exists before locking it. A failure here is a disk problem,
        # NOT contention (nothing is locked yet): surface it as a
        # lock-preparation error, not a retryable EWOULDBLOCK.
        if os.fstat(fd).st_size < 1:
            try:
                os.write(fd, b"\0")
            except OSError as e:
                # A LOCK-VIOLATION here is contention, not a preparation
                # fault: the byte-0 holder had locked the region but not
                # yet written its owner record (file still empty), so our
                # placeholder write hit their lock. Re-raise the raw
                # OSError so the acquire loop treats it as an ordinary
                # contention retry — wrapping it into ConcatLockError
                # made the handler UNLINK the live owner's lock file
                # (the exact two-owners scenario this module documents
                # as unacceptable) and misreport the pause as "could not
                # be prepared" instead of waiting.
                if _is_contention_error(e):
                    raise
                raise ConcatLockError(f"Lock file could not be prepared ({e})") from None
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import importlib

        fcntl = importlib.import_module("fcntl")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _os_unlock_fd(fd: int) -> None:
    """Drop the OS lock on an open fd."""
    if os.name == "nt":
        import importlib

        msvcrt = importlib.import_module("msvcrt")
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import importlib

        fcntl = importlib.import_module("fcntl")
        fcntl.flock(fd, fcntl.LOCK_UN)


def _same_file(fd: int, path: Path) -> bool:
    """True when ``path`` still names the same file as ``fd``.

    Compares device + file index (inode on POSIX; the file index on
    Windows, which Python's stat exposes). Uses ``os.stat``/``os.fstat``
    (not ``Path.stat()``) so the check survives hosts that monkeypatch
    ``Path`` methods. A platform that reports no index (``st_ino == 0``)
    cannot verify identity — assume same.
    """
    try:
        p = os.stat(path)
        f = os.fstat(fd)
    except OSError:
        return False
    if not p.st_ino or not f.st_ino:
        return True
    return (p.st_dev, p.st_ino) == (f.st_dev, f.st_ino)


def _acquire_lock(
    lock_path: Path,
    *,
    what: str,
    token: str,
    output_hint: str,
    timeout: float,
    on_wait: Callable[[], None] | None = None,
    cancel_callback: Callable[[], bool] | None = None,
) -> LockHandle:
    """Shared acquire: OS-lock ``lock_path``, retrying until ``timeout``.

    ``what`` / ``output_hint`` shape the refusal message (the hint
    names the resource the lock protects). The owner record is written
    AFTER the OS lock is held, so a concurrent acquirer can never
    observe a locked file without its record — and never needs one:
    the OS decides liveness, not the content. ``on_wait`` fires once
    when the first contention is observed (hosts log "waiting for
    another run" through it). ``cancel_callback`` is polled between
    retries — when it turns true the wait aborts with
    :class:`LockCancelledError` instead of spinning until the timeout
    (audit round 25 P5: a Cancel / GUI close must stop the worker even
    while it is waiting for a same-project run to finish).
    """
    # os.makedirs (not Path.mkdir): hosts that monkeypatch ``Path``
    # methods for testing would break the plain create call.
    os.makedirs(lock_path.parent, exist_ok=True)
    deadline = time.monotonic() + timeout
    warned = False
    while True:
        # A cancel must stop the acquire BEFORE it opens/creates the
        # lock file too (audit round 27 P11): the old contract only
        # polled between contention retries, so an already-cancelled
        # run could still create the lock file and write its owner
        # record before noticing the cancel at the next phase.
        if cancel_callback is not None and cancel_callback():
            raise LockCancelledError(
                f"Cancelled before acquiring the {what} lock ({output_hint})"
            ) from None
        try:
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o666)
        except OSError as e:
            raise ConcatLockError(f"Could not open lock {lock_path}: {e}") from None
        try:
            _os_lock_fd(fd)
        except ConcatLockError:
            # Lock-preparation failure (Windows placeholder write): not
            # contention — clean up and surface it truthfully.
            with contextlib.suppress(OSError):
                os.close(fd)
            with contextlib.suppress(OSError):
                os.unlink(lock_path)
            raise
        except OSError as e:
            os.close(fd)
            if not _is_contention_error(e):
                # Not "someone else holds the lock" — a real fault (bad
                # fd, filesystem without lock support, ...). It will not
                # heal by retrying; say what it actually was instead of
                # blaming a phantom concurrent run after the timeout
                # (audit round 25 P6).
                raise ConcatLockError(
                    f"Lock {lock_path} could not be locked ({e}) — refusing to run"
                ) from None
            if not warned and on_wait is not None:
                warned = True
                try:
                    on_wait()
                except Exception:
                    logger.debug("on_wait raised", exc_info=True)
            if cancel_callback is not None and cancel_callback():
                raise LockCancelledError(
                    f"Cancelled while waiting for the {what} lock ({output_hint})"
                ) from None
            if time.monotonic() >= deadline:
                raise ConcatLockError(
                    f"Another stream2video run holds the {what} lock ({output_hint}). "
                    f"Refusing to run after {timeout:.0f}s of waiting. A crashed run "
                    "cannot leave this lock behind (the OS releases it automatically) — "
                    "if the other run is truly gone, delete the lock file and retry."
                ) from None
            time.sleep(_RETRY_SLEEP_SECONDS)
            continue
        if not _same_file(fd, lock_path):
            # The path was unlinked + recreated between our open and
            # our lock: the fd locks an orphaned inode that protects
            # nothing. Close (which auto-releases) and retry on the
            # current file — bounded by the same deadline so a churning
            # path can never hang the acquire.
            os.close(fd)
            if cancel_callback is not None and cancel_callback():
                raise LockCancelledError(
                    f"Cancelled while waiting for the {what} lock ({output_hint})"
                ) from None
            if time.monotonic() >= deadline:
                raise ConcatLockError(
                    f"Lock {lock_path} kept changing identity while acquiring "
                    f"({output_hint}) — refusing to run."
                ) from None
            time.sleep(_RETRY_SLEEP_SECONDS)
            continue
        # We hold the OS lock. A cancel that arrived during the open/
        # lock window must stop the acquire BEFORE the owner record is
        # published (audit round 27 P11): release exactly like
        # release_output_lock (platform-correct order) and raise — the
        # file we remove is our own fresh acquisition, so no live
        # owner is ever severed.
        if cancel_callback is not None and cancel_callback():
            release_output_lock(LockHandle(path=lock_path, token=token, what=what, fd=fd))
            raise LockCancelledError(
                f"Cancelled before publishing the {what} lock ({output_hint})"
            ) from None
        break

    try:
        os.lseek(fd, 0, os.SEEK_SET)
        payload = f"token={token} pid={os.getpid()} {output_hint}\n".encode("utf-8", "replace")
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("lock record write stalled")
            view = view[written:]
        # Trim a longer stale record from a previous owner.
        os.ftruncate(fd, os.lseek(fd, 0, os.SEEK_CUR))
    except OSError as e:
        # Audit round 25 P1: between our unlock/close and the unlink a
        # waiter can take the SAME file and write its own valid record;
        # deleting the pathname then severs the new owner's protection
        # (on POSIX a third process would open a fresh file and both
        # would "hold" the lock). Remove our own inode FIRST on POSIX
        # (exactly like release) so the waiter can only ever take a
        # fresh file; on Windows an open file cannot be unlinked, so a
        # new owner's lock makes the unlink fail harmlessly.
        if os.name == "nt":
            with contextlib.suppress(OSError):
                _os_unlock_fd(fd)
            with contextlib.suppress(OSError):
                os.close(fd)
            with contextlib.suppress(OSError):
                os.unlink(lock_path)
        else:
            if _same_file(fd, lock_path):
                with contextlib.suppress(OSError):
                    os.unlink(lock_path)
            with contextlib.suppress(OSError):
                _os_unlock_fd(fd)
            with contextlib.suppress(OSError):
                os.close(fd)
        raise ConcatLockError(
            f"Lock {lock_path} could not be written ({e}) — refusing to run"
        ) from None

    logger.debug(f"Acquired lock {lock_path}")
    return LockHandle(path=lock_path, token=token, what=what, fd=fd)


def acquire_output_lock(
    output_path: Path,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    cancel_callback: Callable[[], bool] | None = None,
) -> LockHandle:
    """Exclusively lock ``<output>.lock``; raise if another run holds it.

    Returns a :class:`LockHandle` for :func:`release_output_lock`. The
    OS lock is held from before the owner record is written, so a
    second acquirer can never judge a locked-but-record-less file
    "stale" — the OS decides liveness, not content.
    """
    return _acquire_lock(
        lock_path_for(output_path),
        what="output",
        token=secrets.token_hex(16),
        output_hint=f"output={output_path}",
        timeout=timeout,
        cancel_callback=cancel_callback,
    )


def acquire_lock_file(
    lock_path: Path,
    what: str,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    on_wait: Callable[[], None] | None = None,
    cancel_callback: Callable[[], bool] | None = None,
) -> LockHandle:
    """Low-level: exclusively lock an arbitrary lock file.

    Used for the pipeline's project locks (per-URL / per-source
    identity), which must be held from BEFORE the download/move/cache
    phases — the output lock alone is taken too late to stop two runs
    of the same URL from clobbering each other's project dir (audit
    round 24 P4). ``what`` names the protected resource in the
    refusal message; ``on_wait`` fires once when another run holds the
    lock (hosts log a "waiting" note through it); ``cancel_callback``
    aborts the wait with :class:`LockCancelledError` (audit round
    25 P5).
    """
    return _acquire_lock(
        lock_path,
        what=what,
        token=secrets.token_hex(16),
        output_hint=f"resource={lock_path}",
        timeout=timeout,
        on_wait=on_wait,
        cancel_callback=cancel_callback,
    )


def _release_stale_pathname(path: Path) -> None:
    """Release a bare-pathname lock file IF (and only if) it is stale.

    The historical ``release_output_lock(Path)`` contract unlinked the
    pathname unconditionally — a live owner's lock file could be
    deleted out from under it (audit round 26 P1): its OS lock lives
    on the now-orphaned inode and a newcomer locks a fresh file at the
    same path, so two processes believe they own the resource and the
    whole identity verification in acquire is bypassed. The fix: try
    to take the OS lock OURSELVES first. Success proves no live owner
    exists (the kernel released it with the crashed owner) and the
    file is ours to remove; contention proves a live owner exists and
    the pathname must survive.
    """
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return
    try:
        _os_lock_fd(fd)
    except ConcatLockError as e:
        # Lock-preparation failure (Windows placeholder write): a disk
        # or filesystem defect, NOT proof of a live owner (audit round
        # 27 P12) — report it truthfully.
        logger.warning("Pathname release of %s failed (lock fault): %s", path, e)
        with contextlib.suppress(OSError):
            os.close(fd)
        return
    except OSError as e:
        if _is_contention_error(e):
            logger.warning("Refusing pathname release of %s: a live owner holds the lock", path)
        else:
            logger.warning("Pathname release of %s failed (lock fault): %s", path, e)
        with contextlib.suppress(OSError):
            os.close(fd)
        return
    # We hold the OS lock: the file was a stale leftover. Hand it to
    # the normal handle release (platform-correct unlock/close/unlink
    # with the same-file identity check).
    release_output_lock(LockHandle(path=path, token="legacy", what="pathname", fd=fd))


def release_output_lock(handle: LockHandle | Path) -> None:
    """Best-effort release of the lock; never raises.

    Unlocks, closes the fd and removes the lock file. The file removal
    order differs per platform (see the module docstring): POSIX
    unlinks BEFORE unlocking so a waiter can never land on a dying
    file; Windows closes first (an open file cannot be unlinked) and
    the residual race self-heals.

    A bare :class:`Path` (legacy callers) is NOT removed
    unconditionally (audit round 26 P1): the pathname may name a LIVE
    lock held by another process, and unlinking it would sever that
    owner's protection (its OS lock stays on the orphaned inode while
    a third process takes the freed name — two "owners" of one lock).
    The pathname is released only after THIS process successfully
    takes the OS lock itself, which proves the file is a stale
    leftover from a crashed run.
    """
    if isinstance(handle, Path):
        _release_stale_pathname(handle)
        return
    if handle.fd < 0:
        return  # already released
    fd, path = handle.fd, handle.path
    handle.fd = -1
    if os.name == "nt":
        with contextlib.suppress(OSError):
            _os_unlock_fd(fd)
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(path)
    else:
        # Unlink BEFORE unlock: while the path still names our inode,
        # a waiter on the path sees our lock until we drop it — it
        # cannot land in the unlock→unlink gap.
        if _same_file(fd, path):
            with contextlib.suppress(OSError):
                os.unlink(path)
        with contextlib.suppress(OSError):
            _os_unlock_fd(fd)
        with contextlib.suppress(OSError):
            os.close(fd)
    logger.debug(f"Released lock {path}")
