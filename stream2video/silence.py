"""Silence detection module using ffmpeg silencedetect filter."""

import json
import logging
import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Callable, List, Optional

from stream2video.utils import (
    drain_stderr_lines,
    get_video_duration as _probe_duration,
    no_window_kwargs,
    set_active_process,
)

logger = logging.getLogger(__name__)


class SilenceDetectionError(Exception):
    """Base silence detection error."""

    pass


class SilenceCancelledError(SilenceDetectionError):
    """Silence detection was cancelled by user (not a real failure)."""

    pass


class SilenceSegment:
    """Silence segment representation."""

    def __init__(self, start: float, end: float):
        self.start = start
        self.end = end
        self.duration = max(0.0, end - start)

    def __repr__(self):
        return f"SilenceSegment({self.start:.2f}s - {self.end:.2f}s, duration={self.duration:.2f}s)"


_SILENCE_POLL_INTERVAL = 0.5
_SILENCE_TIMEOUT = 36000


def detect_silence(
    video_path: Path,
    threshold: float = -20,
    min_silence: float = 0.5,
    margin: float = -0.5,
    progress_callback: Optional[Callable[[float], None]] = None,
    cancel_callback: Optional[Callable[[], bool]] = None,
) -> List[SilenceSegment]:
    """
    Detect silence segments using ffmpeg silencedetect filter.

    Args:
        video_path: Path to video file
        threshold: Silence threshold in dB (default -20, range [-60, -5])
        min_silence: Minimum silence duration in seconds (default 0.5, range [0.1, 60])
        margin: How much to shrink silence zones in seconds (default 0.5, range [-3, 5]).
                Positive = shrink silence (keep more audio around phrases).
                Negative = expand silence (cut more aggressively).
                0 = no adjustment.
        progress_callback: Optional callback with progress fraction [0, 1]
        cancel_callback: Optional callable returning True to abort; checked while ffmpeg runs.

    Returns:
        List of SilenceSegment objects
    """
    if not video_path.exists():
        raise SilenceDetectionError(f"Video file not found: {video_path}")

    if not -60 <= threshold <= -5:
        raise ValueError(f"Threshold must be in range [-60, -5], got {threshold}")

    if not 0.1 <= min_silence <= 60:
        raise ValueError(f"Min silence must be in range [0.1, 60], got {min_silence}")

    if not -3 <= margin <= 5:
        raise ValueError(f"Margin must be in range [-3, 5], got {margin}")

    # Convert dB threshold to linear noise level
    noise = 10 ** (threshold / 20)

    # Get video duration for progress reporting
    duration = _probe_duration(video_path)

    cmd = [
        "ffmpeg",
        "-progress", "pipe:1",
        "-i", str(video_path),
        "-af", f"silencedetect=noise={noise}:duration={min_silence}",
        "-f", "null",
        "-",
    ]

    try:
        logger.info(
            f"Running ffmpeg silencedetect: threshold={threshold}dB ({noise}), "
            f"min_silence={min_silence}s, margin={margin}s"
        )

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=-1,
            **no_window_kwargs(),
        )
    except FileNotFoundError as e:
        raise SilenceDetectionError("ffmpeg not found in PATH") from e

    set_active_process(process)
    stderr_lines: List[str] = []
    cancelled = threading.Event()
    wait_for_drain = drain_stderr_lines(process.stderr, stderr_lines)
    drain_done = False

    def _cancel_monitor():
        if not cancel_callback:
            return
        while not cancelled.wait(_SILENCE_POLL_INTERVAL):
            if process.poll() is not None:
                return
            if cancel_callback():
                process.kill()
                cancelled.set()
                return

    cancel_thread = threading.Thread(target=_cancel_monitor, daemon=True)
    cancel_thread.start()

    try:
        for raw_line in iter(process.stdout.readline, b""):
            line = raw_line.decode("utf-8", errors="replace").strip()
            if line.startswith("out_time_us="):
                try:
                    us = int(line.split("=", 1)[1])
                    if progress_callback and duration and duration > 0:
                        progress_callback(min(us / 1_000_000 / duration, 1.0))
                except (ValueError, IndexError):
                    pass
            if cancelled.is_set():
                raise SilenceCancelledError("silence detection cancelled")

        if cancelled.is_set():
            raise SilenceCancelledError("silence detection cancelled")

        process.wait(timeout=_SILENCE_TIMEOUT)
        wait_for_drain()
        drain_done = True

        if process.returncode != 0:
            stderr_text = "".join(stderr_lines)
            error_msg = stderr_text or "Unknown error"
            raise SilenceDetectionError(f"ffmpeg silencedetect failed: {error_msg}")

        silence_segments = _parse_ffmpeg_output("".join(stderr_lines))
        silence_segments = _apply_margin(silence_segments, margin)

        if not silence_segments:
            logger.info("No silence segments detected (video may have no audio track)")

        return silence_segments

    except subprocess.TimeoutExpired as e:
        process.kill()
        raise SilenceDetectionError(f"ffmpeg timeout after {e.timeout}s") from e
    finally:
        if not drain_done:
            wait_for_drain()
        cancelled.set()
        cancel_thread.join(timeout=1)
        set_active_process(None)
        process.stdout.close()
        process.stderr.close()


def _parse_ffmpeg_output(stderr: str) -> List[SilenceSegment]:
    """Parse ffmpeg silencedetect output."""
    segments = []
    # Match an integer or a single-decimal (dot or comma) number, not malformed "1.2.3"
    num = r"\d+(?:[.,]\d+)?"
    start_pattern = re.compile(rf"silence_start:\s*({num})")
    end_pattern = re.compile(rf"silence_end:\s*({num})")

    def _to_float(s: str) -> float:
        return float(s.replace(",", "."))

    starts = [_to_float(m.group(1)) for m in start_pattern.finditer(stderr)]
    ends = [_to_float(m.group(1)) for m in end_pattern.finditer(stderr)]

    if len(starts) != len(ends):
        if len(starts) > len(ends):
            dropped = len(starts) - len(ends)
            dropped_kind = "unmatched silence_start (no silence_end)"
        else:
            dropped = len(ends) - len(starts)
            dropped_kind = "unmatched silence_end (no silence_start)"
        logger.warning(
            f"Mismatched silence_start ({len(starts)}) and silence_end ({len(ends)}) counts; "
            f"dropping {dropped} {dropped_kind} — ffmpeg output may be truncated"
        )

    for start, end in zip(starts, ends):
        segments.append(SilenceSegment(start, end))

    return segments


def _apply_margin(segments: List[SilenceSegment], margin: float) -> List[SilenceSegment]:
    """Apply margin and merge overlapping segments.

    Positive margin shrinks silence (keep more audio around phrases).
    Negative margin expands silence (remove more audio around phrases).
    """
    if not segments:
        return segments

    expanded = []
    for seg in segments:
        start = max(0, seg.start + margin)
        end = seg.end - margin
        if start < end:
            expanded.append(SilenceSegment(start, end))

    if not expanded:
        return expanded

    expanded.sort(key=lambda s: s.start)

    merged = []
    current = expanded[0]

    for seg in expanded[1:]:
        if seg.start <= current.end:
            current = SilenceSegment(current.start, max(current.end, seg.end))
        else:
            merged.append(current)
            current = seg

    merged.append(current)
    return merged


def _get_cache_path(video_path: Path, output_dir: Path) -> Path:
    return output_dir / f"{video_path.stem}_silence_cache.json"


def save_silence_cache(
    video_path: Path,
    segments: List[SilenceSegment],
    output_dir: Path,
    config: dict,
):
    cache_path = _get_cache_path(video_path, output_dir)
    data = {
        "source": video_path.name,
        "config": {
            "threshold": config.get("threshold"),
            "min_silence": config.get("min_silence"),
            "margin": config.get("margin"),
        },
        "segments": [{"start": s.start, "end": s.end} for s in segments],
    }
    fd, tmp_path = tempfile.mkstemp(
        dir=output_dir, prefix=f".{cache_path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, cache_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    logger.info(f"Silence cache saved to {cache_path}")


def load_silence_cache(
    video_path: Path,
    output_dir: Path,
    config: dict,
) -> Optional[List[SilenceSegment]]:
    cache_path = _get_cache_path(video_path, output_dir)
    if not cache_path.exists():
        return None
    if cache_path.stat().st_mtime < video_path.stat().st_mtime:
        logger.info("Silence cache outdated (source file newer)")
        return None
    try:
        with open(cache_path) as f:
            data = json.load(f)
        for key in ("threshold", "min_silence", "margin"):
            if data.get("config", {}).get(key) != config.get(key):
                logger.info(f"Silence cache ignored: config mismatch ({key})")
                return None
        segments = [SilenceSegment(s["start"], s["end"]) for s in data["segments"]]
        logger.info(f"Loaded {len(segments)} silence segments from cache")
        return segments
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.warning(f"Invalid silence cache: {e}")
        return None
