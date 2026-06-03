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
    no_window_kwargs,
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
_STALL_WARNING = 120
_STALL_KILL = 300
_HYBRID_SEEK_OFFSET = 0.5
_AUDIO_PAD = 0.1  # extra seconds to let AAC encoder flush its lookahead buffer

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
        _run_batch_with_fallback(video_path, keep_segments, output_path, vcodec, vcodec_opts,
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
                **no_window_kwargs(),
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
            **no_window_kwargs(),
        )
    except FileNotFoundError as e:
        raise FFmpegError("ffmpeg not found in PATH") from e

    set_active_process(process)
    stderr_lines: List[str] = []
    cancelled = threading.Event()
    wait_for_drain = drain_stderr_lines(process.stderr, stderr_lines)
    drain_done = False
    last_progress_time = time.monotonic()

    def _cancel_monitor():
        nonlocal last_progress_time
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
                        last_progress_time = time.monotonic()
                    except (ValueError, IndexError):
                        pass
                elapsed_since_progress = time.monotonic() - last_progress_time
                if elapsed_since_progress > _STALL_KILL:
                    process.kill()
                    raise FFmpegError(
                        f"{label} stalled — no progress for {int(elapsed_since_progress)}s, "
                        "possible resource exhaustion"
                    )
                elif elapsed_since_progress > _STALL_WARNING:
                    logger.warning(
                        f"{label}: no progress for {int(elapsed_since_progress)}s — "
                        "waiting..."
                    )

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
    """Encode each segment, join with concat demuxer.

    Segments are stored in a dedicated subdirectory.  If a previous run was
    interrupted, already-encoded segments are reused (resume from where it
    stopped).  On success all segment files are deleted.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    total_duration = sum(e - s for s, e in keep_segments)
    n_segs = len(keep_segments)
    logger.info(f"segment: {n_segs} segments, {total_duration:.1f}s output, {vcodec}")

    seg_dir = output_path.parent / f"_{output_path.stem}_segments"
    seg_dir.mkdir(parents=True, exist_ok=True)

    encoded_keep = 0.0
    skipped = 0

    try:
        for i, (start, end) in enumerate(keep_segments):
            if cancel_callback and cancel_callback():
                raise CancelledError("segment encode cancelled")

            dur = end - start
            seg_path = seg_dir / f"seg_{i:06d}.mp4"

            # Resume: skip already encoded segments
            if seg_path.exists() and seg_path.stat().st_size > 0:
                skipped += 1
                encoded_keep += dur
                if progress_callback and total_duration > 0:
                    progress_callback(min(encoded_keep / total_duration * 0.9, 0.9))
                continue

            # Hybrid seek: input -ss for fast keyframe seek, output -ss for exact position
            seek_before = max(0.0, start - _HYBRID_SEEK_OFFSET)
            seek_after = min(_HYBRID_SEEK_OFFSET, start)

            _run_ffmpeg(
                [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-ss", f"{seek_before:.3f}",
                    "-i", str(video_path),
                    "-ss", f"{seek_after:.3f}",
                    "-af", "apad",
                    "-t", f"{dur + _AUDIO_PAD:.3f}",
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

            encoded_keep += dur
            if progress_callback and total_duration > 0:
                progress_callback(min(encoded_keep / total_duration * 0.9, 0.9))

        if skipped:
            logger.info(f"segment: resumed {skipped}/{n_segs} already encoded, encoded {n_segs - skipped}")

        # Build concat list
        list_path = seg_dir / "concat.txt"
        with open(list_path, "w", encoding="utf-8") as lf:
            for i in range(n_segs):
                sp = seg_dir / f"seg_{i:06d}.mp4"
                lf.write(f"file {_quote_concat_path(sp.name)}\n")

        def _concat_prog(us: int):
            if progress_callback and total_duration > 0:
                progress_callback(min(0.9 + (us / 1_000_000 / total_duration * 0.1), 1.0))

        _run_ffmpeg(
            [
                "ffmpeg", "-y", "-loglevel", "error", "-progress", "pipe:1",
                "-f", "concat", "-safe", "0", "-i", str(list_path),
                "-c", "copy", "-fflags", "+genpts",
                str(output_path),
            ],
            progress_callback=_concat_prog, timeout=_FINAL_CONCAT_TIMEOUT,
            label="segment concat", cancel_callback=cancel_callback,
        )
        logger.info(f"Successfully created output: {output_path}")

        # Cleanup on success
        shutil.rmtree(seg_dir, ignore_errors=True)

    except Exception:
        # On failure: keep segments for resume
        logger.info(f"Segments kept in {seg_dir} for resume on next run")
        raise


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


def _run_batch_with_fallback(
    video_path: Path,
    keep_segments: List[Tuple[float, float]],
    output_path: Path,
    primary_codec: str,
    primary_opts: List[str],
    progress_callback: Optional[Callable[[float], None]] = None,
    cancel_callback: Optional[Callable[[], bool]] = None,
):
    """Run batch concat with primary encoder; fall back to libx264 on failure."""
    def _try(enc: str, enc_opts: List[str]):
        _run_batch_concat(video_path, keep_segments, output_path, enc, enc_opts,
                          progress_callback, cancel_callback)
    _with_libx264_fallback(primary_codec, primary_opts, _try, (ConcatError, OSError))


def _run_batch_concat(
    video_path: Path,
    keep_segments: List[Tuple[float, float]],
    output_path: Path,
    vcodec: str,
    vcodec_opts: List[str],
    progress_callback: Optional[Callable[[float], None]] = None,
    cancel_callback: Optional[Callable[[], bool]] = None,
):
    """Process chunks sequentially: each chunk → temp file, then concat.

    Previous approach built one giant filter graph with all chunks, causing
    ffmpeg to decode the entire video for every select/aselect filter in
    parallel — O(chunks × filesize) RAM.  This version processes one chunk
    at a time so ffmpeg only holds ~1 chunk worth of decoded frames.

    Supports resume: already-encoded chunks are skipped on re-run.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    total_duration = sum(e - s for s, e in keep_segments)
    chunks = [keep_segments[i:i+_BATCH_CHUNK_SIZE]
              for i in range(0, len(keep_segments), _BATCH_CHUNK_SIZE)]
    n_chunks = len(chunks)
    logger.info(
        f"batch: {len(keep_segments)} segments in {n_chunks} chunks, "
        f"{total_duration:.1f}s output, {vcodec}"
    )

    batch_dir = output_path.parent / f"_{output_path.stem}_batch"
    batch_dir.mkdir(parents=True, exist_ok=True)

    try:
        encoded_duration = 0.0
        skipped = 0

        for ci, chunk in enumerate(chunks):
            if cancel_callback and cancel_callback():
                raise CancelledError("batch encode cancelled")

            chunk_path = batch_dir / f"chunk_{ci:04d}.mp4"

            # Resume: skip already encoded chunks
            if chunk_path.exists() and chunk_path.stat().st_size > 0:
                skipped += 1
                encoded_duration += sum(e - s for s, e in chunk)
                if progress_callback and total_duration > 0:
                    progress_callback(min(encoded_duration / total_duration, 0.9))
                continue

            terms = "+".join(f"between(t,{s},{e})" for s, e in chunk)
            graph = (
                f"[0:v]select='{terms}',setpts=N/FRAME_RATE/TB[v];"
                f"[0:a]aselect='{terms}',asetpts=N/SR/TB[a];"
                f"[v][a]concat=n=1:v=1:a=1[outv][outa]"
            )

            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False, encoding="utf-8"
            ) as f:
                f.write(graph)
                f.flush()
                os.fsync(f.fileno())
                script_path = f.name

            try:
                def _chunk_prog(us: int):
                    chunk_dur = sum(e - s for s, e in chunk)
                    if progress_callback and total_duration > 0:
                        base = encoded_duration / total_duration
                        span = chunk_dur / total_duration
                        progress_callback(min(base + us / 1_000_000 / total_duration, base + span))

                _run_ffmpeg(
                    [
                        "ffmpeg", "-y", "-loglevel", "error",
                        "-progress", "pipe:1",
                        "-i", str(video_path),
                        "-filter_complex_script", script_path,
                        "-map", "[outv]", "-c:v", vcodec, *vcodec_opts,
                        "-map", "[outa]", "-c:a", "aac", "-b:a", _AUDIO_BITRATE,
                        str(chunk_path),
                    ],
                    progress_callback=_chunk_prog,
                    timeout=_SEGMENT_ENCODE_TIMEOUT,
                    label=f"batch chunk {ci}/{n_chunks}",
                    cancel_callback=cancel_callback,
                )
            finally:
                Path(script_path).unlink(missing_ok=True)

            encoded_duration += sum(e - s for s, e in chunk)
            logger.info(f"batch chunk {ci+1}/{n_chunks} done ({chunk_path.stat().st_size // 1024 // 1024} MB)")

        if skipped:
            logger.info(f"batch: resumed {skipped}/{n_chunks} already encoded, encoded {n_chunks - skipped}")

        # Concat all chunk files
        list_path = batch_dir / "concat.txt"
        with open(list_path, "w", encoding="utf-8") as lf:
            for ci in range(n_chunks):
                cp = batch_dir / f"chunk_{ci:04d}.mp4"
                lf.write(f"file {_quote_concat_path(cp.as_posix())}\n")

        def _concat_prog(us: int):
            if progress_callback and total_duration > 0:
                progress_callback(min(0.9 + us / 1_000_000 / total_duration * 0.1, 1.0))

        _run_ffmpeg(
            [
                "ffmpeg", "-y", "-loglevel", "error", "-progress", "pipe:1",
                "-f", "concat", "-safe", "0", "-i", str(list_path),
                "-c", "copy", "-fflags", "+genpts",
                str(output_path),
            ],
            progress_callback=_concat_prog,
            timeout=_FINAL_CONCAT_TIMEOUT,
            label="batch concat",
            cancel_callback=cancel_callback,
        )
        logger.info(f"batch: successfully created {output_path}")

        # Cleanup on success
        shutil.rmtree(batch_dir, ignore_errors=True)

    except Exception:
        # On failure: keep chunks for resume
        logger.info(f"Chunks kept in {batch_dir} for resume on next run")
        raise


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
