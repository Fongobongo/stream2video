"""Video download module using yt-dlp CLI subprocess (cancellable)."""

import logging
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from stream2video.utils import CANCEL_POLL_INTERVAL, no_window_kwargs, registered_process

logger = logging.getLogger(__name__)


class DownloadResult(NamedTuple):
    path: Path
    is_downloaded: bool


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
    """Download was cancelled by user (not a real failure)."""


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


def _validate_url(url: str) -> bool:
    """Validate if string is an http(s) URL.

    Bare domains (e.g. ``youtube.com/...``) are intentionally rejected so that
    typos like ``myvideo.mp4`` don't get misclassified as a URL once the
    ``_is_local_file`` check has passed (non-existent local path).
    """
    return bool(re.match(r"^https?://", url, re.IGNORECASE))


def _is_local_file(path_str: str) -> bool:
    """Check if path is local file."""
    try:
        path = Path(path_str)
        return path.is_file()
    except (OSError, ValueError):
        return False


_DOWNLOAD_TIMEOUT = 28800

# Watchdog timeouts for download connectivity / liveness. ``_DOWNLOAD_TIMEOUT``
# is the absolute ceiling for the whole download (8h, sized for big VODs);
# the watchdogs below catch the much more common failure modes where the
# connection hangs without yt-dlp ever reporting an error and the 8h
# ceiling would leave the user staring at a frozen progress bar.
#
# ``_CONNECT_TIMEOUT`` — first byte / first progress event after start.
# Covers DNS failure, TCP/TLS handshake hang, and the initial buffering
# before yt-dlp emits its first progress line. If no progress arrives
# within this window the download is killed with a clear timeout error
# instead of waiting for the 8h ceiling.
#
# ``_NO_PROGRESS_TIMEOUT`` — gap between consecutive progress events.
# If yt-dlp goes silent for this long mid-download the connection has
# almost certainly stalled (server stopped sending, route black-holed,
# etc). The 8h ceiling is far too long for this case.
#
# Both values are deliberately generous so a slow-but-alive connection
# (mobile network, saturated shared uplink) doesn't get killed
# prematurely. Tunable via env vars for users with very slow links.
_CONNECT_TIMEOUT = 300  # 5 min — DNS+handshake+first byte
_NO_PROGRESS_TIMEOUT = 1800  # 30 min — mid-download stall

# yt-dlp format selectors by quality preset.
#
# ``best`` historically used ``best[ext=mp4]/best`` — a single pre-merged
# file. On YouTube that often picks a 720p pre-merged stream even when a
# 1080p video-only stream exists, and it forfeits the better audio codec
# (Opus vs AAC) that the separate audio stream offers. The new default
# mirrors the other presets: ``bestvideo+bestaudio`` first (yt-dlp merges
# with ffmpeg when needed), with a pre-merged file as the fallback so a
# site without separate tracks still works. See P1.2 in the fix plan.
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
    # Old-format line (pre-progress.* template, 4 fields) — fall back to
    # the historical layout ``downloaded|total_estimate|speed|eta`` so a
    # partial download from an earlier yt-dlp version still parses. The
    # first run after upgrade emits the new 5-field format and this
    # branch stops firing. Previously this branch was dead (the condition
    # ``if len(parts) < 5 and len(parts) < 4`` was equivalent to ``< 4``),
    # and even if it fired the field mapping was wrong
    # (total_estimate←speed, speed←eta). See P1 audit v0.3 §5.1.
    if len(parts) < 4:
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
    if len(parts) == 4:
        # Legacy 4-field line: downloaded|total_estimate|speed|eta.
        total = None
        total_estimate = _f(parts[1])
        speed = _f(parts[2])
        eta = _f(parts[3])
    else:
        # Current 5-field line: downloaded|total|total_estimate|speed|eta.
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
    if "permission denied" in msg or "errno 13" in msg:
        return PermissionDeniedError("Permission denied")
    return DownloadError(f"Download failed: {stderr[:500]}")


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
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _stderr_snippet(stderr_chunks: list[str], limit: int = 300) -> str:
    text = "".join(stderr_chunks).strip()
    return text[-limit:] if text else ""


def _timeout_error(message: str, stderr_chunks: list[str]) -> DownloadTimeoutError:
    snippet = _stderr_snippet(stderr_chunks)
    if snippet:
        return DownloadTimeoutError(f"{message}. stderr: {snippet}")
    return DownloadTimeoutError(message)


def _resolve_reported_download_path(out_dir: Path, stdout_lines: list[str]) -> tuple[Path | None, Path | None]:
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


def download(
    url: str,
    out_dir: Path,
    cancel_callback: Callable[[], bool] | None = None,
    quality: str = "best",
    progress_callback: Callable[[DownloadProgress], None] | None = None,
    download_timeout: int = _DOWNLOAD_TIMEOUT,
    connect_timeout: int = _CONNECT_TIMEOUT,
    no_progress_timeout: int = _NO_PROGRESS_TIMEOUT,
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
        size = path.stat().st_size
        logger.info(f"Using local file: {url} ({size // 1024 // 1024} MB)")
        return DownloadResult(path, is_downloaded=False)

    if not _validate_url(url):
        raise URLValidationError(f"Invalid URL: {url}")

    out_dir.mkdir(parents=True, exist_ok=True)

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
        str(out_dir / "%(id)s.%(ext)s"),
        "--format",
        format_str,
        "--print",
        "after_move:filepath",
        url,
    ]

    logger.info(f"Downloading: {url}")
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            **no_window_kwargs(),
        )
    except FileNotFoundError as e:
        raise DownloadError("yt-dlp not found (install via 'pip install yt-dlp')") from e

    with registered_process(process):
        stdout_lines: list[str] = []
        stderr_chunks: list[str] = []

        # Watchdog state. ``last_progress_time`` is updated by the stdout
        # drain thread each time a parseable progress line arrives; the main
        # loop checks it against ``_CONNECT_TIMEOUT`` (before first progress)
        # and ``_NO_PROGRESS_TIMEOUT`` (mid-download). A non-None value also
        # tells the UI layer that yt-dlp is actually pushing bytes, not just
        # sitting on an idle connection.
        #
        # The list container is a Python-mutable-cell workaround for the
        # closure capturing ``last_progress_time`` by reference in a nested
        # function — direct assignment would create a local binding in the
        # drain thread and the main loop wouldn't see updates.
        last_progress_time: list[float | None] = [None]
        last_activity_time: list[float] = [time.monotonic()]
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
                last_activity_time[0] = time.monotonic()
                prog = _parse_progress_line(text)
                if prog is not None:
                    last_progress_time[0] = time.monotonic()
                    if progress_callback is not None:
                        try:
                            progress_callback(prog)
                        except Exception:
                            # A callback crash must not break the download —
                            # progress is best-effort UI feedback, not a hard
                            # signal. Log and continue.
                            logger.debug("progress_callback raised", exc_info=True)
                    continue
                stdout_lines.append(text)

        def _drain_stderr() -> None:
            stderr = process.stderr
            if stderr is None:
                return
            for line in stderr:
                last_activity_time[0] = time.monotonic()
                stderr_chunks.append(line)

        stdout_thread = threading.Thread(target=_drain_stdout, daemon=True)
        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stdout_thread.start()
        stderr_thread.start()

        try:
            deadline = time.monotonic() + download_timeout
            while True:
                if process.poll() is not None:
                    break
                if cancel_callback and cancel_callback():
                    process.kill()
                    process.wait()
                    raise DownloadCancelledError("Download cancelled by user")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    process.kill()
                    process.wait()
                    stdout_thread.join(timeout=2)
                    stderr_thread.join(timeout=2)
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
                    idle_for = now - max(start_time, last_activity_time[0])
                    if idle_for > connect_timeout:
                        process.kill()
                        process.wait()
                        stdout_thread.join(timeout=2)
                        stderr_thread.join(timeout=2)
                        raise _timeout_error(
                            f"Download stalled before first byte: no progress "
                            f"and no yt-dlp activity within {connect_timeout}s "
                            "(DNS/TLS/handshake?)",
                            stderr_chunks,
                        )
                else:
                    silent_for = now - (last_progress_time[0] or now)
                    if silent_for > no_progress_timeout:
                        process.kill()
                        process.wait()
                        stdout_thread.join(timeout=2)
                        stderr_thread.join(timeout=2)
                        raise _timeout_error(
                            f"Download stalled: no progress for {int(silent_for)}s",
                            stderr_chunks,
                        )
                try:
                    process.wait(timeout=min(CANCEL_POLL_INTERVAL, remaining))
                except subprocess.TimeoutExpired:
                    pass

            stdout_thread.join(timeout=2)
            stderr_thread.join(timeout=2)

            if process.returncode != 0:
                stderr_text = "".join(stderr_chunks)
                raise _classify_error(stderr_text) from None

            if not stdout_lines:
                stderr_text = "".join(stderr_chunks)
                raise DownloadError(f"yt-dlp produced no file path. stderr: {stderr_text[:300]}")

            resolved, reported_path = _resolve_reported_download_path(out_dir, stdout_lines)
            if resolved is None:
                raise DownloadError(f"Download completed but file not found: {reported_path}")

            size = resolved.stat().st_size
            if size == 0:
                raise DownloadError(f"Download completed but file is empty: {resolved}")

            logger.info(f"Successfully downloaded: {resolved} ({size // 1024 // 1024} MB)")
            return DownloadResult(resolved, is_downloaded=True)

        finally:
            # Join drain threads in the finally so cancel/timeout/early-raise
            # paths still wait for them. Threads exit when their pipes close
            # (next block), so the join is bounded; a missed join could leak
            # the daemon thread's pipe reads until process exit on Windows.
            stdout_thread.join(timeout=2)
            stderr_thread.join(timeout=2)
            for pipe in (process.stdout, process.stderr):
                if pipe:
                    try:
                        pipe.close()
                    except OSError:
                        pass
