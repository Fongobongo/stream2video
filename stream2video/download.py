"""Video download module using yt-dlp."""

import re
import time
import logging
from pathlib import Path
from typing import Optional

import yt_dlp

logger = logging.getLogger(__name__)


class DownloadError(Exception):
    """Base download error."""

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
    """Validate if string is a URL."""
    url_pattern = r"^https?://|^[a-z]+\.[a-z]+"
    return bool(re.match(url_pattern, url, re.IGNORECASE))


def _is_local_file(path_str: str) -> bool:
    """Check if path is local file."""
    try:
        path = Path(path_str)
        return path.is_file()
    except (OSError, ValueError):
        return False


def download(url: str, out_dir: Path) -> Path:
    """
    Download video from URL using yt-dlp.

    Args:
        url: Video URL or local file path
        out_dir: Output directory for downloaded video

    Returns:
        Path to downloaded/local video file

    Raises:
        URLValidationError: Invalid URL format
        VideoNotAvailableError: Video not accessible
        DownloadTimeoutError: Download timeout
        DiskSpaceError: Insufficient disk space
        PermissionDeniedError: Permission denied
    """
    # Check if input is local file
    if _is_local_file(url):
        logger.info(f"Using local file: {url}")
        return Path(url)

    # Validate URL
    if not _validate_url(url):
        raise URLValidationError(f"Invalid URL: {url}")

    # Ensure output directory exists
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # yt-dlp configuration
    ydl_opts = {
        "format": "best[ext=mp4]/best",
        "outtmpl": str(out_dir / "%(id)s.%(ext)s"),
        "quiet": False,
        "no_warnings": False,
        "socket_timeout": 30,
    }

    max_retries = 1
    retry_backoff = 5

    for attempt in range(max_retries + 1):
        try:
            logger.info(f"Downloading video: {url} (attempt {attempt + 1}/{max_retries + 1})")

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
                output_path = Path(filename)

                if not output_path.exists():
                    raise DownloadError(f"Download completed but file not found: {filename}")

                logger.info(f"Successfully downloaded: {output_path}")
                return output_path

        except yt_dlp.utils.DownloadError as e:
            error_msg = str(e)

            if "Video unavailable" in error_msg or "not available" in error_msg.lower():
                raise VideoNotAvailableError(f"Video not available: {url}") from e

            if "No space left" in error_msg or "disk full" in error_msg.lower():
                raise DiskSpaceError(f"Insufficient disk space in {out_dir}") from e

            if "Permission denied" in error_msg:
                raise PermissionDeniedError(f"Permission denied accessing {url}") from e

            if attempt < max_retries:
                logger.warning(f"Download error (will retry): {error_msg}")
                time.sleep(retry_backoff)
                continue

            raise DownloadError(f"Download failed: {error_msg}") from e

        except yt_dlp.utils.ExtractorError as e:
            error_msg = str(e)

            if "not available" in error_msg.lower() or "removed" in error_msg.lower():
                raise VideoNotAvailableError(f"Video not available: {url}") from e

            if attempt < max_retries:
                logger.warning(f"Extractor error (will retry): {error_msg}")
                time.sleep(retry_backoff)
                continue

            raise DownloadError(f"Extractor error: {error_msg}") from e

        except (TimeoutError, OSError) as e:
            if isinstance(e, OSError) and e.errno == 28:  # ENOSPC
                raise DiskSpaceError(f"Insufficient disk space in {out_dir}") from e

            if isinstance(e, OSError) and e.errno == 13:  # EACCES
                raise PermissionDeniedError(f"Permission denied accessing {out_dir}") from e

            if attempt < max_retries:
                logger.warning(f"Network timeout (will retry in {retry_backoff}s): {e}")
                time.sleep(retry_backoff)
                continue

            raise DownloadTimeoutError(f"Download timeout after {max_retries + 1} attempts") from e

    # Should not reach here
    raise DownloadError("Download failed: maximum retries exceeded")
