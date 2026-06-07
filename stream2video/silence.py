"""Silence detection module using ffmpeg silencedetect filter.

Pipeline (D — fast, audio-only):
  1. Extract audio to WAV (mono 16kHz, -copyts to preserve timestamps).
  2. Run ffmpeg silencedetect on the WAV.
  3. Sample-verify: run silencedetect on the first
     `_SAMPLE_VERIFY_DURATION` seconds of the original video and compare
     against the corresponding window of D's segments. On match, trust D and
     keep the WAV cache. On mismatch (e.g., source has broken timestamps or
     an unexpected `itsoffset`), invalidate the WAV and fall back to a full
     A-path detection on the original video.
  4. Cache the WAV keyed by source mtime so subsequent runs skip extract
     and sample-verify.

The A path (direct on video, no cache) is also available via `output_dir=None`
for callers that don't want WAV caching. It is the canonical result used on
sample-verify mismatch.
"""

import json
import logging
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from stream2video.utils import (
    CANCEL_POLL_INTERVAL,
    cancel_monitor,
    drain_stderr_lines,
    no_window_kwargs,
    set_active_process,
)
from stream2video.utils import (
    get_video_duration as _probe_duration,
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


_SILENCE_POLL_INTERVAL = CANCEL_POLL_INTERVAL
_SILENCE_TIMEOUT = 36000
_SEGMENT_MATCH_TOLERANCE = 0.05
_SAMPLE_VERIFY_DURATION = 60.0

_NUM = r"\d+(?:[.,]\d+)?"
_SILENCE_START_RE = re.compile(rf"silence_start:\s*({_NUM})")
_SILENCE_END_RE = re.compile(rf"silence_end:\s*({_NUM})")


def _to_float(s: str) -> float:
    return float(s.replace(",", "."))


def detect_silence(
    video_path: Path,
    threshold: float = -20,
    min_silence: float = 0.5,
    margin: float = -0.5,
    output_dir: Path | None = None,
    progress_callback: Callable[[float], None] | None = None,
    cancel_callback: Callable[[], bool] | None = None,
    live_cache_path: Path | None = None,
) -> list[SilenceSegment]:
    """
    Detect silence segments using ffmpeg silencedetect filter.

    When `output_dir` is provided, the audio is first extracted to a cached WAV
    file ({stem}_audio.wav) and silencedetect runs on the WAV. The first time
    the WAV is created (or whenever the source mtime is newer), a "sample-verify"
    pass runs silencedetect on the first `_SAMPLE_VERIFY_DURATION` seconds of
    the original video to detect sources with broken timestamps; on mismatch
    the WAV is invalidated and a full direct detection is run on the video.

    Args:
        video_path: Path to video file
        threshold: Silence threshold in dB (default -20, range [-60, -5])
        min_silence: Minimum silence duration in seconds (default 0.5, range [0.1, 60])
        margin: How much to shrink silence zones in seconds (default 0.5, range [-3, 5]).
                Positive = shrink silence (keep more audio around phrases).
                Negative = expand silence (cut more aggressively).
                0 = no adjustment.
        output_dir: If provided, enable the cached-WAV pipeline. The WAV is created
                    on the first run (or whenever the source mtime is newer) and
                    re-used on subsequent runs. If None, silencedetect runs
                    directly on the video (A path, no WAV caching).
        progress_callback: Optional callback with progress fraction [0, 1]
        cancel_callback: Optional callable returning True to abort; checked while ffmpeg runs.
        live_cache_path: If provided, the canonical detection writes a "partial"
                    silence cache JSON at this path as segments are detected, so
                    a UI can poll the file and render a near-real-time preview
                    while the pipeline is still running. The file uses the same
                    format as `save_silence_cache` and is overwritten on every
                    new segment. The caller is responsible for deleting the
                    file on cancel/error (it's NOT removed on success — the
                    final canonical cache is written elsewhere by the caller
                    via `save_silence_cache`).

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

    duration = _probe_duration(video_path)

    config = {
        "threshold": threshold,
        "min_silence": min_silence,
        "margin": margin,
    }

    def _make_live_writer() -> Callable[[list[SilenceSegment]], None] | None:
        if live_cache_path is None:
            return None

        def _write(seg_list: list[SilenceSegment]) -> None:
            try:
                margin_applied = _apply_margin(seg_list, margin)
                _save_silence_cache_to_path(live_cache_path, video_path, margin_applied, config)
            except Exception as e:
                logger.warning(f"Live cache write failed: {e}")

        return _write

    if output_dir is not None:
        wav_path = _get_wav_cache_path(video_path, output_dir)
        if _is_wav_cache_valid(wav_path, video_path):
            logger.debug(f"Using cached WAV: {wav_path}")
            segments = _run_silencedetect(
                wav_path,
                threshold,
                min_silence,
                duration,
                progress_callback,
                cancel_callback,
                "WAV cache",
                on_segment=_make_live_writer(),
            )
        else:
            _extract_audio_wav(video_path, wav_path, cancel_callback)
            segments_D = _run_silencedetect(
                wav_path,
                threshold,
                min_silence,
                duration,
                None,
                cancel_callback,
                "WAV",
            )
            segments_A_sample = _run_silencedetect(
                video_path,
                threshold,
                min_silence,
                duration,
                progress_callback,
                cancel_callback,
                "video (sample)",
                duration_limit=_SAMPLE_VERIFY_DURATION,
            )
            segments_D_sample = [s for s in segments_D if s.start < _SAMPLE_VERIFY_DURATION]
            if _sample_segments_match(
                segments_D_sample, segments_A_sample, _SEGMENT_MATCH_TOLERANCE
            ):
                logger.debug(
                    f"Sample-verify passed (D-sample: {len(segments_D_sample)} starts in first "
                    f"{_SAMPLE_VERIFY_DURATION:.0f}s match A-sample: {len(segments_A_sample)}) "
                    f"— using D result, keeping WAV cache"
                )
                segments = segments_D
                # Sample-verify used a batch D-pass for comparison; if a live
                # cache is requested, write the final result now so the GUI
                # doesn't have to wait for a (skipped) progressive re-run.
                writer = _make_live_writer()
                if writer is not None:
                    writer(segments)
            else:
                logger.warning(
                    f"Sample-verify failed (D-sample: {len(segments_D_sample)}, "
                    f"A-sample: {len(segments_A_sample)} segment starts in first "
                    f"{_SAMPLE_VERIFY_DURATION:.0f}s, tolerance={_SEGMENT_MATCH_TOLERANCE}s). "
                    f"Source may have broken timestamps — falling back to full direct "
                    f"detection. WAV cache invalidated."
                )
                wav_path.unlink(missing_ok=True)
                segments = _run_silencedetect(
                    video_path,
                    threshold,
                    min_silence,
                    duration,
                    progress_callback,
                    cancel_callback,
                    "video",
                    on_segment=_make_live_writer(),
                )
    else:
        segments = _run_silencedetect(
            video_path,
            threshold,
            min_silence,
            duration,
            progress_callback,
            cancel_callback,
            "video",
            on_segment=_make_live_writer(),
        )

    segments = _apply_margin(segments, margin)

    if not segments:
        logger.info("No silence segments detected (video may have no audio track)")

    return segments


def detect_silence_stream(
    input_path: Path,
    threshold: float,
    min_silence: float,
    *,
    on_segment: Callable[[list[SilenceSegment]], None] | None = None,
) -> list[SilenceSegment]:
    """Run ffmpeg silencedetect on ``input_path`` directly — no WAV file.

    Use this for preview-only flows (e.g. the GUI waveform tab) that
    don't need the cached audio extract the real pipeline relies on.
    Results are not persisted to the silence cache; subsequent pipeline
    runs will redo detection on their own schedule.

    If ``on_segment`` is provided, it is called with the running list
    of detected segments every time a new ``silence_end`` line arrives
    on ffmpeg's stderr. The callback runs in the same thread that
    called this function (typically a background thread); callers that
    need to touch a UI must wrap the call with their framework's
    main-thread dispatch (e.g. ``self.after(0, ...)`` in Tkinter).

    Returns the final list of segments. Hard ffmpeg failures raise
    :class:`SilenceDetectionError`; the callback is not invoked on
    sources that have no parseable silencedetect output.
    """
    if on_segment is None:
        # Fast path: reuse the existing batch parser (production code).
        return _run_silencedetect(
            input_path,
            threshold=threshold,
            min_silence=min_silence,
            duration=None,
            progress_callback=None,
            cancel_callback=None,
            label="video (preview)",
            duration_limit=None,
        )

    # Progressive path: parse stderr line-by-line and fire the callback.
    noise = 10 ** (threshold / 20)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-i",
        str(input_path),
        "-af",
        f"silencedetect=noise={noise}:duration={min_silence}",
        "-f",
        "null",
        "-",
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=-1,
            **no_window_kwargs(),
        )
    except FileNotFoundError as e:
        raise SilenceDetectionError("ffmpeg not found in PATH") from e

    set_active_process(proc)
    assert proc.stderr is not None
    pipe = proc.stderr

    segments: list[SilenceSegment] = []
    pending_start: float | None = None
    start_re = re.compile(r"silence_start:\s*(-?\d+\.?\d*)")
    end_re = re.compile(r"silence_end:\s*(-?\d+\.?\d*)")

    try:
        for raw in iter(pipe.readline, b""):
            line = raw.decode("utf-8", errors="replace")
            m_s = start_re.search(line)
            if m_s:
                pending_start = float(m_s.group(1))
                continue
            m_e = end_re.search(line)
            if m_e and pending_start is not None:
                segments.append(SilenceSegment(pending_start, float(m_e.group(1))))
                pending_start = None
                on_segment(list(segments))

        proc.wait(timeout=_SILENCE_TIMEOUT)
        if proc.returncode != 0:
            raise SilenceDetectionError(f"ffmpeg silencedetect failed (rc={proc.returncode})")
        return segments
    except subprocess.TimeoutExpired as e:
        proc.kill()
        raise SilenceDetectionError(f"ffmpeg timeout after {e.timeout}s") from e
    finally:
        set_active_process(None)
        pipe.close()


def _run_silencedetect(
    input_path: Path,
    threshold: float,
    min_silence: float,
    duration: float | None,
    progress_callback: Callable[[float], None] | None,
    cancel_callback: Callable[[], bool] | None,
    label: str,
    duration_limit: float | None = None,
    on_segment: Callable[[list[SilenceSegment]], None] | None = None,
) -> list[SilenceSegment]:
    """Run ffmpeg silencedetect on `input_path` and return parsed segments.

    `label` is used for log/error messages ("WAV", "video", "WAV cache",
    "video (sample)").

    `duration_limit`: if set, ffmpeg processes at most this many seconds of
    input (added as `-t` flag). Used for sample-verification, where running
    silencedetect on the full video would be wasteful. Progress is reported
    relative to `duration_limit` in that case, not the full `duration`.

    `on_segment`: optional callback invoked with a *copy* of the running
    list of detected segments every time a `silence_end` line arrives on
    ffmpeg's stderr. The callback runs on the stderr drain thread, so
    callers that touch a UI must wrap the work in a main-thread dispatch.
    Used by `detect_silence(live_cache_path=...)` to write a partial cache
    file the GUI can poll while the pipeline is still running. When
    `on_segment` is None, the function uses the batch path (parse stderr
    only after the process exits) — the original behaviour.
    """
    noise = 10 ** (threshold / 20)

    cmd = [
        "ffmpeg",
        "-progress",
        "pipe:1",
        "-i",
        str(input_path),
        "-af",
        f"silencedetect=noise={noise}:duration={min_silence}",
        "-f",
        "null",
        "-",
    ]
    if duration_limit is not None:
        # Insert "-t <duration>" right before "-f null" so it's interpreted
        # as a global output option, not a value for "-f".
        cmd[7:7] = ["-t", str(duration_limit)]

    progress_divisor = duration_limit if duration_limit is not None else duration

    try:
        logger.info(
            f"Running ffmpeg silencedetect on {label}: "
            f"threshold={threshold}dB ({noise}), min_silence={min_silence}s"
        )
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=-1,
            **no_window_kwargs(),
        )
    except FileNotFoundError as e:
        raise SilenceDetectionError("ffmpeg not found in PATH") from e

    set_active_process(process)
    stderr_pipe = process.stderr
    stdout_pipe = process.stdout
    assert stderr_pipe is not None and stdout_pipe is not None
    stderr_lines: list[str] = []

    # State for progressive parsing. Only used when on_segment is set; the
    # batch path ignores these and re-parses the accumulated stderr at the
    # end. We mutate `progressive_segments` only from the drain thread (the
    # only place `_on_line` runs), so no lock is needed.
    progressive_segments: list[SilenceSegment] = []
    pending_start: list[float | None] = [None]  # mutable container so the
    # closure can assign without `nonlocal`.

    def _on_line(line: str) -> None:
        m_s = _SILENCE_START_RE.search(line)
        if m_s:
            pending_start[0] = _to_float(m_s.group(1))
            return
        m_e = _SILENCE_END_RE.search(line)
        if m_e and pending_start[0] is not None:
            progressive_segments.append(SilenceSegment(pending_start[0], _to_float(m_e.group(1))))
            pending_start[0] = None
            if on_segment is not None:
                on_segment(list(progressive_segments))

    if on_segment is not None:
        wait_for_drain = drain_stderr_lines(stderr_pipe, stderr_lines, on_line=_on_line)
    else:
        wait_for_drain = drain_stderr_lines(stderr_pipe, stderr_lines)
    drain_done = False

    try:
        with cancel_monitor(process, cancel_callback) as cancelled:
            for raw_line in iter(stdout_pipe.readline, b""):
                line = raw_line.decode("utf-8", errors="replace").strip()
                if line.startswith("out_time_us="):
                    try:
                        us = int(line.split("=", 1)[1])
                        if progress_callback and progress_divisor and progress_divisor > 0:
                            progress_callback(min(us / 1_000_000 / progress_divisor, 1.0))
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

            if on_segment is not None:
                if pending_start[0] is not None:
                    logger.warning(
                        "Unmatched silence_start (no silence_end); dropped — "
                        "ffmpeg output may be truncated"
                    )
                return list(progressive_segments)
            return _parse_ffmpeg_output("".join(stderr_lines))

    except subprocess.TimeoutExpired as e:
        process.kill()
        raise SilenceDetectionError(f"ffmpeg timeout after {e.timeout}s") from e
    finally:
        if not drain_done:
            wait_for_drain()
        set_active_process(None)
        stdout_pipe.close()
        stderr_pipe.close()


def _extract_audio_wav(
    video_path: Path,
    wav_path: Path,
    cancel_callback: Callable[[], bool] | None = None,
) -> None:
    """Extract audio from `video_path` to a 16kHz mono PCM WAV at `wav_path`.

    Uses `-fflags +copyts` to preserve input PTS so that timestamps in the WAV
    match the original video's timeline (required for silence detection results
    to align with the video when used as cut points in cut_and_concat).

    The WAV is the cached artifact for the D (audio-only) path. On broken-PTS
    sources the verification pass at the call site detects the mismatch and
    deletes this file.
    """
    wav_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg",
        "-y",
        "-copyts",
        "-i",
        str(video_path),
        "-vn",
        "-ar",
        "16000",
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        str(wav_path),
    ]

    try:
        logger.info(
            f"Extracting audio: {video_path.name} → {wav_path.name} "
            f"(16kHz mono pcm_s16le, -fflags +copyts)"
        )
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=-1,
            **no_window_kwargs(),
        )
    except FileNotFoundError as e:
        raise SilenceDetectionError("ffmpeg not found in PATH") from e

    set_active_process(process)
    stderr_pipe = process.stderr
    stdout_pipe = process.stdout
    assert stderr_pipe is not None
    stderr_lines: list[str] = []
    wait_for_drain = drain_stderr_lines(stderr_pipe, stderr_lines)
    drain_done = False

    try:
        with cancel_monitor(process, cancel_callback) as cancelled:
            if cancelled.is_set():
                raise SilenceCancelledError("audio extraction cancelled")
            process.wait(timeout=_SILENCE_TIMEOUT)
            wait_for_drain()
            drain_done = True

            if process.returncode != 0:
                stderr_text = "".join(stderr_lines)
                error_msg = stderr_text or "Unknown error"
                wav_path.unlink(missing_ok=True)
                raise SilenceDetectionError(f"ffmpeg extract failed: {error_msg}")
    except subprocess.TimeoutExpired as e:
        process.kill()
        wav_path.unlink(missing_ok=True)
        raise SilenceDetectionError(f"ffmpeg extract timeout after {e.timeout}s") from e
    finally:
        if not drain_done:
            wait_for_drain()
        set_active_process(None)
        if stdout_pipe is not None:
            stdout_pipe.close()
        stderr_pipe.close()


def _parse_ffmpeg_output(stderr: str) -> list[SilenceSegment]:
    """Parse ffmpeg silencedetect output."""
    starts = [_to_float(m.group(1)) for m in _SILENCE_START_RE.finditer(stderr)]
    ends = [_to_float(m.group(1)) for m in _SILENCE_END_RE.finditer(stderr)]

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

    return [SilenceSegment(start, end) for start, end in zip(starts, ends, strict=False)]


def _segments_match(
    seg_a: list[SilenceSegment],
    seg_b: list[SilenceSegment],
    tolerance: float = _SEGMENT_MATCH_TOLERANCE,
) -> bool:
    """True if two segment lists are equivalent within `tolerance` seconds.

    Used to verify that the WAV-based detection (D) matches the video-based
    detection (A). If they differ, the source likely has broken timestamps and
    the caller should fall back to A's result and invalidate the WAV cache.
    """
    if len(seg_a) != len(seg_b):
        return False

    sorted_a = sorted([(s.start, s.end) for s in seg_a])
    sorted_b = sorted([(s.start, s.end) for s in seg_b])

    for (a_start, a_end), (b_start, b_end) in zip(sorted_a, sorted_b, strict=True):
        if abs(a_start - b_start) > tolerance:
            return False
        if abs(a_end - b_end) > tolerance:
            return False

    return True


def _sample_segments_match(
    seg_a: list[SilenceSegment],
    seg_b: list[SilenceSegment],
    tolerance: float = _SEGMENT_MATCH_TOLERANCE,
) -> bool:
    """True if two segment lists have matching START times within `tolerance`.

    Used for sample-verify where A's segments are clipped at the `-t` boundary
    (e.g., a real `(50, 80)` becomes `(50, 60)` in A-sample), so END times are
    not directly comparable. Comparing START times (and counts) is sufficient
    to detect the common case of constant itsoffset broken-PTS, which shifts
    every start by the same offset.
    """
    if len(seg_a) != len(seg_b):
        return False

    starts_a = sorted(s.start for s in seg_a)
    starts_b = sorted(s.start for s in seg_b)

    return all(
        abs(a_start - b_start) <= tolerance
        for a_start, b_start in zip(starts_a, starts_b, strict=True)
    )


def _apply_margin(segments: list[SilenceSegment], margin: float) -> list[SilenceSegment]:
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


def _get_wav_cache_path(video_path: Path, output_dir: Path) -> Path:
    return output_dir / f"{video_path.stem}_audio.wav"


def _is_wav_cache_valid(wav_path: Path, video_path: Path) -> bool:
    """WAV cache is valid if it exists and is at least as new as the source video."""
    if not wav_path.exists():
        return False
    return wav_path.stat().st_mtime >= video_path.stat().st_mtime


def _get_cache_path(video_path: Path, output_dir: Path) -> Path:
    return output_dir / f"{video_path.stem}_silence_cache.json"


def get_silence_cache_partial_path(video_path: Path, output_dir: Path) -> Path:
    """Path for the partial silence cache written during detection.

    The final canonical cache lives at `{stem}_silence_cache.json`; the
    partial version (used by the GUI waveform preview to render
    near-real-time overlays while the pipeline is still running) is
    suffixed with `.partial` so the two files never collide.
    """
    return output_dir / f"{video_path.stem}_silence_cache.json.partial"


def _save_silence_cache_to_path(
    cache_path: Path,
    video_path: Path,
    segments: list[SilenceSegment],
    config: dict,
) -> None:
    """Atomically write the silence cache to `cache_path`.

    Shared by `save_silence_cache` (final, after detection) and the
    live cache writer inside `detect_silence` (partial, during
    detection). The temp file is created in the same directory as
    `cache_path` so `os.replace` is atomic on the same filesystem.
    Parent directories are created if needed.
    """
    cache_path.parent.mkdir(parents=True, exist_ok=True)
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
        dir=cache_path.parent, prefix=f".{cache_path.name}.", suffix=".tmp"
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


def load_silence_cache_from_path(
    cache_path: Path,
    video_path: Path,
    config: dict,
) -> list[SilenceSegment] | None:
    """Load the silence cache at `cache_path` if it exists, is fresh, and
    matches `config`. Returns None on any miss (missing, stale, mismatched,
    or malformed).

    Used by both `load_silence_cache` (final cache) and the GUI's
    live-preview poller (partial cache).
    """
    if not cache_path.exists():
        return None
    if cache_path.stat().st_mtime < video_path.stat().st_mtime:
        return None
    try:
        with open(cache_path) as f:
            data = json.load(f)
        for key in ("threshold", "min_silence", "margin"):
            if data.get("config", {}).get(key) != config.get(key):
                return None
        return [SilenceSegment(s["start"], s["end"]) for s in data["segments"]]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def save_silence_cache(
    video_path: Path,
    segments: list[SilenceSegment],
    output_dir: Path,
    config: dict,
):
    cache_path = _get_cache_path(video_path, output_dir)
    _save_silence_cache_to_path(cache_path, video_path, segments, config)
    logger.info(f"Silence cache saved to {cache_path}")


def load_silence_cache(
    video_path: Path,
    output_dir: Path,
    config: dict,
) -> list[SilenceSegment] | None:
    cache_path = _get_cache_path(video_path, output_dir)
    if not cache_path.exists():
        return None
    if cache_path.stat().st_mtime < video_path.stat().st_mtime:
        logger.info("Silence cache outdated (source file newer)")
        return None
    try:
        result = load_silence_cache_from_path(cache_path, video_path, config)
    except OSError as e:
        logger.warning(f"Could not read silence cache: {e}")
        return None
    if result is not None:
        logger.info(f"Loaded {len(result)} silence segments from cache")
    return result
