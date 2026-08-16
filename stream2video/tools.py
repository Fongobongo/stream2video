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

import errno
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
        # The version probe runs through the retry layer too (audit round
        # 15 P2): the flag-fork decision below is made ONCE per process
        # and cached, so a transient FileNotFoundError from a WinGet shim
        # replacement / AV filter must not silently force the legacy
        # ``-filter_complex_script`` spelling for the whole process.
        proc = run_with_retry(
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
    """If ``cmd[0]`` is EXACTLY our resolved ffmpeg/ffprobe, re-resolve it.

    Returns ``(new_cmd, tool_name_or_empty)``.

    Audit round 20 P3: the old ``"ffmpeg" in exe0`` substring check
    silently swapped ANY executable whose basename merely CONTAINED
    ``ffmpeg``/``ffprobe`` — ``C:\\tools\\ffmpeg-wrapper.exe``,
    ``/opt/custom-ffmpeg-build`` or ``my_ffmpeg_helper`` would have its
    own binary replaced by the system one while keeping the wrapper's
    arguments. Only the PLAIN basename (``ffmpeg`` / ``ffmpeg.exe`` /
    ``ffprobe`` / ``ffprobe.exe`` — no directory, no prefix, no
    suffix) matches: a custom-named wrapper or patched build is retried
    with its own ``cmd0``, preserving "repeat the same operation".
    """
    if not cmd:
        return cmd, ""
    base = Path(str(cmd[0])).name.lower()
    if base in ("ffprobe", "ffprobe.exe"):
        return [ffprobe_path(), *cmd[1:]], "ffprobe"
    if base in ("ffmpeg", "ffmpeg.exe"):
        return [ffmpeg_path(), *cmd[1:]], "ffmpeg"
    return cmd, ""


def _is_transient_spawn_error(exc: OSError) -> bool:
    """True for the failure codes the retry layer exists for.

    Audit round 19 P1: CreateProcessW error 206
    (ERROR_FILENAME_EXCED_RANGE — the exact incident the retry +
    CREATE_NO_WINDOW-dropping logic was built for, CPython bug #37380)
    surfaces in Python as a BARE ``OSError``, not ``FileNotFoundError``,
    so ``except FileNotFoundError`` never retried it. Retry only:
      * ``errno == ENOENT`` (POSIX and hand-built Windows exceptions);
      * Windows ``winerror`` 2 (file not found), 3 (path not found) and
        206 (filename exceeded range) — the transient set the probe and
        the retry workaround exist for.
    Every other OSError (access denied, invalid argument, ...) is
    permanent and must surface immediately.
    """
    if exc.errno == errno.ENOENT:
        return True
    winerror = getattr(exc, "winerror", None)
    if winerror is not None:
        return winerror in (2, 3, 206)
    return False


def _spawn_with_retry(
    kind: str, cmd: list[str], kwargs: dict[str, Any]
) -> subprocess.Popen[Any] | subprocess.CompletedProcess[Any]:
    """Internal: spawn via Popen or run with retry on transient spawn errors.

    Each failed attempt logs the full exception detail (``winerror``,
    ``filename``) plus a low-level ``CreateProcessW`` probe of the resolved
    binary. On a locked/missing binary the probe surfaces the real NTSTATUS
    mapped to Win32 (e.g. ``2`` = file not found, ``3`` = path not found,
    ``5`` = access denied), which is what separates "shim vanished" from
    "filter driver blocked the image" — both surface as the same opaque
    ``OSError`` from ``subprocess``.
    """
    fn = subprocess.Popen if kind == "popen" else subprocess.run
    last_exc: OSError | None = None
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
            # instead of OSError, which would hide the retry
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
                else "retrying spawn after transient spawn error"
            )
        try:
            return fn(cmd, **try_kwargs)
        except OSError as exc:
            if not _is_transient_spawn_error(exc):
                # Permanent failure (access denied, invalid argument, ...):
                # retrying cannot help and would only mask the real error
                # behind the last-attempt message (audit round 19 P1).
                raise
            last_exc = exc
            probe = _createprocess_probe(cmd[0])
            logger.warning(
                "spawn attempt %d/%d failed (OSError: errno=%r "
                "filename=%r winerror=%r); CreateProcess probe: %s",
                attempt + 1,
                1 + _SPAWN_RETRY_ATTEMPTS,
                exc.errno,
                exc.filename,
                # ``winerror`` exists only on Windows OSError instances;
                # reading it directly crashed the retry loop with an
                # AttributeError on POSIX before the next attempt could
                # run (audit round 18 P1).
                getattr(exc, "winerror", None),
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


def _safe_close_handle(kernel32: Any, handle: Any) -> None:
    """Close a Win32 handle without letting one failure skip the rest.

    The probe owns up to three handles (job, process, thread); a
    raising ``CloseHandle`` (ctypes wrapper / mock error) must not
    prevent the remaining closes (audit round 18 P3).
    """
    try:
        kernel32.CloseHandle(handle)
    except Exception:  # pragma: no cover - best-effort cleanup
        pass


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
        # Job object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: closing
        # the job handle below is a GUARANTEED reaper — the kernel kills
        # the process even if TerminateProcess is rejected/raises, so
        # the "child still alive" diagnostic can no longer coincide with
        # an actual leak. The job is created and configured BEFORE
        # CreateProcessW (audit round 20 P4): if it cannot be set up,
        # the probe does NOT create an active (suspended) process at
        # all. A suspended process that survived a failed
        # TerminateProcess without a job would be a live kernel process
        # with no reaper, repeated once per retry attempt — instead the
        # probe degrades to a static file-existence diagnostic that
        # cannot spawn anything.
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9

        class IO_COUNTERS(ctypes.Structure):
            _fields_: ClassVar[list[tuple[str, Any]]] = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_: ClassVar[list[tuple[str, Any]]] = [
                ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_: ClassVar[list[tuple[str, Any]]] = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        job: Any = None
        job_ok = False
        try:
            job = kernel32.CreateJobObjectW(None, None)
            if job:
                info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
                info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                job_ok = bool(
                    kernel32.SetInformationJobObject(
                        job,
                        JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                        ctypes.byref(info),
                        ctypes.sizeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION),
                    )
                )
        except Exception:
            # Job setup is best-effort plumbing; a broken job must not
            # turn the probe itself into "probe raised ...". Only the
            # kill guarantee is lost — the created handle is KEPT so the
            # finally below still closes it (audit round 19 P2: zeroing
            # ``job`` here leaked a kernel handle in the emergency
            # branch).
            job_ok = False
        if not job_ok:
            if job:
                _safe_close_handle(kernel32, job)
            return f"CreateProcessW probe skipped (job object unavailable); exists={exists}"
        # From here the probe OWNS the child's process/thread handles
        # and the job handle. Every exit path — success, wait timeout,
        # an exception raised by the wait/terminate/formatting calls —
        # must close all handles AND must not leave the child running
        # (audit round 14 P2: a hung child was orphaned; audit round 15
        # P2: an exception after CreateProcessW skipped the cleanup
        # entirely; audit round 18 P2: the job object guarantees the
        # kill even when TerminateProcess fails). The ``finally`` closes
        # every handle INDEPENDENTLY (audit round 18 P3: a raising
        # CloseHandle must not skip the remaining closes); the
        # ``except`` terminates the child best-effort if we never
        # confirmed its exit.
        try:
            ok = kernel32.CreateProcessW(
                None,
                buf,
                None,
                None,
                False,
                # CREATE_SUSPENDED (0x4): the probe never lets the child
                # execute code (audit round 18 P2). The retry branch
                # already failed to spawn this binary; letting it
                # actually run just for a diagnostic could execute
                # arbitrary shim/AV-filter code, and the emergency
                # branch must not be able to stack a second live process
                # on top of the original failure. A suspended process
                # cannot run anything — it is reaped with TerminateProcess
                # and/or the job object below.
                0x00000004,
                None,
                None,
                ctypes.byref(si),
                ctypes.byref(pi),
            )
            if not ok:
                err = ctypes.get_last_error()  # type: ignore[attr-defined]
                return (
                    f"CreateProcessW failed: winerror={err} "
                    f"({ctypes.FormatError(err).strip()}); exists={exists}"  # type: ignore[attr-defined]
                )
            # The child is INSIDE the job from here on — even if every
            # kill path below fails, closing the job handle reaps it.
            try:
                assigned = bool(kernel32.AssignProcessToJobObject(job, pi.hProcess))
            except Exception:
                assigned = False
            if not assigned:
                # Spawned but OUTSIDE the job (assign failed): the
                # suspended child has no reaper. Kill it NOW, verify
                # with the bounded post-kill wait, and report the
                # degraded path explicitly.
                try:
                    terminated = bool(kernel32.TerminateProcess(pi.hProcess, 1))
                except Exception:
                    terminated = False
                wait = kernel32.WaitForSingleObject(pi.hProcess, 10000)
                if wait == 0x00000000:
                    return (
                        f"CreateProcessW OK (exists={exists}); job assign failed, "
                        f"child terminated (TerminateProcess={terminated})"
                    )
                return (
                    f"CreateProcessW OK (exists={exists}); job assign failed, child "
                    f"still alive (post-kill wait={wait:#x}, "
                    f"TerminateProcess={terminated})"
                )
            # The child was created CREATE_SUSPENDED, so it CANNOT exit
            # on its own — the old 2s "natural exit" wait was provably
            # futile and added up to 8s of dead time across four retry
            # attempts (audit round 19 P2). The diagnostic fact is
            # already established by the successful CreateProcessW:
            # Windows created the process object. Reap immediately —
            # terminate best-effort, verify with a bounded post-kill
            # wait (fast after a successful TerminateProcess), and let
            # the job object's kill-on-close guarantee the rest.
            try:
                terminated = bool(kernel32.TerminateProcess(pi.hProcess, 1))
            except Exception:
                terminated = False
            # TerminateProcess is asynchronous at the kernel level;
            # wait for the signal so the handle close below can't race
            # a still-running zombie. After a successful TerminateProcess
            # the process is already dead to the scheduler, so this
            # returns almost immediately in practice.
            wait = kernel32.WaitForSingleObject(pi.hProcess, 10000)
            if wait == 0x00000000:
                return (
                    f"CreateProcessW OK (exists={exists}); spawn ok, suspended "
                    f"child terminated (TerminateProcess={terminated})"
                )
            # Child still alive after the kill attempt (terminate failed
            # or was rejected). The job exists (guaranteed before the
            # spawn), so this cannot mean a leak: closing the job handle
            # in the finally reaps the process via
            # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.
            return (
                f"CreateProcessW OK (exists={exists}); child still alive after "
                f"terminate (post-kill wait={wait:#x}, "
                f"TerminateProcess={terminated}); reaped by job KILL_ON_JOB_CLOSE"
            )
        except Exception as e:  # pragma: no cover - probe must never kill the caller
            # We never confirmed the child exited (wait/terminate
            # raised): kill it best-effort so a diagnostic failure can't
            # leave a live ffmpeg behind, then report the exception.
            try:
                kernel32.TerminateProcess(pi.hProcess, 1)
            except Exception:
                pass
            return f"probe raised {type(e).__name__}: {e}"
        finally:
            # Close every handle INDEPENDENTLY (audit round 18 P3): a
            # CloseHandle that raises (ctypes wrapper / mock error) must
            # not skip the remaining closes. The job handle closes last
            # when it holds the kill guarantee — but ordering among the
            # three is otherwise irrelevant.
            if job:
                _safe_close_handle(kernel32, job)
            _safe_close_handle(kernel32, pi.hProcess)
            _safe_close_handle(kernel32, pi.hThread)
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
