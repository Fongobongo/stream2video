"""Video cutting and concatenation module using ffmpeg."""

import logging
import math
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from stream2video.silence import SilenceSegment
from stream2video.utils import (
    CANCEL_POLL_INTERVAL,
    drain_stderr_lines,
    get_video_duration,
    set_active_process,
)

logger = logging.getLogger(__name__)


class ConcatError(Exception):
    pass


class FFmpegError(ConcatError):
    pass


class CancelledError(ConcatError):
    pass


def _quote_concat_path(p: str) -> str:
    """Quote a path for ffmpeg's concat demuxer file list.

    ffmpeg's concat demuxer skips backslash sequences when finding the closing
    quote but stores them LITERALLY in the filename (verified with ffmpeg 8.1.1).
    We therefore avoid backslash escapes entirely. We pick the quote character
    not present in the path; if both are present we raise, since ffmpeg cannot
    safely represent such a path.
    """
    if "'" not in p and '"' not in p and not any(c.isspace() for c in p):
        return p
    if "'" not in p:
        return f"'{p}'"
    if '"' not in p:
        return f'"{p}"'
    raise ConcatError(f"Path contains both quote types, cannot be represented: {p}")


VIDEO_BITRATE = "7000k"
_AUDIO_BITRATE = "128k"
_BATCH_CHUNK_SIZE = 40
_BATCH_TIMEOUT = 28800
ENCODER_CHECK_TIMEOUT = 10
_FINAL_CONCAT_TIMEOUT = 86400
_SEGMENT_ENCODE_TIMEOUT = 600
_STDERR_TRUNCATE = 1000
_CANCEL_POLL_INTERVAL = CANCEL_POLL_INTERVAL

_encoder_check_cache: Dict[str, bool] = {}
_encoder_check_lock = threading.Lock()


def cut_and_concat(
    video_path: Path,
    silence_segments: List[SilenceSegment],
    output_path: Path,
    progress_callback: Optional[Callable[[float], None]] = None,
    method: str = "batch",
    encoder: str = "libx264",
    cancel_callback: Optional[Callable[[], bool]] = None,
) -> Path:
    if not video_path.exists():
        raise ConcatError(f"Input video not found: {video_path}")

    keep_segments = generate_keep_segments(video_path, silence_segments)

    if not keep_segments:
        raise ConcatError("No video segments to keep after removing silence")

    logger.info(f"Keeping {len(keep_segments)} segments, removing {len(silence_segments)} silence segments")

    vcodec, vcodec_opts = get_video_encoder(encoder)
    logger.info(f"Encoder: {vcodec} {vcodec_opts}")

    if method == "segment":
        _run_segment_with_fallback(video_path, keep_segments, output_path, vcodec, vcodec_opts,
                                   progress_callback, cancel_callback)
    elif method == "batch":
        _run_batch_concat(video_path, keep_segments, output_path, vcodec, vcodec_opts,
                          progress_callback, cancel_callback)
    else:
        raise ConcatError(f"Unknown method: {method!r} (use 'segment' or 'batch')")

    return output_path


def generate_keep_segments(
    video_path: Path,
    silence_segments: List[SilenceSegment],
) -> List[Tuple[float, float]]:
    duration = get_video_duration(video_path)
    if duration is None:
        raise ConcatError("Could not determine video duration via ffprobe")

    if duration <= 0:
        raise ConcatError(f"Invalid video duration: {duration}")

    valid = []
    for s in silence_segments:
        start = max(0.0, float(s.start))
        end = min(float(duration), float(s.end))
        if end <= start:
            continue
        if (s.start, s.end) != (start, end):
            logger.warning(
                f"Silence segment ({s.start:.2f}s - {s.end:.2f}s) "
                f"clamped to ({start:.2f}s - {end:.2f}s) to fit duration {duration:.2f}s"
            )
        valid.append((start, end))

    sorted_silences = sorted(valid, key=lambda s: s[0])
    keep_segments = []
    current_time = 0.0

    for start, end in sorted_silences:
        if current_time < start:
            keep_segments.append((current_time, start))
        current_time = max(current_time, end)

    if current_time < duration:
        keep_segments.append((current_time, duration))

    return keep_segments


ENCODER_OPTS: Dict[str, List[str]] = {
    "h264_mf": ["-b:v", VIDEO_BITRATE, "-quality", "100"],
    "h264_amf": ["-usage", "transcoding", "-quality", "speed", "-b:v", VIDEO_BITRATE],
    "h264_nvenc": ["-preset", "p7", "-rc", "vbr", "-b:v", VIDEO_BITRATE,
                   "-maxrate", VIDEO_BITRATE, "-cq", "18"],
    "libx264": ["-crf", "23", "-preset", "medium"],
}


def check_encoder(name: str) -> bool:
    """Smoke test: verify the encoder works by encoding 1 frame. Cached per process."""
    if name == "libx264":
        return True
    with _encoder_check_lock:
        if name in _encoder_check_cache:
            return _encoder_check_cache[name]
        try:
            r = subprocess.run(
                ["ffmpeg", "-y", "-v", "error",
                 "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.1",
                 "-c:v", name, "-frames:v", "1",
                 "-f", "null", "-"],
                capture_output=True, text=True, timeout=ENCODER_CHECK_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            logger.warning(f"{name} smoke test timed out after {ENCODER_CHECK_TIMEOUT}s")
            _encoder_check_cache[name] = False
            return False
        ok = r.returncode == 0
        _encoder_check_cache[name] = ok
        return ok


def get_video_encoder(preferred: str) -> Tuple[str, List[str]]:
    if preferred not in ENCODER_OPTS:
        raise ConcatError(
            f"Unknown encoder {preferred!r} (known: {', '.join(ENCODER_OPTS)})"
        )
    if check_encoder(preferred):
        return preferred, ENCODER_OPTS[preferred][:]
    logger.warning(f"{preferred} not available, falling back to libx264")
    return "libx264", ENCODER_OPTS["libx264"][:]


def _run_ffmpeg(
    cmd: List[str],
    progress_callback: Optional[Callable[[int], None]],
    timeout: int,
    label: str = "ffmpeg",
    cancel_callback: Optional[Callable[[], bool]] = None,
    track_progress: bool = True,
) -> None:
    """Run an ffmpeg command. With track_progress=True (default), parses ffmpeg's
    -progress stream from stdout and invokes progress_callback(us). With False,
    stdout is discarded — use for per-segment encodes where the segment index
    already implies progress.

    Polls cancel_callback every _CANCEL_POLL_INTERVAL seconds during the final
    wait so long-running encodes can be aborted promptly.
    """
    stdout_target = subprocess.PIPE if track_progress else subprocess.DEVNULL
    try:
        process = subprocess.Popen(
            cmd, stdout=stdout_target, stderr=subprocess.PIPE, bufsize=-1,
        )
    except FileNotFoundError as e:
        raise FFmpegError("ffmpeg not found in PATH") from e

    set_active_process(process)
    stderr_lines: List[str] = []
    cancelled = threading.Event()
    wait_for_drain = drain_stderr_lines(process.stderr, stderr_lines)
    drain_done = False

    def _cancel_monitor():
        if not cancel_callback:
            return
        while not cancelled.wait(_CANCEL_POLL_INTERVAL):
            if process.poll() is not None:
                return
            if cancel_callback():
                process.kill()
                cancelled.set()
                return

    cancel_thread = threading.Thread(target=_cancel_monitor, daemon=True)
    cancel_thread.start()

    try:
        if track_progress:
            for raw_line in iter(process.stdout.readline, b""):
                if cancel_callback and cancel_callback():
                    process.kill()
                    raise CancelledError(f"{label} cancelled")
                if cancelled.is_set():
                    raise CancelledError(f"{label} cancelled")
                line = raw_line.decode("utf-8", errors="replace").strip()
                if line.startswith("out_time_us=") and progress_callback:
                    try:
                        us = int(line.split("=", 1)[1])
                        progress_callback(us)
                    except (ValueError, IndexError):
                        pass

        if cancelled.is_set():
            raise CancelledError(f"{label} cancelled")
        _wait_with_cancel(process, timeout, cancel_callback, label)
        wait_for_drain()
        drain_done = True

        if process.returncode != 0:
            stderr_text = "".join(stderr_lines)
            msg = stderr_text[:_STDERR_TRUNCATE] if stderr_text else "unknown error (no stderr)"
            raise FFmpegError(f"{label} failed: {msg}")

    except subprocess.TimeoutExpired as e:
        process.kill()
        raise FFmpegError(f"{label} timeout after {e.timeout}s")
    finally:
        cancelled.set()
        if not drain_done:
            wait_for_drain()
        set_active_process(None)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


def _wait_with_cancel(
    process: subprocess.Popen,
    timeout: int,
    cancel_callback: Optional[Callable[[], bool]],
    label: str,
) -> int:
    """Poll process.wait() so cancel_callback is checked periodically.

    Returns the returncode, or raises CancelledError / TimeoutExpired.
    """
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout)
        try:
            return process.wait(timeout=min(_CANCEL_POLL_INTERVAL, remaining))
        except subprocess.TimeoutExpired:
            if cancel_callback and cancel_callback():
                process.kill()
                raise CancelledError(f"{label} cancelled")


def _run_segment_concat(
    video_path: Path,
    keep_segments: List[Tuple[float, float]],
    output_path: Path,
    vcodec: str,
    vcodec_opts: List[str],
    progress_callback: Optional[Callable[[float], None]] = None,
    cancel_callback: Optional[Callable[[], bool]] = None,
):
    """Single-phase: encode each segment with the target encoder, join with concat demuxer.

    One encode per segment, then stream-copy concat. No intermediate re-encode.
    Cancel is checked before each segment so long pipelines can be aborted.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    total_duration = sum(e - s for s, e in keep_segments)
    n_segs = len(keep_segments)
    logger.info(f"segment: {n_segs} segments, {total_duration:.1f}s output, {vcodec}")

    temp_dir = output_path.parent / f"_{output_path.stem}_segments"
    temp_dir.mkdir(parents=True, exist_ok=True)

    list_path: Optional[Path] = None
    encoded_keep = 0.0

    try:
        list_path = temp_dir / "concat.txt"
        with open(list_path, "w", encoding="utf-8") as lf:
            for i, (start, end) in enumerate(keep_segments):
                if cancel_callback and cancel_callback():
                    raise CancelledError("segment encode cancelled")

                dur = end - start
                seg_path = temp_dir / f"seg_{i:06d}.mp4"

                _run_ffmpeg(
                    [
                        "ffmpeg", "-y", "-loglevel", "error",
                        "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
                        "-i", str(video_path),
                        "-c:v", vcodec, *vcodec_opts,
                        "-c:a", "aac", "-b:a", _AUDIO_BITRATE,
                        str(seg_path),
                    ],
                    progress_callback=None,
                    timeout=_SEGMENT_ENCODE_TIMEOUT,
                    label=f"segment {i} encode",
                    cancel_callback=cancel_callback,
                    track_progress=False,
                )

                lf.write(f"file {_quote_concat_path(seg_path.as_posix())}\n")

                encoded_keep += dur
                if progress_callback and total_duration > 0:
                    progress_callback(min(encoded_keep / total_duration * 0.9, 0.9))

        cmd = [
            "ffmpeg", "-y", "-loglevel", "error", "-progress", "pipe:1",
            "-f", "concat", "-safe", "0", "-i", str(list_path),
            "-c", "copy", "-fflags", "+genpts",
            str(output_path),
        ]

        def _concat_prog(us: int):
            if progress_callback and total_duration > 0:
                progress_callback(min(0.9 + (us / 1_000_000 / total_duration * 0.1), 1.0))

        _run_ffmpeg(
            cmd, progress_callback=_concat_prog, timeout=_FINAL_CONCAT_TIMEOUT,
            label="segment concat", cancel_callback=cancel_callback,
        )
        logger.info(f"Successfully created output: {output_path}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _run_segment_with_fallback(
    video_path: Path,
    keep_segments: List[Tuple[float, float]],
    output_path: Path,
    primary_codec: str,
    primary_opts: List[str],
    progress_callback: Optional[Callable[[float], None]] = None,
    cancel_callback: Optional[Callable[[], bool]] = None,
):
    """Run segment concat with primary encoder; fall back to libx264 on failure."""
    def _try(enc: str, enc_opts: List[str]):
        _run_segment_concat(video_path, keep_segments, output_path, enc, enc_opts,
                            progress_callback, cancel_callback)
    _with_libx264_fallback(primary_codec, primary_opts, _try, (ConcatError, OSError))


def _build_chunked_filter_graph(
    keep_segments: List[Tuple[float, float]],
    chunk_size: int = _BATCH_CHUNK_SIZE,
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


def _run_batch_concat(
    video_path: Path,
    keep_segments: List[Tuple[float, float]],
    output_path: Path,
    vcodec: str,
    vcodec_opts: List[str],
    progress_callback: Optional[Callable[[float], None]] = None,
    cancel_callback: Optional[Callable[[], bool]] = None,
):
    """Execute ffmpeg with chunked filter_complex (select/aselect per chunk + concat)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    total_duration = sum(e - s for s, e in keep_segments)
    n_chunks = math.ceil(len(keep_segments) / _BATCH_CHUNK_SIZE)
    logger.info(f"batch: {len(keep_segments)} segments in {n_chunks} chunks, {total_duration:.1f}s output, {vcodec}")

    graph = _build_chunked_filter_graph(keep_segments, _BATCH_CHUNK_SIZE)

    script_path: Optional[str] = None
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(graph)
        f.flush()
        os.fsync(f.fileno())
        script_path = f.name

    logger.debug(f"Graph ({len(graph)} bytes, {len(keep_segments)} segs in {n_chunks} chunks)")

    def _make_cmd(enc: str, enc_opts: List[str]) -> List[str]:
        return [
            "ffmpeg", "-y", "-loglevel", "error",
            "-progress", "pipe:1",
            "-i", str(video_path),
            "-filter_complex_script", script_path,
            "-map", "[v]", "-c:v", enc, *enc_opts,
            "-map", "[a]", "-c:a", "aac", "-b:a", _AUDIO_BITRATE,
            str(output_path),
        ]

    def _prog(us: int):
        if progress_callback and total_duration > 0:
            progress_callback(min(us / 1_000_000 / total_duration, 1.0))

    def _try(enc: str, enc_opts: List[str]):
        _run_ffmpeg(
            _make_cmd(enc, enc_opts),
            progress_callback=_prog,
            timeout=_BATCH_TIMEOUT,
            label="batch",
            cancel_callback=cancel_callback,
        )
        logger.info(f"Successfully created output with {enc}")

    try:
        _with_libx264_fallback(vcodec, vcodec_opts, _try, (ConcatError, OSError))
    finally:
        if script_path is not None:
            Path(script_path).unlink(missing_ok=True)


def _with_libx264_fallback(
    primary_codec: str,
    primary_opts: List[str],
    try_fn: Callable[[str, List[str]], None],
    exc_types: Tuple[type, ...],
):
    """Run try_fn(primary_codec, primary_opts); on failure, retry once with libx264.

    try_fn must raise one of exc_types (or CancelledError, which is re-raised)
    to trigger fallback. If try_fn fails on libx264, the exception propagates.
    """
    enc, enc_opts = primary_codec, primary_opts
    while True:
        try:
            try_fn(enc, enc_opts)
            return
        except CancelledError:
            raise
        except exc_types as e:
            if enc == "libx264":
                raise
            logger.warning(f"{enc} failed: {str(e)[:200]}; falling back to libx264")
            enc, enc_opts = "libx264", ENCODER_OPTS["libx264"][:]
