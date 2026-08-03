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
import subprocess
import time
from functools import cache
from pathlib import Path
from typing import Any

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


# Number of *additional* spawn attempts after the first one fails with
# FileNotFoundError. Total spawn tries = 1 + _SPAWN_RETRY_ATTEMPTS.
#
# Why: on Windows, a ``FileNotFoundError`` ``CreateProcess`` result is not
# always permanent — winget shim targets and certain AV/Defender filter
# drivers intermittently block the image briefly. Before this helper even
# one transient failure terminated hours-long pipelines (incident
# 2026-08-02/03: after 380 successful segment encodes in one process, the
# all-important concat spawn failed with "ffmpeg not found in PATH").
_SPAWN_RETRY_ATTEMPTS = 3
_SPAWN_RETRY_DELAY_S = 1.5


def _re_resolve_cmd0(cmd: list) -> tuple[list, str]:
    """If ``cmd[0]`` looks like our resolved ffmpeg/ffprobe, re-resolve it.

    Returns ``(new_cmd, tool_name_or_empty)``.
    """
    exe0 = str(cmd[0]).lower() if cmd else ""
    if "ffprobe" in exe0:
        return [ffprobe_path(), *cmd[1:]], "ffprobe"
    if "ffmpeg" in exe0:
        return [ffmpeg_path(), *cmd[1:]], "ffmpeg"
    return cmd, ""


def _spawn_with_retry(
    kind: str, cmd: list[str], kwargs: dict[str, Any]
) -> subprocess.Popen[Any] | subprocess.CompletedProcess[Any]:
    """Internal: spawn via Popen or run with retry on transient FNF."""
    fn = subprocess.Popen if kind == "popen" else subprocess.run
    last_exc: FileNotFoundError | None = None
    for attempt in range(1 + _SPAWN_RETRY_ATTEMPTS):
        try:
            return fn(cmd, **kwargs)
        except FileNotFoundError as exc:
            last_exc = exc
            if attempt >= _SPAWN_RETRY_ATTEMPTS:
                break
            reset_tool_cache()
            cmd, tool = _re_resolve_cmd0(cmd)
            logger.warning(
                "spawn attempt %d/%d failed (FileNotFoundError for %r); "
                "re-resolved %s and retrying in %.1fs",
                attempt + 1,
                1 + _SPAWN_RETRY_ATTEMPTS,
                cmd[0],
                tool or "tool",
                _SPAWN_RETRY_DELAY_S,
            )
            time.sleep(_SPAWN_RETRY_DELAY_S)
    assert last_exc is not None
    raise last_exc


def popen_with_retry(cmd: list[str], **popen_kwargs: Any) -> subprocess.Popen[Any]:
    """``subprocess.Popen(cmd)`` with transparent retry on transient FNF.

    Re-resolves the binary path (bypassing a possibly stale winget shim),
    waits briefly between attempts, and re-raises the *last*
    ``FileNotFoundError`` if every attempt fails — so callers' existing
    "ffmpeg not found in PATH" handling still fires.
    """
    return _spawn_with_retry("popen", list(cmd), popen_kwargs)  # type: ignore[return-value]


def run_with_retry(cmd: list[str], **run_kwargs: Any) -> subprocess.CompletedProcess[Any]:
    """``subprocess.run(cmd)`` with transparent retry on transient FNF."""
    return _spawn_with_retry("run", list(cmd), run_kwargs)  # type: ignore[return-value]

