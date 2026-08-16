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
import re
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
    _ffmpeg_major_minor.cache_clear()


# ``-filter_complex_script`` (a filtergraph read from a file) existed since
# ffmpeg 2.0 but was REMOVED in the 9.x gyan.dev builds the package managers
# now ship (``Unrecognized option 'filter_complex_script'`` → the whole batch
# pipeline fails). ffmpeg ≥ 7.0 provides the equivalent ``-/filter_complex
# <file>`` (the CLI's "read the option argument from a file" feature). The
# batch pipeline needs the file form to stay under Windows's 32K command-line
# limit for large keep-segment graphs, so pick the flag by version:
#   * major >= 7  →  ``-/filter_complex``
#   * anything else / unparseable  →  legacy ``-filter_complex_script``
_FILTER_SCRIPT_MIN_MODERN = 7


@cache
def _ffmpeg_major_minor() -> tuple[int, int] | None:
    """Parse ffmpeg's ``major.minor`` from ``ffmpeg -version`` (cached).

    Returns None when the binary is missing or the banner doesn't match —
    callers must then assume the legacy option spelling (the oldest builds
    the pipeline supports predate the ``-/option=file`` syntax, and
    mis-guessing "modern" on an ancient ffmpeg fails loudly at spawn time
    instead of silently).
    """
    try:
        proc = subprocess.run(
            [ffmpeg_path(), "-version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            **subprocess_kwargs_lowest(),
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("ffmpeg -version probe failed (%s); assuming legacy CLI", e)
        return None
    m = re.match(r"ffmpeg version [nN]?(\d+)\.(\d+)", (proc.stdout or "").strip())
    if not m:
        logger.warning(
            "could not parse ffmpeg version from banner %r; assuming legacy CLI",
            (proc.stdout or "")[:60],
        )
        return None
    return int(m.group(1)), int(m.group(2))


def subprocess_kwargs_lowest() -> dict[str, Any]:
    """No-window / no-flag kwargs for quick diagnostic spawns.

    Kept separate from the pipeline's subprocess plumbing on purpose: the
    version probe runs before any pipeline subsystem is initialized.
    """
    if os.name == "nt":
        # Windows-only constant; typeshed marks it platform-gated, so
        # Linux CI needs the ignore (unused on Windows — see the mypy
        # override in pyproject).
        return {"creationflags": subprocess.CREATE_NO_WINDOW}  # type: ignore[attr-defined]
    return {}


def filter_complex_script_args(script_path: str) -> list[str]:
    """Return the CLI args that feed a filtergraph file to ffmpeg.

    ``["-/filter_complex", path]`` on ffmpeg ≥ 7, else
    ``["-filter_complex_script", path]``. The 9.x builds (now shipped by
    choco/apt on CI) removed the legacy flag; the ``-/option`` syntax only
    landed in 7.0 — hence the version fork.
    """
    ver = _ffmpeg_major_minor()
    if ver is not None and ver[0] >= _FILTER_SCRIPT_MIN_MODERN:
        return ["-/filter_complex", script_path]
    return ["-filter_complex_script", script_path]


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
            # Drop ONLY the CREATE_NO_WINDOW bit (0x08000000): it's the
            # one that composes badly with an inconsistent parent console
            # (CPython #37380 → winerror 206). Zeroing the whole field
            # also discarded BELOW_NORMAL_PRIORITY_CLASS — so a
            # low-priority retry silently ran at normal priority exactly
            # when the machine was already under the AV/filter-driver
            # load that triggered the retry in the first place. On POSIX
            # ``creationflags`` is Windows-only
            # plumbing: passing a non-zero value raises ValueError
            # instead of FileNotFoundError, which would hide the retry
            # path entirely.
            orig_flags = int(kwargs.get("creationflags", 0))
            if os.name == "nt":
                try_kwargs = {**kwargs, "creationflags": orig_flags & ~0x08000000}
            else:
                try_kwargs = {k: v for k, v in kwargs.items() if k != "creationflags"}
            logger.warning(
                "retrying spawn without CREATE_NO_WINDOW (workaround for "
                "CreateProcess error 206 when parent's console code page "
                "was changed after spawning with CREATE_NO_WINDOW)"
                if os.name == "nt"
                else "retrying spawn after transient FileNotFoundError"
            )
        try:
            return fn(cmd, **try_kwargs)
        except FileNotFoundError as exc:
            last_exc = exc
            probe = _createprocess_probe(cmd[0])
            # ``winerror`` exists on Windows OSError instances; typeshed
            # models it platform-gated, so Linux CI needs the ignore (it is
            # unused on Windows — see the mypy override in pyproject).
            logger.warning(
                "spawn attempt %d/%d failed (FileNotFoundError: filename=%r "
                "winerror=%s); CreateProcess probe: %s",
                attempt + 1,
                1 + _SPAWN_RETRY_ATTEMPTS,
                exc.filename,
                exc.winerror,  # type: ignore[attr-defined]
                probe,
            )
            if attempt >= _SPAWN_RETRY_ATTEMPTS:
                break
            reset_tool_cache()
            # The spawn failure may have been a transiently-blocked ffmpeg
            # (winget shim, AV filter). A previously-run smoke test cached
            # False for the whole process; drop it so the re-resolved
            # binary gets re-smoke-tested ("encoder unavailable"
            # used to stick until app restart).
            try:
                from stream2video.concat.encoders import reset_encoder_check_cache

                reset_encoder_check_cache()
            except Exception:
                logger.debug("reset_encoder_check_cache failed", exc_info=True)
            cmd, _tool = _re_resolve_cmd0(cmd)
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

    # WinDLL / get_last_error / FormatError are Windows-only ctypes
    # helpers; typeshed exposes them on all platforms' type info but marks
    # the attributes platform-gated, so Linux CI needs the ignores (unused
    # on Windows — see the mypy override in pyproject).
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    try:
        try:
            exists = Path(exe).is_file()
        except OSError:
            exists = False

        # A zeroed buffer without ``cb`` set is rejected by CreateProcessW
        # with ERROR_INVALID_PARAMETER — which would mask the REAL spawn
        # failure (winerror 2/3/5, winget-shim-vanished, AV-block) that
        # this probe exists to diagnose. Derive ``cb`` from the actual
        # struct size instead of hard-coding 104 — on a 32-bit Python
        # STARTUPINFOW is 68 bytes and the wrong cb is rejected with
        # ERROR_INVALID_PARAMETER, which is the exact failure mode this
        # probe is supposed to diagnose (not cause).
        class STARTUPINFOW(ctypes.Structure):
            _fields_: ClassVar[list[tuple[str, Any]]] = [
                ("cb", wintypes.DWORD),
                ("lpReserved", wintypes.LPWSTR),
                ("lpDesktop", wintypes.LPWSTR),
                ("lpTitle", wintypes.LPWSTR),
                ("dwX", wintypes.DWORD),
                ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD),
                ("dwYSize", wintypes.DWORD),
                ("dwXCountChars", wintypes.DWORD),
                ("dwYCountChars", wintypes.DWORD),
                ("dwFillAttribute", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD),
                ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
                ("hStdInput", wintypes.HANDLE),
                ("hStdOutput", wintypes.HANDLE),
                ("hStdError", wintypes.HANDLE),
            ]

        si = STARTUPINFOW()
        si.cb = ctypes.sizeof(STARTUPINFOW)

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
            None,
            buf,
            None,
            None,
            False,
            0,
            None,
            None,
            ctypes.byref(si),
            ctypes.byref(pi),
        )
        if ok:
            # Wait for the child to exit so we don't leak a live ffmpeg
            # on the user's machine just for a diagnostic — and so AV
            # software sees a complete spawn+exit, not an orphan. If the
            # child hasn't exited within the window (a hung spawn on a
            # broken shim / AV scan), TERMINATE it explicitly before
            # closing the handles — the old code closed them
            # unconditionally, so a slow ``ffmpeg -version`` kept running
            # with nobody holding its handle (audit round 14 P2; the
            # probe runs in the emergency retry branch, so an orphan
            # would stack a second hung process on top of the original
            # spawn failure).
            wait = kernel32.WaitForSingleObject(pi.hProcess, 2000)
            if wait == 0x00000102:  # WAIT_TIMEOUT
                kernel32.TerminateProcess(pi.hProcess, 1)
                # TerminateProcess is asynchronous at the kernel level;
                # wait for the signal so the handle close below can't
                # race a still-running zombie. The second wait has a
                # generous bound — after TerminateProcess the process is
                # already dead to the scheduler, so this returns almost
                # immediately in practice.
                kernel32.WaitForSingleObject(pi.hProcess, 10000)
            kernel32.CloseHandle(pi.hProcess)
            kernel32.CloseHandle(pi.hThread)
            return f"CreateProcessW OK (exists={exists}); spawn succeeded"
        err = ctypes.get_last_error()  # type: ignore[attr-defined]
        return (
            f"CreateProcessW failed: winerror={err} "
            f"({ctypes.FormatError(err).strip()}); exists={exists}"  # type: ignore[attr-defined]
        )
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
