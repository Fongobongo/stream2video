"""Video cutting and concatenation module using ffmpeg."""

import logging
import math
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Callable, List, Optional, Tuple

logger = logging.getLogger(__name__)


class ConcatError(Exception):
    """Base concatenation error."""
    pass


class FFmpegError(ConcatError):
    """FFmpeg execution error."""
    pass


VIDEO_BITRATE = "7000k"
_CHUNK_SIZE = 90  # max inputs per final concat filter (to stay under Windows 8191 char cmdline)


def cut_and_concat(
    video_path: Path,
    silence_segments: List,
    output_path: Path,
    progress_callback: Optional[Callable[[float], None]] = None,
    method: str = "segment",
    encoder: str = "libx264",
) -> Path:
    """Cut out silence using the selected method and encoder."""
    if not video_path.exists():
        raise ConcatError(f"Input video not found: {video_path}")

    keep_segments = generate_keep_segments(video_path, silence_segments)

    if not keep_segments:
        raise ConcatError("No video segments to keep after removing silence")

    logger.info(f"Keeping {len(keep_segments)} segments, removing {len(silence_segments)} silence segments")

    vcodec, vcodec_opts = get_video_encoder(encoder)
    logger.info(f"Encoder: {vcodec} {vcodec_opts}")

    if method == "segment":
        _run_ffmpeg_segment_concat(video_path, keep_segments, output_path, vcodec, vcodec_opts, progress_callback)
    elif method == "batch":
        _run_ffmpeg_batch_concat(video_path, keep_segments, output_path, vcodec, vcodec_opts, progress_callback)
    else:
        raise ConcatError(f"Unknown method: {method!r} (use 'segment' or 'batch')")

    return output_path


def get_video_duration(video_path: Path) -> Optional[float]:
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


def generate_keep_segments(video_path: Path, silence_segments: List) -> List[Tuple[float, float]]:
    """Generate segments to keep (inverse of silence segments)."""
    duration = get_video_duration(video_path)
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


ENCODER_OPTS: dict = {
    "h264_mf": ["-b:v", VIDEO_BITRATE, "-quality", "100"],
    "h264_amf": ["-usage", "transcoding", "-quality", "speed", "-b:v", VIDEO_BITRATE],
    "h264_nvenc": ["-preset", "p7", "-rc", "vbr", "-b:v", VIDEO_BITRATE, "-maxrate", VIDEO_BITRATE, "-cq", "18"],
    "libx264": ["-crf", "23", "-preset", "medium"],
}

MAX_ENCODE_RETRIES = 5


def _start_stderr_reader(process) -> Tuple[List[str], threading.Thread]:
    """Start a daemon thread that collects stderr output."""
    stderr_lines: List[str] = []
    def _reader():
        for raw_line in iter(process.stderr.readline, b""):
            try:
                stderr_lines.append(raw_line.decode("utf-8", errors="replace"))
            except Exception:
                pass
    thread = threading.Thread(target=_reader, daemon=True)
    thread.start()
    return stderr_lines, thread


def check_encoder(name: str) -> bool:
    """Smoke test: verify the encoder works on this GPU by encoding 1 frame."""
    if name == "libx264":
        return True
    r = subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.1",
         "-c:v", name, "-frames:v", "1",
         "-f", "null", "-"],
        capture_output=True, text=True, timeout=10,
    )
    return r.returncode == 0


def get_video_encoder(preferred: str) -> Tuple[str, List[str]]:
    """Verify the requested encoder is available; fallback to libx264 if not.

    Args:
        preferred: "h264_nvenc", "h264_amf", "h264_mf", or "libx264".
    """
    if check_encoder(preferred):
        return preferred, ENCODER_OPTS[preferred][:]
    logger.warning(f"{preferred} not available, falling back to libx264")
    return "libx264", ENCODER_OPTS["libx264"][:]


# ── segment method (per-segment libx264 + TS byte concat + h264_mf final) ──

def _run_ffmpeg_segment_concat(
    video_path: Path,
    keep_segments: List[Tuple[float, float]],
    output_path: Path,
    vcodec: str,
    vcodec_opts: List[str],
    progress_callback: Optional[Callable[[float], None]] = None,
):
    """Two-phase concat:
    1. Per-segment libx264 CRF 18 + TS byte concat chunks + remux MP4
    2. Chunk concat FILTER → {vcodec}
    Avoids intermediate re-encode (byte concat is lossless)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    total_duration = sum(e - s for s, e in keep_segments)
    n_segs = len(keep_segments)
    n_chunks = math.ceil(n_segs / _CHUNK_SIZE)

    logger.info(f"per-segment: {n_segs} segments in {n_chunks} chunks, {total_duration:.1f}s output, {vcodec}")

    temp_dir = output_path.parent / f"_{output_path.stem}_segments"
    temp_dir.mkdir(parents=True, exist_ok=True)

    fsp: Optional[str] = None

    try:
        logger.info(f"Phase 1: encoding {n_segs} segments and building chunks")
        encoded_keep = 0.0

        chunk_ts_buf: List[Path] = []
        chunk_mp4s: List[Path] = []

        for i, (start, end) in enumerate(keep_segments):
            dur = end - start
            seg_mp4 = temp_dir / f"seg_{i:06d}.mp4"
            seg_ts = temp_dir / f"seg_{i:06d}.ts"

            r = subprocess.run([
                "ffmpeg", "-y", "-loglevel", "error",
                "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
                "-i", str(video_path),
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
                "-c:a", "aac", "-b:a", "128k",
                str(seg_mp4),
            ], capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                raise FFmpegError(f"segment {i} encode failed: {r.stderr[:200]}")

            r2 = subprocess.run([
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", str(seg_mp4),
                "-c", "copy", "-f", "mpegts",
                str(seg_ts),
            ], capture_output=True, text=True, timeout=30)
            if r2.returncode != 0:
                raise FFmpegError(f"segment {i} TS conversion failed: {r2.stderr[:200]}")

            seg_mp4.unlink(missing_ok=True)
            chunk_ts_buf.append(seg_ts)

            if len(chunk_ts_buf) >= _CHUNK_SIZE or i == n_segs - 1:
                ch_idx = len(chunk_mp4s)
                chunk_ts = temp_dir / f"chunk_{ch_idx:03d}.ts"
                chunk_mp4 = temp_dir / f"chunk_{ch_idx:03d}.mp4"
                chunk_mp4s.append(chunk_mp4)

                with open(chunk_ts, "wb") as out:
                    for ts_file in chunk_ts_buf:
                        out.write(ts_file.read_bytes())

                for ts_file in chunk_ts_buf:
                    ts_file.unlink(missing_ok=True)
                chunk_ts_buf.clear()

                r3 = subprocess.run([
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-i", str(chunk_ts),
                    "-c", "copy", "-fflags", "+genpts",
                    str(chunk_mp4),
                ], capture_output=True, text=True, timeout=60)
                if r3.returncode != 0:
                    raise FFmpegError(f"chunk {ch_idx} remux failed: {r3.stderr[:200]}")

                chunk_ts.unlink(missing_ok=True)

            encoded_keep += dur
            if progress_callback and total_duration > 0:
                progress_callback(min(encoded_keep / total_duration * 0.6, 0.6))

        logger.info(f"Phase 2: concat FILTER of {len(chunk_mp4s)} chunks → {vcodec}")

        chunk_filter = "".join(f"[{i}:v][{i}:a]" for i in range(len(chunk_mp4s)))
        chunk_filter += f"concat=n={len(chunk_mp4s)}:v=1:a=1[v][a]"

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write(chunk_filter)
            fsp = f.name

        inputs = []
        for cf in chunk_mp4s:
            inputs.extend(["-i", str(cf)])

        # Retry loop: up to MAX_ENCODE_RETRIES attempts on selected encoder → libx264 fallback
        retry_limit = MAX_ENCODE_RETRIES if vcodec != "libx264" else 1

        last_error: Optional[Exception] = None

        for attempt in range(retry_limit):
            enc, enc_opts = (vcodec, vcodec_opts)
            logger.info(f"Encode attempt {attempt + 1}/{retry_limit} with {enc}")

            cmd = ["ffmpeg", "-y", "-loglevel", "error", "-progress", "pipe:1"] + inputs + [
                "-filter_complex_script", fsp,
                "-map", "[v]", "-map", "[a]",
                "-c:v", enc, *enc_opts,
                "-c:a", "aac", "-b:a", "128k",
                str(output_path),
            ]

            try:
                process = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=-1,
                )
            except FileNotFoundError as e:
                raise FFmpegError("ffmpeg not found in PATH") from e

            stderr_lines, stderr_thread = _start_stderr_reader(process)

            try:
                for raw_line in iter(process.stdout.readline, b""):
                    try:
                        line = raw_line.decode("utf-8", errors="replace").strip()
                    except Exception:
                        continue
                    if line.startswith("out_time_us="):
                        try:
                            us = int(line.split("=", 1)[1])
                            if progress_callback and total_duration > 0:
                                progress_callback(min(0.6 + (us / 1_000_000 / total_duration * 0.4), 1.0))
                        except (ValueError, IndexError):
                            pass

                process.wait(timeout=86400)
                stderr_thread.join(timeout=5)

                if process.returncode != 0:
                    stderr_text = "".join(stderr_lines)
                    raise FFmpegError(f"final concat ({enc}) failed: {stderr_text[:1000] or 'unknown error'}")

                logger.info(f"Successfully created output: {output_path}")
                last_error = None
                break

            except subprocess.TimeoutExpired as e:
                process.kill()
                last_error = FFmpegError(f"final concat ({enc}) timeout after {e.timeout}s")
            finally:
                process.stdout.close()
                process.stderr.close()

            if attempt < retry_limit - 1 and last_error:
                logger.warning(f"Attempt {attempt + 1} failed, retrying...")

        # libx264 fallback
        if last_error and vcodec != "libx264":
            logger.warning(f"All {retry_limit} attempts failed, falling back to libx264")
            enc, enc_opts = "libx264", ENCODER_OPTS["libx264"][:]
            cmd = ["ffmpeg", "-y", "-loglevel", "error", "-progress", "pipe:1"] + inputs + [
                "-filter_complex_script", fsp,
                "-map", "[v]", "-map", "[a]",
                "-c:v", enc, *enc_opts,
                "-c:a", "aac", "-b:a", "128k",
                str(output_path),
            ]
            try:
                process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=-1)
            except FileNotFoundError:
                raise FFmpegError("ffmpeg not found in PATH")

            stderr_lines, stderr_thread = _start_stderr_reader(process)
            try:
                for raw_line in iter(process.stdout.readline, b""):
                    try:
                        line = raw_line.decode("utf-8", errors="replace").strip()
                    except Exception:
                        continue
                    if line.startswith("out_time_us="):
                        try:
                            us = int(line.split("=", 1)[1])
                            if progress_callback and total_duration > 0:
                                progress_callback(min(0.6 + (us / 1_000_000 / total_duration * 0.4), 1.0))
                        except (ValueError, IndexError):
                            pass
                process.wait(timeout=86400)
                stderr_thread.join(timeout=5)
                if process.returncode != 0:
                    stderr_text = "".join(stderr_lines)
                    raise FFmpegError(f"libx264 fallback failed: {stderr_text[:1000] or 'unknown error'}")
                logger.info(f"Successfully created output: {output_path}")
                last_error = None
            except subprocess.TimeoutExpired as e:
                process.kill()
                raise FFmpegError(f"libx264 fallback timeout after {e.timeout}s")
            finally:
                process.stdout.close()
                process.stderr.close()

        if last_error:
            raise last_error

    finally:
        if fsp is not None:
            Path(fsp).unlink(missing_ok=True)
        for p in temp_dir.glob("*"):
            p.unlink(missing_ok=True)
        if temp_dir.exists():
            try:
                temp_dir.rmdir()
            except OSError:
                pass


# ── batch method (select/aselect filter_complex with adjusted timestamps) ──

def _build_chunked_filter_graph(
    keep_segments: List[Tuple[float, float]],
    chunk_size: int = 40,
) -> str:
    """Build filter_complex graph: select/aselect chunks + concat."""
    chunks = [keep_segments[i:i+chunk_size] for i in range(0, len(keep_segments), chunk_size)]
    n = len(chunks)
    lines = []
    for idx, chunk in enumerate(chunks):
        terms = "+".join(f"between(t,{s},{e})" for s, e in chunk)
        lines.append(f"[0:v]select='{terms}',setpts=N/FRAME_RATE/TB[v{idx}];")
        lines.append(f"[0:a]aselect='{terms}',asetpts=N/SR/TB[a{idx}];")
    labels = "".join(f"[v{i}][a{i}]" for i in range(n))
    lines.append(f"{labels}concat=n={n}:v=1:a=1[v][a]")
    return "\n".join(lines)


def _run_ffmpeg_batch_concat(
    video_path: Path,
    keep_segments: List[Tuple[float, float]],
    output_path: Path,
    vcodec: str,
    vcodec_opts: List[str],
    progress_callback: Optional[Callable[[float], None]] = None,
):
    """Execute ffmpeg with chunked filter_complex (select/aselect per chunk + concat)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    total_duration = sum(e - s for s, e in keep_segments)

    chunk_size = 40
    n_chunks = math.ceil(len(keep_segments) / chunk_size)
    logger.info(f"batch: {len(keep_segments)} segments in {n_chunks} chunks, {total_duration:.1f}s output, {vcodec}")

    graph = _build_chunked_filter_graph(keep_segments, chunk_size)

    script_path: Optional[str] = None
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(graph)
        script_path = f.name

    logger.debug(f"Graph ({len(graph)} bytes, {len(keep_segments)} segs in {n_chunks} chunks)")

    retry_limit = MAX_ENCODE_RETRIES if vcodec != "libx264" else 1
    last_error: Optional[Exception] = None

    try:
        for attempt in range(retry_limit):
            enc, enc_opts = (vcodec, vcodec_opts)
            logger.info(f"Encode attempt {attempt + 1}/{retry_limit} with {enc}")

            cmd = [
                "ffmpeg", "-y", "-loglevel", "error",
                "-progress", "pipe:1",
                "-i", str(video_path),
                "-filter_complex_script", script_path,
                "-map", "[v]", "-c:v", enc, *enc_opts,
                "-map", "[a]", "-c:a", "aac", "-b:a", "128k",
                str(output_path),
            ]

            try:
                process = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=-1,
                )
            except FileNotFoundError as e:
                raise FFmpegError("ffmpeg not found in PATH") from e

            stderr_lines, stderr_thread = _start_stderr_reader(process)

            try:
                for raw_line in iter(process.stdout.readline, b""):
                    try:
                        line = raw_line.decode("utf-8", errors="replace").strip()
                    except Exception:
                        continue
                    if line.startswith("out_time_us="):
                        try:
                            us = int(line.split("=", 1)[1])
                            if progress_callback and total_duration > 0:
                                progress_callback(min(us / 1_000_000 / total_duration, 1.0))
                        except (ValueError, IndexError):
                            pass

                process.wait(timeout=28800)
                stderr_thread.join(timeout=5)

                if process.returncode != 0:
                    stderr_text = "".join(stderr_lines)
                    raise FFmpegError(f"batch ({enc}) failed: {stderr_text[:1000] or 'unknown error'}")

                logger.info(f"Successfully created output: {output_path}")
                last_error = None
                break

            except subprocess.TimeoutExpired as e:
                process.kill()
                last_error = FFmpegError(f"batch ({enc}) timeout after {e.timeout}s")
            finally:
                process.stdout.close()
                process.stderr.close()

            if attempt < retry_limit - 1 and last_error:
                logger.warning(f"Attempt {attempt + 1} failed, retrying...")

        # libx264 fallback
        if last_error and vcodec != "libx264":
            logger.warning(f"All {retry_limit} attempts failed, falling back to libx264")
            enc, enc_opts = "libx264", ENCODER_OPTS["libx264"][:]
            cmd = [
                "ffmpeg", "-y", "-loglevel", "error",
                "-progress", "pipe:1",
                "-i", str(video_path),
                "-filter_complex_script", script_path,
                "-map", "[v]", "-c:v", enc, *enc_opts,
                "-map", "[a]", "-c:a", "aac", "-b:a", "128k",
                str(output_path),
            ]
            try:
                process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=-1)
            except FileNotFoundError:
                raise FFmpegError("ffmpeg not found in PATH")

            stderr_lines, stderr_thread = _start_stderr_reader(process)
            try:
                for raw_line in iter(process.stdout.readline, b""):
                    try:
                        line = raw_line.decode("utf-8", errors="replace").strip()
                    except Exception:
                        continue
                    if line.startswith("out_time_us="):
                        try:
                            us = int(line.split("=", 1)[1])
                            if progress_callback and total_duration > 0:
                                progress_callback(min(us / 1_000_000 / total_duration, 1.0))
                        except (ValueError, IndexError):
                            pass
                process.wait(timeout=28800)
                stderr_thread.join(timeout=5)
                if process.returncode != 0:
                    stderr_text = "".join(stderr_lines)
                    raise FFmpegError(f"libx264 fallback failed: {stderr_text[:1000] or 'unknown error'}")
                logger.info(f"Successfully created output: {output_path}")
                last_error = None
            except subprocess.TimeoutExpired as e:
                process.kill()
                raise FFmpegError(f"libx264 fallback timeout after {e.timeout}s")
            finally:
                process.stdout.close()
                process.stderr.close()

        if last_error:
            raise last_error
    finally:
        if script_path is not None:
            Path(script_path).unlink(missing_ok=True)
