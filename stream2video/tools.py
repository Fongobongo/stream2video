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
from typing import Any, ClassVar

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
    """Internal: spawn via Popen or run with retry on transient FNF.

    Each failed attempt logs the full exception detail (``winerror``,
    ``filename``) plus a low-level ``CreateProcessW`` probe of the resolved
    binary. On a locked/missing binary the probe surfaces the real NTSTATUS
    mapped to Win32 (e.g. ``2`` = file not found, ``3`` = path not found,
    ``5`` = access denied), which is what separates "shim vanished" from
    "filter driver blocked the image" — both surface as the same opaque
    ``FileNotFoundError`` from ``subprocess``.
    """
    fn = subprocess.Popen if kind == "popen" else subprocess.run
    last_exc: FileNotFoundError | None = None
    # On the first attempt try exactly what the caller asked. On retries,
    # disable STARTF_USESTDHANDLES: the pipeline's parent's stdhandles can
    # be in an inconsistent state (CPython bug #37380: when the parent's
    # stdout handle is in a "console code page switching" state after
    # ``chcp``, and the child is spawned with ``CREATE_NO_WINDOW`` AND
    # ``STARTF_USESTDHANDLES``, CreateProcessW returns 206). The safe
    # workaround is to spawn with ``creationflags=0`` so Windows uses the
    # parent's console handles as-is (or creates a fresh console where
    # none is attached for pythonw.exe).
    for attempt in range(1 + _SPAWN_RETRY_ATTEMPTS):
        try_kwargs = kwargs
        if attempt > 0:
            try_kwargs = {**kwargs, "creationflags": 0}
            logger.warning(
                "retrying spawn with creationflags=0 (workaround for "
                "CreateProcess error 206 when parent's console code page "
                "was changed after spawning with CREATE_NO_WINDOW)"
            )
        try:
            return fn(cmd, **try_kwargs)
        except FileNotFoundError as exc:
            last_exc = exc
            probe = _createprocess_probe(cmd[0])
            logger.warning(
                "spawn attempt %d/%d failed (FileNotFoundError: filename=%r "
                "winerror=%s); CreateProcess probe: %s",
                attempt + 1,
                1 + _SPAWN_RETRY_ATTEMPTS,
                exc.filename,
                exc.winerror,
                probe,
            )
            if attempt >= _SPAWN_RETRY_ATTEMPTS:
                break
            reset_tool_cache()
            cmd, tool = _re_resolve_cmd0(cmd)
            time.sleep(_SPAWN_RETRY_DELAY_S)
    assert last_exc is not None
    raise last_exc


def _createprocess_probe(exe: str) -> str:
    """Try ``CreateProcessW(exe)`` directly; return a one-line diagnostic.

    Never raises. On non-Windows returns a static note.
    """
    if os.name != "nt":
        return "probe n/a (not Windows)"
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        try:
            exists = Path(exe).is_file()
        except OSError:
            exists = False

        si = (ctypes.c_byte * 112)()  # STARTUPINFO storage; contents unused

        class PROCESS_INFORMATION(ctypes.Structure):
            _fields_: ClassVar[list[tuple[str, Any]]] = [
                ("hProcess", wintypes.HANDLE),
                ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD),
                ("dwThreadId", wintypes.DWORD),
            ]

        pi = PROCESS_INFORMATION()
        buf = ctypes.create_unicode_buffer(f'"{exe}" -version')
        ok = kernel32.CreateProcessW(
            None, buf, None, None, False, 0, None, None,
            ctypes.cast(si, ctypes.c_void_p), ctypes.byref(pi),
        )
        if ok:
            kernel32.CloseHandle(pi.hProcess)
            kernel32.CloseHandle(pi.hThread)
            return f"CreateProcessW OK (exists={exists}); wait, spawn just worked?!"
        err = ctypes.get_last_error()
        return f"CreateProcessW failed: winerror={err} ({ctypes.FormatError(err).strip()}); exists={exists}"
    except Exception as e:  # pragma: no cover - probe must never kill the caller
        return f"probe raised {type(e).__name__}: {e}"


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

