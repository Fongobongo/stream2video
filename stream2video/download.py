"""Video download module using yt-dlp CLI subprocess (cancellable)."""

import logging
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from stream2video.config import CONFIG_DEFAULTS
from stream2video.tools import _is_transient_spawn_error, popen_with_retry
from stream2video.utils import (
    CANCEL_POLL_INTERVAL,
    drain_stderr_lines,
    registered_process,
    subprocess_kwargs,
)

logger = logging.getLogger(__name__)


class DownloadResult(NamedTuple):
    path: Path
    is_downloaded: bool
    # yt-dlp extractor key (``YouTube``, ``Twitch``, ...) of the resolved
    # video, when yt-dlp reported it. None for local files and when the
    # report could not be parsed. Used to namespace the stable project
    # identity so two sites with the same video id never collide
    # (audit round 25 P2).
    extractor_key: str | None = None


class DownloadProgress(NamedTuple):
    """Progress update from yt-dlp.

    Any field may be None when yt-dlp doesn't know it yet (e.g. total
    bytes for a stream that doesn't report its size, or speed during the
    initial ramp-up). Callers should treat None as "unknown" and fall
    back to an indeterminate display.
    """

    downloaded_bytes: float | None
    total_bytes: float | None
    speed: float | None  # bytes per second
    eta: float | None  # seconds remaining


class DownloadError(Exception):
    """Base download error."""


class DownloadCancelledError(DownloadError):
    """Download was cancelled by user (not a real failure).

    ``partial`` marks "the process had already produced bytes when the
    cancel landed" — i.e. the file on disk is truncated relative to
    whatever yt-dlp was going to write. The pipeline controller uses
    this to decide whether the file is safe to leave behind for resume
    (``partial=False``: a fully-written source the user may want to
    reuse) or should be unlinked as garbage (``partial=True``).
    """

    def __init__(self, message: str = "Download cancelled", *, partial: bool = True) -> None:
        super().__init__(message)
        self.partial = partial


class URLValidationError(DownloadError):
    """Invalid URL format."""


class VideoNotAvailableError(DownloadError):
    """Video not available."""


class DownloadTimeoutError(DownloadError):
    """Download timeout."""


class DiskSpaceError(DownloadError):
    """Insufficient disk space."""


class PermissionDeniedError(DownloadError):
    """Permission denied."""


class FileBusyError(DownloadError):
    """File locked by another process (e.g. opened in a media player)."""


def _validate_url(url: str) -> bool:
    """Validate if string is an http(s) URL.

    Bare domains (e.g. ``youtube.com/...``) are intentionally rejected so that
    typos like ``myvideo.mp4`` don't get misclassified as a URL once the
    ``_is_local_file`` check has passed (non-existent local path).
    """
    return bool(re.match(r"^https?://", url, re.IGNORECASE))


def redact_input_url(url: str) -> str:
    """Mask the secret-bearing parts of an input URL for logging.

    Signed URLs, basic-auth userinfo, API tokens and private query
    parameters must never reach the GUI log, ``stream2video.log`` or a
    lock-wait message (audit round 25 P4): the full URL stays in
    memory only, handed to yt-dlp as-is. Query and fragment are dropped
    entirely (no whitelist of "safe" params to maintain), userinfo is
    replaced with ``<redacted>``, and the scheme+host+port+path are
    kept so the user can still see which site the run is about. Local
    paths and anything that is not an http(s) URL pass through
    unchanged.
    """
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    if parts.scheme.lower() not in ("http", "https"):
        return url
    host = parts.hostname or parts.netloc
    if parts.port:
        host = f"{host}:{parts.port}"
    netloc = f"<redacted>@{host}" if parts.username else host
    path = parts.path
    if parts.query:
        path = f"{path}?<redacted>"
    if parts.fragment:
        path = f"{path}#<redacted>"
    return urllib.parse.urlunsplit((parts.scheme, netloc, path, "", ""))


def _is_local_file(path_str: str) -> bool:
    """Check if path is local file."""
    try:
        path = Path(path_str)
        return path.is_file()
    except (OSError, ValueError):
        return False


# Watchdog timeouts for download connectivity / liveness. ``download_timeout``
# (CONFIG_DEFAULTS, 8h) is the absolute ceiling for the whole download,
# sized for big VODs; the watchdogs below catch the much more common failure
# modes where the connection hangs without yt-dlp ever reporting an error
# and the 8h ceiling would leave the user staring at a frozen progress bar.
#
# ``connect_timeout`` — first byte / first progress event after start.
# Covers DNS failure, TCP/TLS handshake hang, and the initial buffering
# before yt-dlp emits its first progress line. If no progress arrives
# within this window the download is killed with a clear timeout error
# instead of waiting for the 8h ceiling.
#
# ``no_progress_timeout`` — gap between consecutive progress events.
# If yt-dlp goes silent for this long mid-download the connection has
# almost certainly stalled (server stopped sending, route black-holed,
# etc). The 8h ceiling is far too long for this case.
#
# All three live in ``CONFIG_DEFAULTS`` (config.py) — the single source
# of truth the CLI, GUI, and pipeline all read; the function defaults
# below are lookups of the same table so a caller that omits them gets
# exactly the configured values (R2.17 audit: the values used to be
# duplicated here as module constants, so a config change didn't move
# the function defaults).

# yt-dlp format selectors by quality preset.
#
# ``best`` historically used ``best[ext=mp4]/best`` — a single pre-merged
# file. On YouTube that often picks a 720p pre-merged stream even when a
# 1080p video-only stream exists, and it forfeits the better audio codec
# (Opus vs AAC) that the separate audio stream offers. The new default
# mirrors the other presets: ``bestvideo+bestaudio`` first (yt-dlp merges
# with ffmpeg when needed), with a pre-merged file as the fallback so a
# site without separate tracks still works.
#
# The fallback ``/best`` at the end of the resolution-capped selectors
# is kept: if a site only serves a pre-merged stream larger than the
# cap, the user gets it rather than a hard failure. This is documented
# behaviour — users who want a strict cap should know that
# pre-merged-only sites can still exceed it.
_DOWNLOAD_FORMATS: dict[str, str] = {
    "best": "bestvideo+bestaudio/best[ext=mp4]/best",
    "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
    "720p": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
    "480p": "bestvideo[height<=480]+bestaudio/best[height<=480]/best",
    "360p": "bestvideo[height<=360]+bestaudio/best[height<=360]/best",
}


def _format_selector_for_quality(quality: str) -> str:
    """Return the yt-dlp format string for a quality preset."""
    try:
        return _DOWNLOAD_FORMATS[quality]
    except KeyError as e:
        raise DownloadError(
            f"Unknown download quality: {quality!r} "
            f"(use {' or '.join(repr(q) for q in _DOWNLOAD_FORMATS)})"
        ) from e


# Prefix used for yt-dlp ``--progress-template`` lines so they can be
# distinguished from the regular stdout (notably the final filepath line
# produced by ``--print after_move:filepath``).Parsed by
# ``_parse_progress_line``.
_PROGRESS_PREFIX = "s2v_progress|"


def _parse_progress_line(line: str) -> DownloadProgress | None:
    """Parse a yt-dlp ``--progress-template`` line into a DownloadProgress.

    The line format is:
        s2v_progress|<downloaded_bytes>|<total_bytes>|<total_bytes_estimate>|<speed>|<eta>

    yt-dlp writes ``NA`` for fields it doesn't know yet (e.g. total size
    for a stream that doesn't report it). Returns ``None`` for lines
    that aren't progress updates (including the final filepath line from
    ``--print``).

    ``total_bytes`` (Content-Length from the HTTP response) is preferred
    over ``total_bytes_estimate`` (yt-dlp's heuristic) when both are
    present; if ``total_bytes`` is ``NA``, the estimate is used. This
    fixes the previous behaviour where ``total_bytes`` (often known for
    regular HTTP downloads) was ignored and only the estimate was
    surfaced, leaving the UI showing an indeterminate bar for streams
    that did report their size.
    """
    if not line.startswith(_PROGRESS_PREFIX):
        return None
    parts = line[len(_PROGRESS_PREFIX) :].split("|")
    # The template we pass to yt-dlp always emits 5 fields
    # (``%(progress.*)s`` was available long before our minimum supported
    # yt-dlp version; the 4-field "legacy" layout was a pre-template
    # experiment that no supported yt-dlp emits, and was removed here
    # after the audit showed it was dead code in production paths).
    if len(parts) != 5:
        return None

    def _f(v: str) -> float | None:
        if v == "NA" or v == "":
            return None
        try:
            f = float(v)
        except (ValueError, TypeError):
            return None
        # Reject NaN / inf — float("NaN") / float("inf") parse fine but
        # break numeric comparisons downstream (e.g. downloaded/total,
        # ``min(1.0, x)``). Treat them as "unknown" so the caller falls
        # back to the indeterminate display.
        if f != f or f in (float("inf"), float("-inf")):
            return None
        return f

    downloaded = _f(parts[0])
    total = _f(parts[1])
    total_estimate = _f(parts[2])
    speed = _f(parts[3])
    eta = _f(parts[4])

    # Prefer the exact total; fall back to yt-dlp's estimate when the
    # exact value isn't known (live streams, chunked transfers, etc).
    effective_total = total if total is not None else total_estimate

    return DownloadProgress(
        downloaded_bytes=downloaded,
        total_bytes=effective_total,
        speed=speed,
        eta=eta,
    )


_VIDEO_EXTENSIONS = frozenset(
    {
        ".mp4",
        ".mkv",
        ".webm",
        ".mov",
        ".avi",
        ".ts",
        ".m4v",
        ".flv",
        ".wmv",
        ".mpg",
        ".mpeg",
        ".3gp",
    }
)


def _classify_error(stderr: str) -> DownloadError:
    """Map yt-dlp stderr text to a DownloadError subclass."""
    msg = stderr.lower()
    unavailable_markers = (
        "video unavailable",
        "this video is unavailable",
        "this video is no longer available",
        "video is private",
        "this video is private",
        "has been removed",
    )
    if any(m in msg for m in unavailable_markers):
        return VideoNotAvailableError("Video not available")
    if "no space left" in msg or "disk full" in msg or "errno 28" in msg:
        return DiskSpaceError("Insufficient disk space")
    # Windows file-lock and POSIX EBUSY mean "close the player/browser tab
    # holding this file" — a different remediation than chmod/permissions,
    # so classify before the generic PermissionDenied branch.
    if (
        "winerror 32" in msg
        or "being used by another process" in msg
        or "resource busy" in msg
        or "device or resource busy" in msg
        or "errno 16" in msg
    ):
        return FileBusyError(
            "A file is locked by another program "
            "(close the media player / browser tab using it and retry)"
        )
    if "permission denied" in msg or "errno 13" in msg:
        return PermissionDeniedError("Permission denied")
    # Keep the message prefix-free: both the CLI ("Download failed: {e}")
    # and the GUI add their own label, so a "Download failed:" here would
    # double-print.
    return DownloadError(stderr[:500] if stderr else "unknown error")


def _find_downloaded_file(out_dir: Path, expected: Path) -> Path | None:
    """Locate the downloaded file; fall back to glob by video id (video extensions only)."""
    if expected.exists():
        return expected
    # Use Path.stem so multi-dot suffixes (.mp4.part, .webm.part) and
    # IDs with characters outside [\w-] are handled correctly. The
    # previous regex anchored `[/\\]([\w-]+)\.\w+$` which dropped the
    # leaf for `.mp4.part` (the trailing `\w+$` matches `part`, not the
    # real stem).
    try:
        video_id = Path(expected).stem
    except (ValueError, OSError):
        return None
    if not video_id:
        return None
    candidates = [p for p in out_dir.glob(f"{video_id}.*") if p.suffix.lower() in _VIDEO_EXTENSIONS]
    if not candidates:
        return None

    def _mtime(p: Path) -> float:
        """mtime for "newest wins"; a file deleted between glob() and
        stat() (antivirus, user cleanup) would otherwise raise OSError
        out of the whole fallback."""
        try:
            return p.stat().st_mtime
        except OSError:
            return -1.0

    return max(candidates, key=_mtime)


def _stderr_snippet(stderr_chunks: list[str], limit: int = 300) -> str:
    text = "".join(stderr_chunks).strip()
    return text[-limit:] if text else ""


def _timeout_error(message: str, stderr_chunks: list[str]) -> DownloadTimeoutError:
    snippet = _stderr_snippet(stderr_chunks)
    if snippet:
        return DownloadTimeoutError(f"{message}. stderr: {snippet}")
    return DownloadTimeoutError(message)


def _sweep_partial_fragments(out_dir: Path, run_id: str) -> None:
    """Remove THIS run's orphaned yt-dlp partial fragments.

    yt-dlp's outtmpl leaves ``*.part`` / ``*.ytdl`` / ``*.temp`` behind
    when a download is cancelled mid-write. The download loop raises
    ``DownloadCancelledError(partial=True)`` BEFORE the resolver maps
    stdout to a destination path, so the pipeline controller's cleanup
    can not unlink a path it never learned (P2 audit: every cancelled
    download leaked a uniquely-named dead file).

    Only files carrying THIS run's unique token are deleted (audit
    round 24 P3): the directory can hold concurrent sibling downloads,
    and an mtime/snapshot heuristic misclassifies a sibling's fragment
    created after our snapshot as ours — deleting another run's live
    download. The token is unique per invocation and embedded in every
    filename this run's yt-dlp writes (``<id>-<epoch>-<token>.<ext>.part``),
    so a name is matched structurally: the token must sit in the
    ``-<epoch>-<token>`` slot of the outtmpl, right before the fragment
    suffix — a plain substring test could hit a video id or another
    run's fragment that happens to contain the same eight hex chars
    (audit round 25 P9).
    """
    fragment_re = re.compile(rf"-{re.escape(run_id)}\.[^.]*\.(?:part|ytdl|temp)$")
    try:
        entries = list(out_dir.iterdir())
    except OSError:
        return

    for p in entries:
        try:
            name = p.name
        except OSError:
            continue
        if not name.endswith((".part", ".ytdl", ".temp")):
            continue
        if fragment_re.search(name) is None:
            continue
        try:
            os.unlink(p)
            logger.info(f"Removed orphaned download fragment: {p}")
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.debug(f"Could not unlink partial fragment {p}: {e}")


def _resolve_reported_download_path(
    out_dir: Path, stdout_lines: list[str]
) -> tuple[Path | None, Path | None]:
    """Find the downloaded file path among yt-dlp stdout lines."""
    last_candidate: Path | None = None
    for line in reversed(stdout_lines):
        text = line.strip()
        if not text:
            continue
        candidate = Path(text)
        if last_candidate is None:
            last_candidate = candidate
        resolved = _find_downloaded_file(out_dir, candidate)
        if resolved is not None:
            return resolved, candidate
    return None, last_candidate


def _parse_extractor_key(stdout_lines: list[str]) -> str | None:
    """Read the ``before_dl:%(extractor_key)s`` line from yt-dlp stdout.

    The print arrives as the FIRST stdout line (it runs before the
    download produces any progress output). Returns None when the line
    is missing or looks like something else (a relative path, a
    progress echo): the caller then falls back to the un-namespaced
    identity, which is no worse than before the extractor existed.
    """
    for line in stdout_lines:
        text = line.strip()
        if not text:
            continue
        # A path-like first line (relative path without separators,
        # extension, or a drive prefix) is not an extractor key.
        if (
            os.sep in text
            or (os.altsep and os.altsep in text)
            or text.lower().endswith(tuple(_VIDEO_EXTENSIONS))
            or re.match(r"^[a-zA-Z]:[\\/]", text)
        ):
            return None
        return text
    return None


def download(
    url: str,
    out_dir: Path,
    cancel_callback: Callable[[], bool] | None = None,
    quality: str = "best",
    progress_callback: Callable[[DownloadProgress], None] | None = None,
    download_timeout: int = CONFIG_DEFAULTS["download_timeout"],
    connect_timeout: int = CONFIG_DEFAULTS["connect_timeout"],
    no_progress_timeout: int = CONFIG_DEFAULTS["no_progress_timeout"],
    proxy: str = "",
    low_process_priority: bool = False,
    rlimit_as_mb: int = 0,
) -> DownloadResult:
    """
    Download video from URL via yt-dlp CLI, or pass through a local file.

    Spawns yt-dlp as a subprocess so the call can be cancelled (via cancel_callback
    or GUI close) and the OS process can be killed cleanly. The process is
    registered with registered_process (owner="default") for external kill.

    Args:
        url: Video URL or local file path
        out_dir: Output directory for downloaded video
        cancel_callback: Optional callable returning True to abort
        quality: Download quality preset — one of
            ``best`` / ``1080p`` / ``720p`` / ``480p`` / ``360p``.
            Mapped to a yt-dlp format selector. Ignored for local files.
        progress_callback: Optional callable receiving a ``DownloadProgress``
            on each yt-dlp progress update (downloaded bytes, total bytes,
            speed in bytes/sec, ETA in seconds). Any field may be ``None``
            when yt-dlp doesn't know it yet. Called from the stdout drain
            thread — callers must be thread-safe (the CLI's Rich task update
            is; the GUI schedules onto the Tk main loop via ``after``).
        low_process_priority: spawn yt-dlp at BELOW_NORMAL priority (Windows)
            / nice 10 (POSIX), matching the concat runner's ffmpeg policy.
        rlimit_as_mb: POSIX-only RLIMIT_AS cap in MiB for the yt-dlp child
            (no-op on Windows); 0 disables.

    Returns:
        DownloadResult with `path` to the file and `is_downloaded` flag

    Raises:
        URLValidationError: Invalid URL format
        VideoNotAvailableError: Video not accessible
        DownloadCancelledError: User cancellation (subclass of DownloadError)
        DownloadError: Generic failure
    """
    if _is_local_file(url):
        path = Path(url)
        try:
            size = path.stat().st_size
        except OSError as e:
            raise DownloadError(f"Cannot read local file {path}: {e}") from e
        logger.info(f"Using local file: {url} ({size // 1024 // 1024} MB)")
        return DownloadResult(path, is_downloaded=False)

    if not _validate_url(url):
        raise URLValidationError(f"Invalid URL: {url}")

    out_dir.mkdir(parents=True, exist_ok=True)

    # Per-invocation token embedded in every filename this run's yt-dlp
    # writes. ``%(epoch)s`` alone has SECOND granularity — two runs of
    # the same URL in the same second would write/resume the SAME file
    # (audit round 24 P2). The token also lets the failure-path sweep
    # identify exactly this run's fragments (audit round 24 P3).
    run_id = secrets.token_hex(4)

    format_str = _format_selector_for_quality(quality)
    logger.info(f"Download quality: {quality} ({format_str})")

    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-warnings",
        "--newline",
        # Send one progress update per line to stdout, using a prefix we
        # can recognise and parse (see _parse_progress_line). yt-dlp emits
        # ``NA`` for fields it doesn't know yet (e.g. total_bytes_estimate
        # for streams without a content-length).
        #
        # Template field names: yt-dlp's ``--progress-template`` exposes a
        # dict with ``info`` and ``progress`` sub-dicts. Old yt-dlp (pre
        # 2022) accepted bare ``%(downloaded_bytes)s``; current yt-dlp
        # (verified with 2026.03.17) treats bare names as attributes of
        # the ``info`` dict, which doesn't have ``downloaded_bytes`` etc,
        # so the values come out as ``NA`` and the UI shows "?" for
        # speed / percent / ETA despite the download running fine. Use
        # the explicit ``progress.*`` prefix so the values populate
        # correctly across all supported yt-dlp versions.
        #
        # ``total_bytes`` is preferred over the estimate when yt-dlp knows
        # it (HTTP responses with a Content-Length header). The parser
        # below uses ``total_bytes`` as-is; ``total_bytes_estimate`` is
        # only used as a fallback when ``total_bytes`` is ``NA`` (see
        # ``_parse_progress_line``).
        "--progress-template",
        (
            f"{_PROGRESS_PREFIX}%(progress.downloaded_bytes)s"
            f"|%(progress.total_bytes)s|%(progress.total_bytes_estimate)s"
            f"|%(progress.speed)s|%(progress.eta)s"
        ),
        "--output",
        # %(epoch)s + a per-invocation token makes the filename unique
        # per run: two runs pointed at the same URL in the same second
        # (or a rerun against an abandoned .part fragment from a
        # previous run) can never write into / resume the same file.
        # The stdout ``after_move:filepath`` report still gives us the
        # exact path, and _find_downloaded_file's stem-glob matches
        # "<id>-NNN-TOKEN.*" under the "<id>.*" prefix (newest mtime
        # wins).
        str(out_dir / f"%(id)s-%(epoch)s-{run_id}.%(ext)s"),
        "--format",
        format_str,
        "--print",
        "after_move:filepath",
        # The extractor key (``YouTube``, ``Twitch``, ...) namespaces the
        # stable project identity: two different sites with the same
        # video id must never share a project dir / caches (audit round
        # 25 P2). Printed once BEFORE the download (before_dl) so the
        # line arrives as the FIRST stdout line, ahead of any progress
        # output.
        "--print",
        "before_dl:%(extractor_key)s",
        url,
    ]

    if proxy:
        cmd.extend(["--proxy", proxy])

    logger.info(f"Downloading: {redact_input_url(url)}")
    try:
        # popen_with_retry: a winget shim / AV filter driver
        # intermittently returns FileNotFoundError on spawn; an 8-hour VOD
        # download must survive that transient.
        process = popen_with_retry(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # stdin=DEVNULL: same rationale as concat/runner.py — when the
            # parent is pythonw.exe (GUI subsystem) with an attached
            # console, inheriting the parent's console-mode stdin handle
            # makes CreateProcessW fail with winerror 206.
            stdin=subprocess.DEVNULL,
            text=True,
            # yt-dlp reconfigures its own stdout/stderr to UTF-8; without
            # an explicit ``encoding=`` the pipes are decoded with the
            # Windows ANSI codepage (cp1251/cp1252), so a non-ASCII
            # output path in ``--print after_move:filepath`` arrives as
            # mojibake and the file lookup below reports "file not found".
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **subprocess_kwargs(low_process_priority, rlimit_as_mb),
        )
    except FileNotFoundError as e:
        raise DownloadError("yt-dlp not found (install via 'pip install yt-dlp')") from e
    except OSError as e:
        # popen_with_retry retries transient spawn failures; an
        # EXHAUSTED transient (e.g. WinError 206: the executable was
        # being replaced by a package-manager shim / AV filter during
        # the retry window) must surface as a download-class error, not
        # leak into the pipeline as "Unexpected" (audit round 24 P9 —
        # the encoder smoke-test already classifies this via
        # _is_transient_spawn_error). A PERMANENT OSError (permissions,
        # resource limits) is not a download failure — propagate it
        # truthfully so the caller sees the real cause.
        if _is_transient_spawn_error(e):
            raise DownloadError(
                "yt-dlp could not be started (the executable was unavailable "
                "or being replaced — antivirus / package-manager shim). "
                "Retry the download."
            ) from e
        raise

    with registered_process(process):
        stdout_lines: list[str] = []
        stderr_chunks: list[str] = []

        # Watchdog state. ``last_progress_time`` is updated by the stdout
        # drain thread each time a parseable progress line arrives; the main
        # loop checks it against ``connect_timeout`` (before first progress)
        # and ``no_progress_timeout`` (mid-download). A non-None value also
        # tells the UI layer that yt-dlp is actually pushing bytes, not just
        # sitting on an idle connection.
        #
        # The list container is a Python-mutable-cell workaround for the
        # closure capturing ``last_progress_time`` by reference in a nested
        # function — direct assignment would create a local binding in the
        # drain thread and the main loop wouldn't see updates.
        #
        # NOTE: the connect watchdog measures from ``start_time`` ONLY —
        # ordinary stdout/stderr lines (progress-template chatter, extractor
        # logs, retry notices) must NOT reset it. A chatty-but-hung yt-dlp
        # that emits lines without ever delivering a progress event would
        # otherwise dodge the connect timeout and sit until the 8h ceiling.
        last_progress_time: list[float | None] = [None]
        start_time = time.monotonic()

        def _drain_stdout() -> None:
            # ``process.stdout`` is non-None here (we set stdout=PIPE in
            # Popen), but mypy can't prove it. Assert once so the for-loop
            # below sees a concrete IO[Any] instead of IO[Any] | None.
            stdout = process.stdout
            if stdout is None:
                return
            for line in stdout:
                text = line.rstrip()
                prog = _parse_progress_line(text)
                if prog is not None:
                    last_progress_time[0] = time.monotonic()
                    if progress_callback is not None:
                        try:
                            progress_callback(prog)
                        except Exception:
                            # A callback crash must not break the download —
                            # progress is best-effort UI feedback, not a hard
                            # signal. Log at WARNING so the user (and any
                            # developer reading the log) sees the real reason
                            # the progress bar stopped updating, not just a
                            # silent freeze.
                            logger.warning(
                                "progress_callback raised; download continues",
                                exc_info=True,
                            )
                    continue
                stdout_lines.append(text)

        # Stderr drain via the shared drain_stderr_lines: the historical
        # local ``_drain_stderr`` thread re-implemented the same ring
        # trim (head + marker + tail) and thread lifecycle — now owned by
        # utils so download.py can't drift from the other pipelines.
        assert process.stderr is not None  # Popen is always created with stderr=PIPE
        wait_for_drain = drain_stderr_lines(process.stderr, stderr_chunks)

        stdout_thread = threading.Thread(target=_drain_stdout, daemon=True)
        stdout_thread.start()

        success = False
        try:
            deadline = time.monotonic() + download_timeout
            while True:
                if process.poll() is not None:
                    break
                if cancel_callback and cancel_callback():
                    process.kill()
                    try:
                        process.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        pass
                    raise DownloadCancelledError(
                        "Download cancelled by user",
                        # partial=True: cancel fired while the process was
                        # alive, when we have no evidence the file is
                        # complete. The caller treats it as truncated and
                        # unlinks rather than surfacing as a valid source.
                        partial=True,
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    process.kill()
                    try:
                        # Bounded reap: TerminateProcess is async on
                        # Windows; an unbounded wait() would hang the
                        # download worker if the OS-level kill blocks
                        # (network share, AV filter driver).
                        process.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        pass
                    stdout_thread.join(timeout=2)
                    wait_for_drain(2.0)
                    raise _timeout_error(
                        f"Download timeout after {download_timeout}s",
                        stderr_chunks,
                    )

                # Connection / progress watchdog. Two branches:
                #   1. No progress yet AND we're past connect_timeout — the
                #      connection didn't establish or yt-dlp is stuck before
                #      the first byte. Kill with a clearer error than the
                #      generic ceiling.
                #   2. Progress seen before but silent for no_progress_timeout
                #      — the connection dropped mid-download. yt-dlp's own
                #      retry logic (when enabled) usually fires first, but we
                #      don't enable it, so the watchdog is the only safety.
                now = time.monotonic()
                if last_progress_time[0] is None:
                    idle_for = now - start_time
                    if idle_for > connect_timeout:
                        process.kill()
                        try:
                            process.wait(timeout=30)
                        except subprocess.TimeoutExpired:
                            pass
                        stdout_thread.join(timeout=2)
                        wait_for_drain(2.0)
                        raise _timeout_error(
                            f"Download stalled before first byte: no progress "
                            f"within {connect_timeout}s of start "
                            "(DNS/TLS/handshake?)",
                            stderr_chunks,
                        )
                else:
                    silent_for = now - last_progress_time[0]
                    if silent_for > no_progress_timeout:
                        process.kill()
                        try:
                            process.wait(timeout=30)
                        except subprocess.TimeoutExpired:
                            pass
                        stdout_thread.join(timeout=2)
                        wait_for_drain(2.0)
                        raise _timeout_error(
                            f"Download stalled: no progress for {int(silent_for)}s",
                            stderr_chunks,
                        )
                try:
                    process.wait(timeout=min(CANCEL_POLL_INTERVAL, remaining))
                except subprocess.TimeoutExpired:
                    pass

            stdout_thread.join(timeout=2)
            wait_for_drain(2.0)

            if process.returncode != 0:
                stderr_text = "".join(stderr_chunks)
                raise _classify_error(stderr_text) from None

            if not stdout_lines:
                stderr_text = "".join(stderr_chunks)
                raise DownloadError(f"yt-dlp produced no file path. stderr: {stderr_text[:300]}")

            resolved, reported_path = _resolve_reported_download_path(out_dir, stdout_lines)
            if resolved is None:
                raise DownloadError(f"Download completed but file not found: {reported_path}")

            extractor_key = _parse_extractor_key(stdout_lines)

            # ``stat()`` can raise a raw OSError (file quarantined by AV
            # between the resolve and here, permissions, ...) which is
            # not a ``DownloadError`` — surface it as one so the pipeline
            # controller maps it to a download failure, not "unexpected
            # error".
            try:
                size = resolved.stat().st_size
            except OSError as e:
                raise DownloadError(f"Downloaded file unreadable: {resolved}: {e}") from e
            if size == 0:
                raise DownloadError(f"Download completed but file is empty: {resolved}")

            logger.info(f"Successfully downloaded: {resolved} ({size // 1024 // 1024} MB)")
            success = True
            return DownloadResult(resolved, is_downloaded=True, extractor_key=extractor_key)

        finally:
            # Join drain threads in the finally so cancel/timeout/early-raise
            # paths still wait for them. Threads exit when their pipes close
            # (next block), so the join is bounded; a missed join could leak
            # the daemon thread's pipe reads until process exit on Windows.
            #
            # Kill-first: on any path that ISN'T one of the
            # four explicit kills above (an unexpected exception from the
            # progress callback, KeyboardInterrupt, OSError, ...) the yt-dlp
            # child would otherwise survive us — an orphaned process keeps
            # draining network→disk for hours while the GUI reports the
            # download as finished/failed. Only the failure paths kill the
            # process explicitly; on success it has already exited (the loop
            # breaks on poll() is not None) so this is a no-op.
            if process.poll() is None:
                try:
                    process.kill()
                    process.wait(timeout=5)
                except Exception:
                    logger.debug("final yt-dlp reap failed", exc_info=True)
            stdout_thread.join(timeout=2)
            wait_for_drain(2.0)
            for pipe in (process.stdout, process.stderr):
                if pipe:
                    try:
                        pipe.close()
                    except OSError:
                        pass

            # P2 audit: ``DownloadCancelledError(partial=True)`` raised
            # above picks up no resolved path — the resolver that maps
            # yt-dlp's stdout to a downloaded file runs AFTER the loop.
            # Exit is caused here, and the pipeline controller's cleanup
            # can't unlink a path it never learned. yt-dlp template uses
            # ``%(id)s-%(epoch)s-<run-token>.webm.part`` (epoch second
            # resolution), so every cancelled download leaks a
            # uniquely-named dead file: GBs of truncated VOD that a
            # re-run can't reuse, accumulating across cancelled sessions.
            #
            # Sweep ``out_dir`` for *.part / *.ytdl / *.temp fragments
            # whose name carries THIS run's token. The token filter (not
            # an mtime/snapshot heuristic) makes the sweep exact under
            # concurrency: a sibling download's fragment is never
            # touched, regardless of when it was created (audit round
            # 24 P3). Any OSError is logged at debug because an AV sweep
            # locking the partial mid-unlink surfaces here, and
            # elevating it would mask the original cancel exception.
            #
            # Failure paths only: on success the download's own fragment
            # was renamed by yt-dlp, so a sweep has nothing to reclaim.
            if not success:
                try:
                    _sweep_partial_fragments(out_dir, run_id)
                except Exception:
                    logger.debug("partial-fragment sweep failed", exc_info=True)
