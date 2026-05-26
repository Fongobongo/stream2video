"""Video cutting and concatenation module using ffmpeg."""

import logging
import subprocess
import tempfile
from pathlib import Path
from typing import List, Tuple, Optional

logger = logging.getLogger(__name__)


class ConcatError(Exception):
    """Base concatenation error."""

    pass


class FFmpegError(ConcatError):
    """FFmpeg execution error."""

    pass


def cut_and_concat(
    video_path: Path,
    silence_segments: List,  # List[SilenceSegment]
    output_path: Path,
) -> Path:
    """
    Cut out silence segments and concatenate remaining video.

    Args:
        video_path: Path to input video
        silence_segments: List of SilenceSegment objects to remove
        output_path: Path to output video file

    Returns:
        Path to output video file

    Raises:
        FFmpegError: If ffmpeg fails
    """
    if not video_path.exists():
        raise ConcatError(f"Input video not found: {video_path}")

    # Generate keep segments (inverse of silence)
    keep_segments = _generate_keep_segments(video_path, silence_segments)

    if not keep_segments:
        raise ConcatError("No video segments to keep after removing silence")

    logger.info(f"Keeping {len(keep_segments)} segments, removing {len(silence_segments)} silence segments")

    # Create concat file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        concat_file = Path(f.name)
        _write_concat_file(concat_file, video_path, keep_segments)

    try:
        # Run ffmpeg to concatenate
        _run_ffmpeg_concat(concat_file, output_path)

        logger.info(f"Successfully created output video: {output_path}")
        return output_path

    finally:
        concat_file.unlink(missing_ok=True)


def _get_video_duration(video_path: Path) -> float:
    """Get video duration in seconds."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1:noprint_indexes=1",
        str(video_path),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=True)
        return float(result.stdout.strip())
    except (subprocess.CalledProcessError, ValueError, FileNotFoundError) as e:
        logger.warning(f"Could not determine video duration: {e}")
        return None


def _generate_keep_segments(video_path: Path, silence_segments: List) -> List[Tuple[float, float]]:
    """
    Generate segments to keep (inverse of silence segments).

    Args:
        video_path: Path to video
        silence_segments: List of silence segments to remove

    Returns:
        List of (start, end) tuples for segments to keep
    """
    duration = _get_video_duration(video_path)

    if duration is None:
        logger.warning("Could not determine duration, will use silence segments as-is")
        return [(0, duration)] if duration else []

    # Sort silence segments by start time
    sorted_silences = sorted(silence_segments, key=lambda s: s.start)

    keep_segments = []
    current_time = 0.0

    for silence in sorted_silences:
        if current_time < silence.start:
            keep_segments.append((current_time, silence.start))

        current_time = silence.end

    # Add remaining segment
    if current_time < duration:
        keep_segments.append((current_time, duration))

    return keep_segments


def _write_concat_file(concat_file: Path, video_path: Path, keep_segments: List[Tuple[float, float]]):
    """Write ffmpeg concat demuxer file."""
    with open(concat_file, "w") as f:
        for start, end in keep_segments:
            # Create filter_complex for precise cutting
            f.write(f"file '{video_path}'\n")
            f.write(f"inpoint {start}\n")
            f.write(f"outpoint {end}\n")


def _run_ffmpeg_concat(concat_file: Path, output_path: Path):
    """Run ffmpeg with concat demuxer."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Use filter approach for more reliable concatenation
    cmd = [
        "ffmpeg",
        "-y",  # Overwrite output file
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_file),
        "-c",
        "copy",  # Copy streams without re-encoding
        str(output_path),
    ]

    try:
        logger.info(f"Running ffmpeg: {' '.join(cmd[:5])}...")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=3600,  # 1 hour timeout
            check=False,
        )

        if result.returncode != 0:
            error_msg = result.stderr or result.stdout or "Unknown error"
            logger.error(f"ffmpeg failed: {error_msg}")
            raise FFmpegError(f"ffmpeg failed: {error_msg}")

    except subprocess.TimeoutExpired as e:
        raise FFmpegError(f"ffmpeg timeout after {e.timeout}s") from e

    except FileNotFoundError as e:
        raise FFmpegError("ffmpeg not found in PATH") from e
