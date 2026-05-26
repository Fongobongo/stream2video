"""Video cutting and concatenation module using ffmpeg."""

import logging
import subprocess
import tempfile
import re
from pathlib import Path
from typing import List, Tuple, Optional

logger = logging.getLogger(__name__)

KEYFRAME_SEARCH_WINDOW = 5.0  # Max seconds to search backward for a keyframe


class ConcatError(Exception):
    """Base concatenation error."""
    pass


class FFmpegError(ConcatError):
    """FFmpeg execution error."""
    pass


def cut_and_concat(
    video_path: Path,
    silence_segments: List,
    output_path: Path,
) -> Path:
    """
    Cut out silence segments and concatenate remaining video.
    Aligns cut points to video keyframes to prevent pixelation artifacts.
    Filters out tiny keep segments to avoid cut-off speech.
    """
    if not video_path.exists():
        raise ConcatError(f"Input video not found: {video_path}")

    keep_segments = _generate_keep_segments(video_path, silence_segments)

    if not keep_segments:
        raise ConcatError("No video segments to keep after removing silence")

    # Align segment boundaries to keyframes to prevent pixelation at splice points
    keyframes = _find_keyframes(video_path)
    keep_segments = _align_to_keyframes(keep_segments, keyframes)

    logger.info(f"Keeping {len(keep_segments)} segments (after alignment/filtering), removing {len(silence_segments)} silence segments")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        concat_file = Path(f.name)
        _write_concat_file(concat_file, video_path, keep_segments)

    try:
        _run_ffmpeg_concat(concat_file, output_path)
        logger.info(f"Successfully created output video: {output_path}")
        return output_path
    finally:
        concat_file.unlink(missing_ok=True)


def _get_video_duration(video_path: Path) -> Optional[float]:
    """Get video duration in seconds."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=True)
        return float(result.stdout.strip())
    except (subprocess.CalledProcessError, ValueError, FileNotFoundError) as e:
        logger.warning(f"Could not determine video duration: {e}")
        return None


def _find_keyframes(video_path: Path) -> List[float]:
    """Estimate keyframe positions from GOP-size (fast, no full scan)."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=gop_size,r_frame_rate",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=True)
        lines = result.stdout.strip().split("\n")
        gop_size = None
        fps = None

        for line in lines:
            line = line.strip()
            if line and gop_size is None:
                try:
                    gop_size = int(float(line))
                except ValueError:
                    pass
            elif line and fps is None:
                try:
                    parts = line.split("/")
                    if len(parts) == 2:
                        fps = float(parts[0]) / float(parts[1])
                except (ValueError, ZeroDivisionError):
                    pass

        if gop_size is None or gop_size <= 0:
            logger.debug("No valid GOP-size found, skipping keyframe alignment")
            return []

        if fps is None or fps <= 0:
            fps = 30.0

        duration = _get_video_duration(video_path)
        if duration is None or duration <= 0:
            return []

        interval = gop_size / fps
        count = int(duration / interval) + 1
        keyframes = [i * interval for i in range(count)]
        logger.debug(f"Estimated {len(keyframes)} keyframes (GOP={gop_size}, fps={fps:.2f}, interval={interval:.2f}s)")
        return keyframes

    except (subprocess.CalledProcessError, FileNotFoundError, ValueError) as e:
        logger.warning(f"Could not estimate keyframes via GOP-size: {e}")
        return []


def _align_to_keyframes(
    segments: List[Tuple[float, float]],
    keyframes: List[float],
) -> List[Tuple[float, float]]:
    """
    Snap segment starts to the nearest preceding keyframe.
    Ensures concat demuxer output starts cleanly at a keyframe.
    """
    if not keyframes:
        return segments

    kf_index = 0
    aligned = []

    for start, end in segments:
        if start == 0.0:
            aligned.append((start, end))
            continue

        new_start = start
        while kf_index < len(keyframes) and keyframes[kf_index] <= start:
            if start - keyframes[kf_index] <= KEYFRAME_SEARCH_WINDOW:
                new_start = keyframes[kf_index]
            kf_index += 1

        if kf_index > 0 and new_start == start:
            prev_kf = keyframes[kf_index - 1]
            if start - prev_kf <= KEYFRAME_SEARCH_WINDOW:
                new_start = prev_kf

        # Prevent overlap with previous segment (keyframe before prev end)
        if aligned and new_start < aligned[-1][1]:
            new_start = start

        if new_start < end:
            aligned.append((new_start, end))

    return aligned


def _generate_keep_segments(video_path: Path, silence_segments: List) -> List[Tuple[float, float]]:
    """Generate segments to keep (inverse of silence segments)."""
    duration = _get_video_duration(video_path)
    if duration is None:
        raise ConcatError("Could not determine video duration via ffprobe")

    sorted_silences = sorted(silence_segments, key=lambda s: s.start)
    keep_segments = []
    current_time = 0.0

    for silence in sorted_silences:
        if current_time < silence.start:
            keep_segments.append((current_time, silence.start))
        current_time = silence.end

    if current_time < duration:
        keep_segments.append((current_time, duration))

    return keep_segments


def _write_concat_file(concat_file: Path, video_path: Path, keep_segments: List[Tuple[float, float]]):
    """Write ffmpeg concat demuxer file."""
    with open(concat_file, "w") as f:
        for start, end in keep_segments:
            f.write(f"file '{video_path}'\n")
            f.write(f"inpoint {start}\n")
            f.write(f"outpoint {end}\n")


def _run_ffmpeg_concat(concat_file: Path, output_path: Path):
    """Run ffmpeg with concat demuxer."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_file),
        "-c", "copy",
        "-fflags", "+genpts",
        "-copyts",
        str(output_path),
    ]

    try:
        logger.info(f"Running ffmpeg: {' '.join(cmd[:5])}...")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=3600,
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
