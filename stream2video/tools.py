"""Shared resolution of external binaries (ffmpeg / ffprobe).

Why this module exists: on Windows the pipeline historically spawned the
tools by their bare names through ``subprocess.Popen([...\"ffmpeg\"...])``.
WinGet installs ffmpeg as 0-byte App-Execution-Alias reparse points under
``%LOCALAPPDATA%\\Microsoft\\WinGet\\Links\\``; a package self-update can
replace the target binary while a long encode is running, at which point a
transient ``CreateProcess`` failure surfaces as ``FileNotFoundError`` inside
``concat._run_ffmpeg`` — and the old message just said ``"ffmpeg not found in
PATH"``.

Two behaviours pin that down:

* Resolve each tool **once per process** (cached in :data:`_tool_cache`) and
  log the actual binary path at startup so a future failure log is
  self-describing.
* When the found file is a symlink/reparse point (the WinGet case), derefence
  it to the real binary target for the subprocess spawn — bypassing any
  later alias-level hiccup.

Fallback: if ``shutil.which`` finds nothing, :func:`resolve_tool` returns the
bare name so existing ``FileNotFoundError`` handlers still fire with the same
message as before.
"""

from __future__ import annotations

import logging
import os
import shutil
from functools import cache
from pathlib import Path

logger = logging.getLogger(__name__)


@cache
def _resolve_uncached(name: str) -> str:
    """Resolve ``name`` (no extension) to a spawnable path.

    - ``shutil.which`` finds the executable on PATH (WinGet links included).
    - If the found file is a symlink/reparse point, resolve to the real
      target so a WinGet self-update cannot break us mid-run.
    - On any failure (not found, dangling link) log once and fall back to the
      bare ``name`` so the caller's existing ``FileNotFoundError`` handler
      fires with the historical message.
    """
    found = shutil.which(name)
    if not found:
        logger.warning(
            "%s: not found via shutil.which; spawning with bare name "
            "(FileNotFoundError will raise at spawn time if unavailable)",
            name,
        )
        return name

    resolved = found
    try:
        p = Path(found)
        if p.is_symlink() or (os.name == "nt" and p.is_file()):
            target = p.resolve(strict=True)
            if target != p:
                resolved = str(target)
    except (OSError, RuntimeError):
        # Dangling link (mid-WinGet-update) — keep the PATH result; it may
        # still spawn, and if it doesn't, the caller surfaces FileNotFoundError
        # as before.
        resolved = found

    logger.info("%s resolved to %s", name, resolved)
    return resolved


def resolve_tool(name: str) -> str:
    """Return the spawnable path for ``name`` (cached per process)."""
    return _resolve_uncached(name)


def ffmpeg_path() -> str:
    """Convenience: resolved path to the ffmpeg binary."""
    return resolve_tool("ffmpeg")


def ffprobe_path() -> str:
    """Convenience: resolved path to the ffprobe binary."""
    return resolve_tool("ffprobe")


def reset_tool_cache() -> None:
    """Drop the cached resolutions so tests can re-resolve with patched PATH."""
    _resolve_uncached.cache_clear()

