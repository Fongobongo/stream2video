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


VIDEO_BITRATE = "7000k"


def cut_and_concat(
    video_path: Path,
    silence_segments: List,
    output_path: Path,
) -> Path:
    """Cut out silence using filter_complex select/aselect, re-encode both streams."""
    if not video_path.exists():
        raise ConcatError(f"Input video not found: {video_path}")

    keep_segments = _generate_keep_segments(video_path, silence_segments)

    if not keep_segments:
        raise ConcatError("No video segments to keep after removing silence")

    logger.info(f"Keeping {len(keep_segments)} segments, removing {len(silence_segments)} silence segments")

    _run_ffmpeg_filter_complex(video_path, keep_segments, output_path)
    return output_path


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


def _get_video_encoder() -> Tuple[str, List[str]]:
    """Check available h264 encoders, prefer h264_mf with quality opts."""
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"],
        capture_output=True, text=True, timeout=10,
    )
    if "h264_mf" in r.stdout:
        return "h264_mf", ["-b:v", VIDEO_BITRATE, "-quality", "100"]
    logger.info("h264_mf not found, falling back to libx264")
    return "libx264", ["-crf", "23", "-preset", "medium"]


def _run_ffmpeg_filter_complex(
    video_path: Path,
    keep_segments: List[Tuple[float, float]],
    output_path: Path,
):
    """Execute ffmpeg with filter_complex select/aselect, re-encode both streams.
    Uses -filter_complex_script to avoid Windows command-line length limits."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    expr_parts = [f"between(t,{s},{e})" for s, e in keep_segments]
    select_expr = "+".join(expr_parts)

    vcodec, vcodec_opts = _get_video_encoder()

    graph = (
        f"[0:v]select='{select_expr}',setpts=N/FRAME_RATE/TB[v];\n"
        f"[0:a]aselect='{select_expr}',asetpts=N/SR/TB[a]"
    )

    logger.info(f"filter_complex: {len(keep_segments)} segments, {vcodec}")

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as f:
        f.write(graph)
        script_path = f.name

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error", "-stats",
        "-i", str(video_path),
        "-filter_complex_script", script_path,
        "-map", "[v]", "-c:v", vcodec, *vcodec_opts,
        "-map", "[a]", "-c:a", "aac", "-b:a", "128k",
        str(output_path),
    ]

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=28800, check=False)
        if r.returncode != 0:
            raise FFmpegError(f"ffmpeg failed: {r.stderr.strip()[:1000] or 'unknown error'}")
        logger.info(f"Successfully created output: {output_path}")
    except subprocess.TimeoutExpired as e:
        raise FFmpegError(f"ffmpeg timeout after {e.timeout}s") from e
    except FileNotFoundError as e:
        raise FFmpegError("ffmpeg not found in PATH") from e
    finally:
        Path(script_path).unlink(missing_ok=True)
