"""Exclusive lock on the pipeline's output file.

Two concurrent runs (GUI + CLI, or two CLIs) pointed at the same
``output_path`` would otherwise interleave ``-y`` writes into the same
file and silently corrupt each other. The lock is a small
sibling file created with ``O_CREAT | O_EXCL`` — atomic on every
filesystem, no third-party dependency, and it self-describes: while
``out.mp4.lock`` exists, a second run fails fast with a clear message
instead of producing a half-overwritten video.

Ownership model (fixes the acquire race where a second process could
read the lock file in the gap between ``os.open`` and the pid write,
judge it stale and delete a live lock):

* Every lock records an unique owner token plus the pid. The token is
  generated BEFORE the lock file exists, so it is written in the same
  breath as the pid.
* A lock whose content is unreadable (no pid/token yet) is presumed
  *mid-acquire* while its mtime is fresh and the caller simply retries
  — it is never reclaimed during that grace window.
* Only a lock whose pid line is missing AND whose mtime is older than
  the grace window is reclaimed (crashed between create and write).
* :func:`release_output_lock` removes the lock only when the stored
  token still matches the caller's — a lock that was reclaimed and
  re-taken by another run is never deleted out from under it.
"""

import logging
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from stream2video.concat.errors import ConcatError

logger = logging.getLogger(__name__)


class ConcatLockError(ConcatError):
    """Raised when another run already holds the lock for this output."""


def lock_path_for(output_path: Path) -> Path:
    return output_path.with_name(output_path.name + ".lock")


# A lock whose content is missing/unreadable is given this long to gain
# a pid line (the acquire writes it within milliseconds; the grace
# absorbs scheduler stalls / slow filesystems) before it may be
# reclaimed as a crashed-acquire.
_ACQUIRE_GRACE_SECONDS = 30.0
# Retry cadence while waiting for a concurrent acquirer to finish
# writing its pid line.
_RETRY_SLEEP_SECONDS = 0.05
_RETRY_MAX_TRIES = int(_ACQUIRE_GRACE_SECONDS / _RETRY_SLEEP_SECONDS)


@dataclass(frozen=True)
class LockHandle:
    """The token an acquired lock is released with.

    ``token`` is generated before the lock file is created, so the
    owner can prove the file on disk is still its own when releasing —
    a lock that was reclaimed and re-taken by another run is left
    alone.
    """

    path: Path
    token: str


def _lock_text_from_content(lock_path: Path) -> str:
    """Read the lock file's diagnostic text (empty string on any error)."""
    try:
        return lock_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _lock_pid_from_text(text: str) -> int | None:
    for token in text.split():
        if token.startswith("pid="):
            try:
                return int(token.split("=", 1)[1])
            except ValueError:
                return None
    return None


def _lock_token_from_text(text: str) -> str | None:
    for token in text.split():
        if token.startswith("token="):
            return token.split("=", 1)[1]
    return None


def _lock_create_time_from_text(text: str) -> float | None:
    for token in text.split():
        if token.startswith("started="):
            try:
                return float(token.split("=", 1)[1])
            except ValueError:
                return None
    return None


def _lock_is_fresh(lock_path: Path, now: float | None = None) -> bool:
    """True when the lock file's mtime is inside the acquire grace window.

    The mtime of a live lock is never refreshed during a run, so this
    check is ONLY used to distinguish "another process is mid-acquire"
    (fresh, no pid yet) from "a process crashed before writing its pid"
    (stale, no pid). Pid-bearing locks are judged purely on pid
    liveness, never on age.
    """
    try:
        mtime = lock_path.stat().st_mtime
    except OSError:
        return True
    return mtime >= (time.time() if now is None else now) - _ACQUIRE_GRACE_SECONDS


def _owner_is_alive(pid: int, create_time: float | None) -> bool:
    """Best-effort liveness probe (no hard psutil dependency).

    With the process creation time recorded in the lock (audit round 22
    P9), PID reuse cannot keep a stale lock "alive" forever: a NEW
    process that happened to inherit the dead owner's PID was started
    at a different moment, so the lock is provably abandoned and gets
    reclaimed. Without the timestamp (no psutil at acquire time, legacy
    lock files) fall back to plain pid liveness — and REFUSE (treat as
    alive) rather than risk stealing a live lock when even that probe
    fails: the user gets the manual-cleanup message.
    """
    try:
        import psutil

        proc = psutil.Process(pid)
        if create_time is not None:
            return abs(proc.create_time() - create_time) < 1.0
        return psutil.pid_exists(pid)
    except psutil.NoSuchProcess:
        return False
    except (psutil.AccessDenied, psutil.ZombieProcess):
        # Process exists but cannot be inspected — assume alive.
        return True
    except Exception:
        # psutil API hiccup — refuse rather than risk stealing a live
        # lock: the user gets the manual-cleanup message.
        return True


def acquire_output_lock(output_path: Path) -> LockHandle:
    """Create ``<output>.lock`` atomically; raise if another run holds it.

    Returns a :class:`LockHandle` (path + owner token) to hand to
    :func:`release_output_lock`. The file's *content* is diagnostic
    (token + pid + source path hint) in case a stale lock survives a
    crash and the user wonders what it is.

    Stale-lock handling:

    * A lock whose pid is ALIVE is refused unconditionally — regardless
      of the lock file's age: the mtime is never refreshed during a
      run, so an old mtime only means the run is *long* (final-concat
      timeouts reach 24 h), not that the owner died. Deleting it would
      clobber a genuinely concurrent run writing the same output.
    * A lock whose pid line is missing but whose mtime is fresh is
      another run in the tiny window between creating the lock file and
      writing its pid — the caller retries briefly instead of stealing
      the lock (race fix).
    * A lock whose pid is gone — or whose pid line never appeared and
      whose mtime is older than the grace window — is presumed
      abandoned (BSOD, ``kill -9``, power loss, crash mid-write) and is
      reclaimed instead of bricking the next run forever.
    """
    token = secrets.token_hex(16)
    lock_path = lock_path_for(output_path)
    # Record OUR process creation time so a later reclaim can tell "the
    # same process still runs" from "some other process reused the pid"
    # (audit round 22 P9). psutil is optional: without it the lock is
    # written without the timestamp and liveness falls back to plain
    # pid checks.
    create_time: float | None = None
    try:
        import psutil

        create_time = psutil.Process().create_time()
    except Exception:
        create_time = None
    for _ in range(_RETRY_MAX_TRIES):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            text = _lock_text_from_content(lock_path)
            pid = _lock_pid_from_text(text)
            if pid is None:
                if _lock_is_fresh(lock_path):
                    # Another run is between open() and its pid write —
                    # the race the audit flagged. Wait for the pid line
                    # to appear instead of deleting a live lock.
                    time.sleep(_RETRY_SLEEP_SECONDS)
                    continue
                # Old lock that never gained a pid line: the previous
                # run crashed in the create→write gap. Fall through to
                # the reclaim path below.
            elif _owner_is_alive(pid, _lock_create_time_from_text(text)):
                # Refuse to clobber another run's output. The error
                # message points at the lock so the user can delete it
                # manually after confirming the other run is really gone.
                raise ConcatLockError(
                    f"Another stream2video run already holds {lock_path} "
                    f"(output {output_path.name} is being written by it, pid={pid}). "
                    f"If that run crashed, delete the .lock file and retry."
                ) from None
            logger.warning(f"Output lock {lock_path} is stale (pid={pid}) — reclaiming it")
            try:
                lock_path.unlink(missing_ok=True)
            except OSError as e:
                raise ConcatLockError(
                    f"Stale lock {lock_path} could not be removed ({e})"
                ) from None
            continue
        except PermissionError as e:
            # Windows: an open handle (another process mid-write) can
            # surface as PermissionError instead of FileExistsError.
            raise ConcatLockError(f"Could not create output lock {lock_path}: {e}") from None
    else:
        raise ConcatLockError(
            f"Another stream2video run is still starting up and holding {lock_path} "
            f"(no pid recorded after {_ACQUIRE_GRACE_SECONDS:.0f}s). "
            f"If that run crashed, delete the .lock file and retry."
        ) from None
    try:
        owner_fields = f"pid={os.getpid()}"
        if create_time is not None:
            owner_fields += f" started={create_time}"
        payload = f"token={token} {owner_fields} output={output_path}\n".encode("utf-8", "replace")
        # POSIX allows PARTIAL writes; a truncated ownership record
        # would make release skip our own lock and could let a live
        # lock be reclaimed as stale (audit round 22 P6). Loop until
        # every byte has landed.
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("output lock write stalled")
            view = view[written:]
    finally:
        os.close(fd)
    # Verify the record round-trips: a lock whose own token/pid cannot
    # be parsed back is a correctness failure, not a diagnostic nicety
    # — refuse to run with an unverifiable lock rather than risk a
    # double-write race or a release that never fires.
    written_text = _lock_text_from_content(lock_path)
    if _lock_token_from_text(written_text) != token or _lock_pid_from_text(written_text) is None:
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise ConcatLockError(
            f"Output lock {lock_path} could not be verified after writing "
            "(token/pid missing) — refusing to run with an unverifiable lock"
        ) from None
    logger.debug(f"Acquired output lock {lock_path}")
    return LockHandle(path=lock_path, token=token)


def release_output_lock(handle: LockHandle | Path) -> None:
    """Best-effort removal of the lock file — only if still owned by us.

    Accepts a :class:`LockHandle` (from :func:`acquire_output_lock`) or
    a bare path (legacy callers). With a handle, the lock is removed
    only when its content still carries the caller's token: if the lock
    was reclaimed and re-taken by another run meanwhile, it is left
    alone instead of deleting a live lock out from under the new owner.
    A bare path is removed unconditionally (legacy behaviour).

    Any failure is logged, never raised, so cleanup doesn't mask the
    pipeline's real error. A leftover lock from a killed process is
    surfaced by :func:`acquire_output_lock` with a clear message on the
    next run.
    """
    try:
        if isinstance(handle, LockHandle):
            path, token = handle.path, handle.token
            if _lock_token_from_text(_lock_text_from_content(path)) != token:
                logger.debug(f"Not removing {path}: lock re-acquired by another run")
                return
        else:
            path = handle
        path.unlink(missing_ok=True)
        logger.debug(f"Released output lock {path}")
    except OSError as e:
        logger.warning(f"Could not remove output lock {path}: {e}")
