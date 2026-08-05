"""ffprobe-based validity helpers for resume-skip logic."""

import logging
import subprocess
from pathlib import Path

from stream2video.tools import ffprobe_path, run_with_retry
from stream2video.utils import no_window_kwargs

logger = logging.getLogger(__name__)


def _ffprobe_is_valid_media(path: Path, stream_type: str = "v") -> bool:
    """Quick validity check: ffprobe can read codec + duration for the
    requested stream type.

    Used by resume-skip to reject a chunk that exists and is large enough
    but is internally corrupt (e.g. ffmpeg crashed mid-write and the
    moov atom is missing). Without this, the concat demuxer would accept
    the file but emit a broken segment in the middle of the output.

    ``stream_type`` selects the ffprobe ``-select_streams`` filter: ``"v"``
    for video segments (the historical default, used by the concat
    segment/cut/raw paths) and ``"a"`` for audio segments (audio-extract
    resume — an audio-only file has no video stream and would otherwise
    fail video validation → resume always re-encoded everything, see
    the P0 audit in the v0.3 release plan).
    """
    try:
        r = run_with_retry(
            [
                ffprobe_path(),
                "-v",
                "error",
                "-select_streams",
                stream_type,
                "-show_entries",
                "stream=codec_name",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            **no_window_kwargs(),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return r.returncode == 0 and bool(r.stdout.strip())


# Back-compat alias for the old name; new call sites should use
# _ffprobe_is_valid_media(path, stream_type=...). Kept so external
# search/grep across older branches doesn't report a dangling reference.
def _ffprobe_is_valid_mp4(path: Path) -> bool:
    return _ffprobe_is_valid_media(path, stream_type="v")


def _ffprobe_duration_ok(path: Path, expected_seconds: float, *, slack: float = 1.0) -> bool:
    """Check that a resume part's ffprobe duration is close to the expected value.

    ffmpeg killed mid-write can leave a valid moov atom (the file passes
    ``_ffprobe_is_valid_media``) but a truncated body — the duration read
    from the moov reflects the planned length, not the actual content. Comparing
    against the expected duration catches holes in the middle of the final
    video. ``slack`` is the tolerance in seconds; 1.0s covers encoder flush
    jitter and ffmpeg's own rounding without accepting truncated outputs.

    When ffprobe cannot determine the duration (corrupt file, timeout,
    non-media data), returns ``True`` — the caller's existing
    ``_ffprobe_is_valid_media`` codec check already gatekeeps those cases,
    and we don't want to double-reject a file whose codec is fine but
    whose duration is unreadable for unrelated reasons.
    """
    try:
        r = run_with_retry(
            [
                ffprobe_path(),
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            **no_window_kwargs(),
        )
        if r.returncode != 0:
            return True  # duration unreadable — fall back to codec check alone
        duration_str = r.stdout.strip()
        if not duration_str:
            return True
        actual = float(duration_str)
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        return True  # duration unreadable — fall back to codec check alone
    return abs(actual - expected_seconds) <= slack
