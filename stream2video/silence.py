"""Silence detection module using auto-editor."""

import json
import logging
import subprocess
import re
from pathlib import Path
from typing import List, Tuple, Optional

logger = logging.getLogger(__name__)


class SilenceDetectionError(Exception):
    """Base silence detection error."""

    pass


class AutoEditorError(SilenceDetectionError):
    """Auto-editor execution error."""

    pass


class SilenceSegment:
    """Silence segment representation."""

    def __init__(self, start: float, end: float):
        """
        Initialize silence segment.

        Args:
            start: Start time in seconds
            end: End time in seconds
        """
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
    Detect silence segments in video using auto-editor.

    Args:
        video_path: Path to video file
        threshold: Silence threshold in dB (default -20, range [-60, -5])
        min_silence: Minimum silence duration in seconds (default 0.5, range [0.1, 60])
        margin: Margin around silence segments in seconds (default 0.1, range [0, 5])

    Returns:
        List of SilenceSegment objects

    Raises:
        AutoEditorError: If auto-editor fails
    """
    if not video_path.exists():
        raise SilenceDetectionError(f"Video file not found: {video_path}")

    # Validate parameters
    if not -60 <= threshold <= -5:
        raise ValueError(f"Threshold must be in range [-60, -5], got {threshold}")

    if not 0.1 <= min_silence <= 60:
        raise ValueError(f"Min silence must be in range [0.1, 60], got {min_silence}")

    if not 0 <= margin <= 5:
        raise ValueError(f"Margin must be in range [0, 5], got {margin}")

    # Build auto-editor command
    # auto-editor uses: --threshold (in dB), --min_clip_length (in seconds)
    cmd = [
        "auto-editor",
        str(video_path),
        "--output",
        "/dev/null",  # Don't save output video
        "--export",
        "json",  # Export as JSON for parsing
        "--threshold",
        str(threshold),
        "--min_clip_length",
        str(min_silence),
    ]

    try:
        logger.info(f"Running auto-editor: {' '.join(cmd)}")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,  # 10 minutes timeout
            check=False,
        )

        if result.returncode != 0:
            error_msg = result.stderr or result.stdout or "Unknown error"
            logger.error(f"auto-editor failed: {error_msg}")
            raise AutoEditorError(f"auto-editor failed: {error_msg}")

        # Parse output
        silence_segments = _parse_auto_editor_output(result.stdout, result.stderr)

        # Apply margin
        if margin > 0:
            silence_segments = _apply_margin(silence_segments, margin)

        logger.info(f"Detected {len(silence_segments)} silence segments")
        for seg in silence_segments:
            logger.debug(f"  {seg}")

        return silence_segments

    except subprocess.TimeoutExpired as e:
        raise AutoEditorError(f"auto-editor timeout after {e.timeout}s") from e

    except FileNotFoundError as e:
        raise AutoEditorError("auto-editor not found in PATH. Install with: pip install auto-editor") from e


def _parse_auto_editor_output(stdout: str, stderr: str) -> List[SilenceSegment]:
    """
    Parse auto-editor output to extract silence segments.

    Args:
        stdout: Standard output from auto-editor
        stderr: Standard error from auto-editor

    Returns:
        List of SilenceSegment objects
    """
    segments = []

    # Try JSON parsing first
    output_lines = stdout.strip().split("\n")

    for line in output_lines:
        if not line.strip():
            continue

        try:
            # Try parsing as JSON
            data = json.loads(line)
            if isinstance(data, dict):
                # Extract silence segments from JSON
                if "silence" in data:
                    silence_list = data["silence"]
                    if isinstance(silence_list, list):
                        for silence in silence_list:
                            if isinstance(silence, (list, tuple)) and len(silence) >= 2:
                                start, end = float(silence[0]), float(silence[1])
                                segments.append(SilenceSegment(start, end))
            elif isinstance(data, list):
                # Direct list format
                if len(data) >= 2:
                    try:
                        start, end = float(data[0]), float(data[1])
                        segments.append(SilenceSegment(start, end))
                    except (ValueError, TypeError):
                        pass

        except json.JSONDecodeError:
            # Fallback: try regex parsing for common formats
            # Match patterns like: (0.5, 2.5), [0.5-2.5], 0.5-2.5
            for match in re.finditer(r"[(\[]?\s*(\d+\.?\d*)\s*[-,]\s*(\d+\.?\d*)\s*[)\]]?", line):
                try:
                    start, end = float(match.group(1)), float(match.group(2))
                    if start < end:
                        segments.append(SilenceSegment(start, end))
                except ValueError:
                    pass

    if segments:
        logger.debug(f"Parsed {len(segments)} silence segments from auto-editor output")

    return segments


def _apply_margin(segments: List[SilenceSegment], margin: float) -> List[SilenceSegment]:
    """
    Apply margin to silence segments and merge overlapping segments.

    Args:
        segments: Original silence segments
        margin: Margin to apply in seconds

    Returns:
        Adjusted and merged silence segments
    """
    if not segments:
        return segments

    # Expand each segment by margin
    expanded = [SilenceSegment(max(0, seg.start - margin), seg.end + margin) for seg in segments]

    # Sort by start time
    expanded.sort(key=lambda s: s.start)

    # Merge overlapping segments
    merged = []
    current = expanded[0]

    for seg in expanded[1:]:
        if seg.start <= current.end:
            # Overlapping or adjacent - merge
            current = SilenceSegment(current.start, max(current.end, seg.end))
        else:
            # Gap - save current and start new
            merged.append(current)
            current = seg

    merged.append(current)

    return merged
