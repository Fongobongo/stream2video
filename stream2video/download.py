"""Video download module using yt-dlp CLI subprocess (cancellable)."""

import logging
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, NamedTuple, Optional

from stream2video.utils import CANCEL_POLL_INTERVAL, set_active_process

logger = logging.getLogger(__name__)


class DownloadResult(NamedTuple):
    path: Path
    is_downloaded: bool


class DownloadError(Exception):
    """Base download error."""

    pass


class DownloadCancelledError(DownloadError):
    """Download was cancelled by user (not a real failure)."""

    pass


class URLValidationError(DownloadError):
    """Invalid URL format."""

    pass


class VideoNotAvailableError(DownloadError):
    """Video not available."""

    pass


class DownloadTimeoutError(DownloadError):
    """Download timeout."""

    pass


class DiskSpaceError(DownloadError):
    """Insufficient disk space."""

    pass


class PermissionDeniedError(DownloadError):
    """Permission denied."""

    pass


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


_CANCEL_POLL_INTERVAL = CANCEL_POLL_INTERVAL
_DOWNLOAD_TIMEOUT = 28800

_VIDEO_EXTENSIONS = frozenset({
    ".mp4", ".mkv", ".webm", ".mov", ".avi", ".ts",
    ".m4v", ".flv", ".wmv", ".mpg", ".mpeg", ".3gp",
})


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


def _find_downloaded_file(out_dir: Path, expected: Path) -> Optional[Path]:
    """Locate the downloaded file; fall back to glob by video id (video extensions only)."""
    if expected.exists():
        return expected
    m = re.search(r"[/\\]([\w-]+)\.\w+$", str(expected))
    if not m:
        return None
    video_id = m.group(1)
    candidates = [
        p for p in out_dir.glob(f"{video_id}.*")
        if p.suffix.lower() in _VIDEO_EXTENSIONS
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def download(
    url: str,
    out_dir: Path,
    cancel_callback: Optional[Callable[[], bool]] = None,
) -> DownloadResult:
    """
    Download video from URL via yt-dlp CLI, or pass through a local file.

    Spawns yt-dlp as a subprocess so the call can be cancelled (via cancel_callback
    or GUI close) and the OS process can be killed cleanly. The process is
    registered with set_active_process for external kill.

    Args:
        url: Video URL or local file path
        out_dir: Output directory for downloaded video
        cancel_callback: Optional callable returning True to abort

    Returns:
        DownloadResult with `path` to the file and `is_downloaded` flag

    Raises:
        URLValidationError: Invalid URL format
        VideoNotAvailableError: Video not accessible
        DownloadTimeoutError: Download timeout
        DiskSpaceError: Insufficient disk space
        PermissionDeniedError: Permission denied
        DownloadCancelledError: User cancellation (subclass of DownloadError)
        DownloadError: Generic failure
    """
    if _is_local_file(url):
        logger.info(f"Using local file: {url}")
        return DownloadResult(Path(url), is_downloaded=False)

    if not _validate_url(url):
        raise URLValidationError(f"Invalid URL: {url}")

    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--no-warnings",
        "--no-progress",
        "--output", str(out_dir / "%(id)s.%(ext)s"),
        "--format", "best[ext=mp4]/best",
        "--print", "after_move:filepath",
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
        )
    except FileNotFoundError as e:
        raise DownloadError("yt-dlp not found (install via 'pip install yt-dlp')") from e

    set_active_process(process)
    stdout_lines: list[str] = []
    stderr_chunks: list[str] = []

    def _drain_stdout():
        for line in process.stdout:
            stdout_lines.append(line.rstrip())

    def _drain_stderr():
        for line in process.stderr:
            stderr_chunks.append(line)

    stdout_thread = threading.Thread(target=_drain_stdout, daemon=True)
    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    try:
        deadline = time.monotonic() + _DOWNLOAD_TIMEOUT
        while True:
            if cancel_callback and cancel_callback():
                process.kill()
                raise DownloadCancelledError("Download cancelled by user")
            if process.poll() is not None:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                raise DownloadTimeoutError(
                    f"Download timeout after {_DOWNLOAD_TIMEOUT}s"
                )
            try:
                process.wait(timeout=min(_CANCEL_POLL_INTERVAL, remaining))
            except subprocess.TimeoutExpired:
                pass

        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)

        if process.returncode != 0:
            stderr_text = "".join(stderr_chunks)
            raise _classify_error(stderr_text) from None

        if not stdout_lines:
            stderr_text = "".join(stderr_chunks)
            raise DownloadError(
                f"yt-dlp produced no file path. stderr: {stderr_text[:300]}"
            )

        output_path = Path(stdout_lines[-1].strip())
        resolved = _find_downloaded_file(out_dir, output_path)
        if resolved is None:
            raise DownloadError(
                f"Download completed but file not found: {output_path}"
            )

        logger.info(f"Successfully downloaded: {resolved}")
        return DownloadResult(resolved, is_downloaded=True)

    finally:
        set_active_process(None)
        for pipe in (process.stdout, process.stderr):
            if pipe:
                try:
                    pipe.close()
                except OSError:
                    pass
