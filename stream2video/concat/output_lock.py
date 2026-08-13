"""Exclusive lock on the pipeline's output file.

Two concurrent runs (GUI + CLI, or two CLIs) pointed at the same
``output_path`` would otherwise interleave ``-y`` writes into the same
file and silently corrupt each other (fix-plan #6). The lock is a small
sibling file created with ``O_CREAT | O_EXCL`` — atomic on every
filesystem, no third-party dependency, and it self-describes: while
``out.mp4.lock`` exists, a second run fails fast with a clear message
instead of producing a half-overwritten video.
"""

import logging
import os
import time
from pathlib import Path

from stream2video.concat.errors import ConcatError

logger = logging.getLogger(__name__)


class ConcatLockError(ConcatError):
    """Raised when another run already holds the lock for this output."""


def lock_path_for(output_path: Path) -> Path:
    return output_path.with_name(output_path.name + ".lock")


# A lock older than this is presumed stale (BSOD / kill -9 / power loss
# leave no pid to check, so the age is the only signal). Timeouts in the
# pipeline top out at 7 days for a single phase, but a lock that old
# with a DEAD pid is unambiguously abandoned.
_STALE_LOCK_AGE_S = 60 * 60


def _lock_pid_from_content(lock_path: Path) -> int | None:
    """Parse the pid recorded in the lock file's diagnostic line."""
    try:
        text = lock_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for token in text.split():
        if token.startswith("pid="):
            try:
                return int(token.split("=", 1)[1])
            except ValueError:
                return None
    return None


def _pid_is_alive(pid: int) -> bool:
    """Best-effort liveness probe (no psutil dependency here)."""
    try:
        import psutil

        return psutil.pid_exists(pid)
    except Exception:
        # No psutil or an API hiccup — fall back to the age heuristic.
        return True


def acquire_output_lock(output_path: Path) -> Path:
    """Create ``<output>.lock`` atomically; raise if another run holds it.

    Returns the lock path so the caller can pass it to
    :func:`release_output_lock`. The file's *content* is diagnostic
    (pid + source path hint) in case a stale lock survives a crash and
    the user wonders what it is.

    Stale-lock handling (C15 audit): a lock whose pid is gone — or that
    is older than :data:`_STALE_LOCK_AGE_S` — is presumed abandoned
    (BSOD, ``kill -9``, power loss) and is reclaimed instead of
    bricking the next run forever. A lock whose pid is ALIVE is refused
    unconditionally: deleting it would clobber a genuinely concurrent
    run writing the same output.
    """
    lock_path = lock_path_for(output_path)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        lock_age = 0.0
        try:
            lock_age = time.time() - lock_path.stat().st_mtime
        except OSError:
            pass
        pid = _lock_pid_from_content(lock_path)
        stale = (
            pid is None
            or not _pid_is_alive(pid)
            or lock_age >= _STALE_LOCK_AGE_S
        )
        if stale:
            logger.warning(
                f"Output lock {lock_path} is stale "
                f"(pid={pid}, age={lock_age:.0f}s) — reclaiming it"
            )
            try:
                lock_path.unlink(missing_ok=True)
            except OSError as e:
                raise ConcatLockError(
                    f"Stale lock {lock_path} could not be removed ({e})"
                ) from None
            return acquire_output_lock(output_path)
        # Refuse to clobber another run's output. The error message
        # points at the lock so the user can delete it manually after
        # confirming the other run is really gone.
        raise ConcatLockError(
            f"Another stream2video run already holds {lock_path} "
            f"(output {output_path.name} is being written by it, pid={pid}). "
            f"If that run crashed, delete the .lock file and retry."
        ) from None
    except PermissionError as e:
        # Windows: an open handle (another process mid-write) can
        # surface as PermissionError instead of FileExistsError.
        raise ConcatLockError(
            f"Could not create output lock {lock_path}: {e}"
        ) from None
    try:
        os.write(
            fd,
            f"pid={os.getpid()} output={output_path}\n".encode("utf-8", "replace"),
        )
    finally:
        os.close(fd)
    logger.debug(f"Acquired output lock {lock_path}")
    return lock_path


def release_output_lock(lock_path: Path) -> None:
    """Best-effort removal of the lock file.

    Called in a ``finally`` — any failure is logged, never raised, so
    cleanup doesn't mask the pipeline's real error. A leftover lock from
    a killed process is surfaced by :func:`acquire_output_lock` with a
    clear message on the next run.
    """
    try:
        lock_path.unlink(missing_ok=True)
        logger.debug(f"Released output lock {lock_path}")
    except OSError as e:
        logger.warning(f"Could not remove output lock {lock_path}: {e}")
