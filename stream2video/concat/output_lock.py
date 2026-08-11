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
from pathlib import Path

from stream2video.concat.errors import ConcatError

logger = logging.getLogger(__name__)


class ConcatLockError(ConcatError):
    """Raised when another run already holds the lock for this output."""


def lock_path_for(output_path: Path) -> Path:
    return output_path.with_name(output_path.name + ".lock")


def acquire_output_lock(output_path: Path) -> Path:
    """Create ``<output>.lock`` atomically; raise if it already exists.

    Returns the lock path so the caller can pass it to
    :func:`release_output_lock`. The file's *content* is diagnostic
    (pid + source path hint) in case a stale lock survives a crash and
    the user wonders what it is.
    """
    lock_path = lock_path_for(output_path)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        # Refuse to clobber another run's output. The error message
        # points at the lock so the user can delete it manually after
        # confirming the other run is really gone.
        raise ConcatLockError(
            f"Another stream2video run already holds {lock_path} "
            f"(output {output_path.name} is being written by it). "
            f"If that run crashed, delete the .lock file and retry."
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
