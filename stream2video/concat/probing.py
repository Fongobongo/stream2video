"""ffprobe-based validity helpers for resume-skip logic."""

import contextlib
import logging
import math
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from stream2video.concat.errors import CancelledError
from stream2video.tools import ffmpeg_path, ffprobe_path, popen_with_retry, run_with_retry
from stream2video.utils import drain_stderr_lines, no_window_kwargs

logger = logging.getLogger(__name__)

# stderr markers a decode treats as failure even when ffmpeg exits 0
# (a truncated payload warns "partial file" and still returns rc=0 —
# observed live, audit round 28 P1).
_TRUNCATION_MARKERS = (
    "partial file",
    "moov atom not found",
    "packet corrupt",
    "invalid data",
    "error while decoding",
    "truncated",
)


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


def _ffprobe_media_complete(path: Path, stream_type: str = "v") -> bool:
    """Strict freshness check for a file about to be PUBLISHED as the
    stable source (audit round 27 P2): ``_ffprobe_is_valid_media``
    proves only that the requested stream has a readable codec name —
    a file with a valid header but a truncated/corrupt body passes it,
    and the publish would then atomically replace the previous good
    copy with garbage. This probe additionally requires a finite,
    non-zero container duration, which a truncated body cannot supply
    (ffprobe reads the duration from the actual media, not from a
    header field alone).

    Fail-closed like its sibling: spawn faults, timeouts and
    unparseable output all return False — the caller must then refuse
    to publish. Exceptions of the "ffprobe cannot run at all" kind
    (transient spawn failure) are NOT swallowed here: they re-raise so
    the caller can distinguish "invalid media" from "validation
    unavailable" and must not delete the download (audit round 27 P1).
    """
    r = run_with_retry(
        [
            ffprobe_path(),
            "-v",
            "error",
            "-select_streams",
            stream_type,
            "-show_entries",
            "stream=codec_name:format=duration",
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
        return False
    lines = [line.strip() for line in r.stdout.splitlines() if line.strip()]
    if not lines or not lines[0]:
        return False
    duration: float | None = None
    for line in lines[1:]:
        try:
            value = float(line)
        except ValueError:
            continue
        if math.isfinite(value) and value > 0:
            duration = value
            break
    return duration is not None


def _ffmpeg_decode_probe(path: Path, at_seconds: float, stream_type: str) -> bool:
    """Decode ONE second of ``path`` (video or audio stream) at
    ``at_seconds`` into the null sink.

    A truncated body can still carry a header with a PLANNED duration
    (audit round 28 P1): ffprobe metadata alone cannot tell a
    header-only file from a complete one, so the publish gate decodes
    the head AND (when the duration is known) the tail — a body hole
    makes the decode fail. Fail-closed like the other probes; spawn
    faults re-raise so the caller keeps the file and reports
    "validation unavailable".

    Production no longer uses this (the publish gate and resume checks
    run the whole-stream decode instead — audit round 29 P3/P4); kept
    as a tested diagnostic helper.
    """
    r = run_with_retry(
        [
            ffmpeg_path(),
            "-v",
            "error",
            "-ss",
            f"{max(0.0, at_seconds):.2f}",
            "-i",
            str(path),
            "-map",
            "0:v:0" if stream_type == "v" else "0:a:0",
            "-t",
            "1",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        **no_window_kwargs(),
    )
    if r.returncode != 0:
        return False
    stderr = (r.stderr or "").lower()
    return not any(marker in stderr for marker in _TRUNCATION_MARKERS)


def _ffprobe_stream_duration(path: Path, stream_type: str) -> float | None:
    """Duration of ONE stream type (v/a), or None when unreadable.

    Container duration cannot detect an audio track truncated much
    earlier than the video (audit round 30 P5): the stream-level
    comparison between video and audio durations is what catches a
    12 s video carrying a 2 s audio track.
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
                "stream=duration",
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
            return None
        value = float(r.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _ffmpeg_full_decode(
    path: Path,
    stream_type: str = "v",
    timeout: float = 43200,
    cancel_callback: "Callable[[], bool] | None" = None,
) -> bool:
    """Decode the WHOLE requested stream into the null sink.

    Head+tail probes cannot prove body integrity: zeroing a 256-byte
    window in the MIDDLE of an MP4 leaves the header, head decode and
    tail decode all clean (reproduced live, audit round 29 P3/P4). A
    full decode is the only check that reads every packet, so it is
    used by the fresh-download publish gate, every resume-reuse
    decision and the final-output validation.

    Hardened for long media (audit round 30 P7/P8):

      * ``-xerror`` — stop at the FIRST decode error instead of
        printing a per-packet error storm from a damaged multi-hour
        file;
      * stderr goes through the shared ring-bounded drain
        (``drain_stderr_lines``), never ``capture_output`` — the
        integrity check itself must not be the OOM vector;
      * the wait loop polls ``cancel_callback`` — a user Cancel kills
        ffmpeg immediately (raises ``CancelledError``) instead of
        holding the pipeline for up to the 12 h ceiling;
      * ``timeout`` is the caller's config (phase timeout), bounded
        by a hard ceiling.

    Spawn faults, timeout and cancellations re-raise: "validation
    unavailable" must never be mistaken for "invalid media".
    """
    cmd = [
        ffmpeg_path(),
        "-v",
        "error",
        "-xerror",
        "-i",
        str(path),
        "-map",
        "0:v:0" if stream_type == "v" else "0:a:0",
        "-f",
        "null",
        "-",
    ]
    proc = popen_with_retry(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **no_window_kwargs(),
    )
    stderr_chunks: list[str] = []
    assert proc.stderr is not None
    wait_for_drain = drain_stderr_lines(proc.stderr, stderr_chunks)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                proc.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                pass
            if cancel_callback is not None and cancel_callback():
                with contextlib.suppress(OSError):
                    proc.kill()
                raise CancelledError("media validation cancelled")
            if time.monotonic() >= deadline:
                with contextlib.suppress(OSError):
                    proc.kill()
                raise subprocess.TimeoutExpired(cmd, timeout)
    finally:
        wait_for_drain(2.0)
    if proc.returncode != 0:
        return False
    stderr = "".join(stderr_chunks).lower()
    return not any(marker in stderr for marker in _TRUNCATION_MARKERS)


def _ffprobe_duration_ok(path: Path, expected_seconds: float, *, slack: float = 1.0) -> bool:
    """Check that a resume part's ffprobe duration is close to the expected value.

    ffmpeg killed mid-write can leave a valid moov atom (the file passes
    ``_ffprobe_is_valid_media``) but a truncated body — the duration read
    from the moov reflects the planned length, not the actual content. Comparing
    against the expected duration catches holes in the middle of the final
    video. ``slack`` is the tolerance in seconds; 1.0s covers encoder flush
    jitter and ffmpeg's own rounding without accepting truncated outputs.

    When ffprobe cannot determine the duration (corrupt file, timeout,
    non-media data), returns ``False`` — fail-closed so a resume part
    whose integrity cannot be verified is re-encoded instead of silently
    accepted. The historical behaviour returned ``True`` (deferring to the
    caller's codec check), which let a truncated-but-readable resume part
    pass the integrity gate and inject a hole into the final output.
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
            return False  # duration unreadable — do not trust the part
        duration_str = r.stdout.strip()
        if not duration_str:
            return False
        actual = float(duration_str)
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        return False  # duration unreadable — do not trust the part
    return abs(actual - expected_seconds) <= slack
