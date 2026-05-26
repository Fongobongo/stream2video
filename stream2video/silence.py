"""Silence detection module using ffmpeg silencedetect filter."""

import logging
import math
import re
import subprocess
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)


class SilenceDetectionError(Exception):
    """Base silence detection error."""

    pass


class SilenceSegment:
    """Silence segment representation."""

    def __init__(self, start: float, end: float):
        self.start = start
        self.end = end
        self.duration = end - start

    def __repr__(self):
        return f"SilenceSegment({self.start:.2f}s - {self.end:.2f}s, duration={self.duration:.2f}s)"


def detect_silence(
    video_path: Path,
    threshold: int = -20,
    min_silence: float = 0.5,
    margin: float = 0.1,
) -> List[SilenceSegment]:
    """
    Detect silence segments using ffmpeg silencedetect filter.

    Args:
        video_path: Path to video file
        threshold: Silence threshold in dB (default -20, range [-60, -5])
        min_silence: Minimum silence duration in seconds (default 0.5, range [0.1, 60])
        margin: Margin around silence segments in seconds (default 0.1, range [0, 5])

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

    cmd = [
        "ffmpeg",
        "-i", str(video_path),
        "-af", f"silencedetect=noise={noise}:duration={min_silence}",
        "-f", "null",
        "-",
    ]

    try:
        logger.info(f"Running ffmpeg silencedetect: threshold={threshold}dB ({noise}), min_silence={min_silence}s")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=3600,
            check=False,
        )

        if result.returncode != 0:
            error_msg = result.stderr or result.stdout or "Unknown error"
            raise SilenceDetectionError(f"ffmpeg silencedetect failed: {error_msg}")

        silence_segments = _parse_ffmpeg_output(result.stderr)

        if margin > 0:
            silence_segments = _apply_margin(silence_segments, margin)

        logger.info(f"Detected {len(silence_segments)} silence segments")
        for seg in silence_segments:
            logger.debug(f"  {seg}")

        return silence_segments

    except subprocess.TimeoutExpired as e:
        raise SilenceDetectionError(f"ffmpeg timeout after {e.timeout}s") from e

    except FileNotFoundError as e:
        raise SilenceDetectionError("ffmpeg not found in PATH") from e


def _parse_ffmpeg_output(stderr: str) -> List[SilenceSegment]:
    """Parse ffmpeg silencedetect output."""
    segments = []
    start_pattern = re.compile(r"silence_start:\s*([\d.]+)")
    end_pattern = re.compile(r"silence_end:\s*([\d.]+)")

    starts = [float(m.group(1)) for m in start_pattern.finditer(stderr)]
    ends = [float(m.group(1)) for m in end_pattern.finditer(stderr)]

    for start, end in zip(starts, ends):
        segments.append(SilenceSegment(start, end))

    return segments


def _apply_margin(segments: List[SilenceSegment], margin: float) -> List[SilenceSegment]:
    """Apply margin and merge overlapping segments. Negative margin shrinks segments."""
    if not segments:
        return segments

    expanded = []
    for seg in segments:
        start = max(0, seg.start - margin)
        end = seg.end + margin
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
