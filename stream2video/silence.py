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
import time
from collections.abc import Callable
from pathlib import Path

from stream2video.config import CONFIG_DEFAULTS
from stream2video.utils import (
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


class SilenceCancelledError(SilenceDetectionError):
    """Silence detection was cancelled by user (not a real failure)."""


class SilenceSegment:
    """Silence segment representation."""

    def __init__(self, start: float, end: float):
        self.start = start
        self.end = end
        self.duration = max(0.0, end - start)

    def __repr__(self):
        return f"SilenceSegment({self.start:.2f}s - {self.end:.2f}s, duration={self.duration:.2f}s)"


_SILENCE_TIMEOUT = 36000
_SEGMENT_MATCH_TOLERANCE = 0.05
_SAMPLE_VERIFY_DURATION = 60.0
# Resume cache throttling. We save at most every 30 seconds OR every
# 100 new segments, whichever fires first. This keeps the per-segment
# overhead negligible while still letting a cancelled run pick up from
# a useful checkpoint.
_RESUME_THROTTLE_S = 30.0
_RESUME_THROTTLE_N = 100


def _noop_on_segment(_segments: list[SilenceSegment]) -> None:
    """Default no-op callback used when `initial_segments` is set but
    the caller didn't supply `on_segment` — see `_run_silencedetect`."""


_NUM = r"\d+(?:[.,]\d+)?"
_SILENCE_START_RE = re.compile(rf"silence_start:\s*({_NUM})")
_SILENCE_END_RE = re.compile(rf"silence_end:\s*({_NUM})")


def _to_float(s: str) -> float:
    return float(s.replace(",", "."))


def detect_silence(
    video_path: Path,
    threshold: float = CONFIG_DEFAULTS["threshold"],
    min_silence: float = CONFIG_DEFAULTS["min_silence"],
    margin: float = CONFIG_DEFAULTS["margin"],
    output_dir: Path | None = None,
    progress_callback: Callable[[float], None] | None = None,
    cancel_callback: Callable[[], bool] | None = None,
    on_segment: Callable[[list[SilenceSegment]], None] | None = None,
    resume_cache_path: Path | None = None,
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
        threshold: Silence threshold in dB (default from CONFIG_DEFAULTS,
                currently -30, range [-60, -5]). The default was historically
                -20 when called directly via the API; that diverged from the
                config-file / GUI value of -30, so an API call cut more
                aggressively than a config-file run on the same source. The
                defaults are now unified (see P1.7 in the fix plan).
        min_silence: Minimum silence duration in seconds (default from
                CONFIG_DEFAULTS, currently 2.0, range [0.1, 60]).
        margin: How much to shrink silence zones in seconds (default from
                CONFIG_DEFAULTS, currently 0.5, range [-3, 5]).
                Positive = shrink silence (keep more audio around phrases).
                Negative = expand silence (cut more aggressively).
                0 = no adjustment.
        output_dir: If provided, enable the cached-WAV pipeline. The WAV is created
                    on the first run (or whenever the source mtime is newer) and
                    re-used on subsequent runs. If None, silencedetect runs
                    directly on the video (A path, no WAV caching).
        progress_callback: Optional callback with progress fraction [0, 1]
        cancel_callback: Optional callable returning True to abort; checked while ffmpeg runs.
        on_segment: Optional callback invoked with a *snapshot* of the running
                    list of raw (pre-margin) segments every time a new
                    ``silence_end`` line arrives on ffmpeg's stderr. The
                    callback runs on ffmpeg's stderr drain thread, so callers
                    that need to touch a UI must wrap the work in their
                    framework's main-thread dispatch (e.g. ``self.after(0, ...)``
                    in Tkinter). Used by the GUI to keep a near-real-time
                    preview in sync with the running detection. On resume,
                    the callback is also fired once at the start with the
                    pre-seeded initial list so the GUI's overlay is correct
                    from the moment the pipeline begins. Not invoked for
                    sample-verify passes (which are batch and discarded).
        resume_cache_path: Path to a resume cache file. If set and the file
                    is fresh (mtime >= source mtime) and config-matching,
                    detection picks up from the last throttled checkpoint
                    written by a previous cancelled/crashed run. The file
                    is unlinked at the start of detection so a retry
                    within the same call doesn't re-load it. Use
                    ``_get_resume_cache_path(video_path, output_dir)`` to
                    compute the conventional path. Throttled checkpoints
                    are written when `resume_cache_path` is set; a new
                    run that uses resume will overwrite the file as it
                    progresses, so leave it in place for cancellation /
                    crash recovery and let the GUI unlink it on success.

    Returns:
        List of SilenceSegment objects (margin applied)
    """
    if not video_path.exists():
        raise SilenceDetectionError(f"Video file not found: {video_path}")

    if not -60 <= threshold <= -5:
        raise ValueError(f"Threshold must be in range [-60, -5], got {threshold}")

    if not 0.1 <= min_silence <= 60:
        raise ValueError(f"Min silence must be in range [0.1, 60], got {min_silence}")

    if not -3 <= margin <= 5:
        raise ValueError(f"Margin must be in range [-3, 5], got {margin}")

    current_config = {
        "threshold": threshold,
        "min_silence": min_silence,
        "margin": margin,
    }

    # Resume cache: load and validate, then unlink so a retry inside
    # this call doesn't re-load it. The file is ephemeral — if the
    # detection is cancelled, the next run reads whatever was last
    # throttled-saved. On success, the final cache is the source of
    # truth and the resume file is the GUI's responsibility to clean
    # up.
    initial_segments: list[SilenceSegment] = []
    resume_from: float | None = None
    if resume_cache_path is not None:
        loaded = _load_silence_cache_from_path(resume_cache_path, video_path, current_config)
        if loaded is not None:
            initial_segments = loaded
            resume_from = initial_segments[-1].end if initial_segments else None
            if resume_from is not None:
                logger.info(
                    f"Resuming from resume cache: {len(initial_segments)} segments, "
                    f"seek to t={resume_from:.2f}s"
                )
            else:
                logger.info("Resume cache has 0 segments — starting from t=0 with checkpointing on")
        else:
            logger.info("No valid resume cache — starting fresh")
        # Unlink unconditionally — if it was stale/missing, this is a
        # no-op (missing_ok=True guards against FileNotFoundError); if
        # it was valid, we don't want a retry to re-load it.
        try:
            resume_cache_path.unlink(missing_ok=True)
        except OSError as e:
            logger.debug(f"Resume cache unlink failed (will be retried next run): {e}")

    duration = _probe_duration(video_path)

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
                on_segment=on_segment,
                initial_segments=initial_segments,
                resume_from=resume_from,
                resume_save_path=resume_cache_path,
                resume_save_config=current_config,
            )
        else:
            _extract_audio_wav(video_path, wav_path, cancel_callback)
            # The WAV was just (re-)extracted — no prior work to resume
            # from, even if `initial_segments` was set above (it came
            # from an old run whose state is no longer in sync with the
            # new WAV). Drop the resume context for the canonical
            # detection so we don't skip work we don't actually have.
            segments_D = _run_silencedetect(
                wav_path,
                threshold,
                min_silence,
                duration,
                None,
                cancel_callback,
                "WAV",
            )
            # Sample-verify must NOT use the user-facing progress_callback:
            # it runs for a fixed _SAMPLE_VERIFY_DURATION window and would
            # fill the progress bar to 100% over 60s while the real D
            # detection (which has no progress callback) just finished.
            # The bar would either freeze at 100% (verify pass) or jump back
            # to 0% on the A-fallback (verify fail). Keep verify invisible.
            segments_A_sample = _run_silencedetect(
                video_path,
                threshold,
                min_silence,
                duration,
                None,
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
                    on_segment=on_segment,
                    initial_segments=initial_segments,
                    resume_from=resume_from,
                    resume_save_path=resume_cache_path,
                    resume_save_config=current_config,
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
            on_segment=on_segment,
            initial_segments=initial_segments,
            resume_from=resume_from,
            resume_save_path=resume_cache_path,
            resume_save_config=current_config,
        )

    segments = apply_margin(segments, margin, duration)

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

    try:
        for raw in iter(pipe.readline, b""):
            line = raw.decode("utf-8", errors="replace")
            m_s = _SILENCE_START_RE.search(line)
            if m_s:
                pending_start = float(m_s.group(1))
                continue
            m_e = _SILENCE_END_RE.search(line)
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
    initial_segments: list[SilenceSegment] | None = None,
    resume_from: float | None = None,
    resume_save_path: Path | None = None,
    resume_save_config: dict | None = None,
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
    When set, the function uses the progressive path (parse stderr
    line-by-line) — otherwise it uses the batch path (parse stderr only
    after the process exits). On resume (`initial_segments` set), the
    snapshot passed to the callback includes the pre-seeded initial
    segments so the GUI always sees the full picture from the first call.

    `initial_segments`: pre-seeded raw (pre-margin) segments from a
    previous run's resume cache. The new ffmpeg call starts at
    `resume_from` and produces *additional* segments; the returned list
    concatenates initial + new (all raw, margin applied once by the
    caller). Ignored unless `on_segment` is also set — the batch path
    has no place to surface the pre-seeded segments.

    `resume_from`: absolute input-time position to seek ffmpeg to before
    decoding (added as `-ss` before `-i`). `-copyts` is also added so
    ffmpeg preserves source PTS after the seek — without it the
    silencedetect timestamps would restart at 0 relative to the seek
    point and the new segments would NOT be directly concatenable with
    `initial_segments`.

    `resume_save_path` + `resume_save_config`: throttled checkpoint of
    `progressive_segments` to `resume_save_path` so a subsequent run
    can pick up from a useful point if this one is cancelled or
    crashes. Triggered every `_RESUME_THROTTLE_S` seconds OR every
    `_RESUME_THROTTLE_N` new segments, whichever fires first. No save
    happens if no new segments have been detected.
    """
    # If `initial_segments` is set, the progressive path is required —
    # the batch parser can't see the pre-seeded list and would return
    # only the new segments, losing the initial ones. Auto-enable
    # progressive mode with a no-op callback so the throttled save
    # still works for callers that don't need live updates.
    if initial_segments and on_segment is None:
        on_segment = _noop_on_segment

    noise = 10 ** (threshold / 20)

    # Build the ffmpeg command in dependency order: global options →
    # input → filter → output. `extend` keeps the list monotonic so
    # inserting `-ss` or `-t` does not require magic indices.
    # `-copyts` is required when seeking (resume path): without it,
    # input `-ss` before `-i` resets the output PTS to zero, so
    # silencedetect would report timestamps *relative to the seek
    # point* instead of absolute source time — silently corrupting
    # the `initial + new` merge on real videos. With `-copyts`, ffmpeg
    # preserves the source PTS and the segments are directly
    # concatenable. The option is harmless (no-op) when not seeking.
    cmd = ["ffmpeg", "-copyts", "-progress", "pipe:1"]
    if resume_from is not None and resume_from > 0:
        # `-ss` before `-i` = fast seek (keyframe-aligned). Accurate
        # seek (output PTS aligned) is not needed — silencedetect
        # outputs timestamps from the source PTS, which `-copyts`
        # above preserves.
        cmd.extend(["-ss", f"{resume_from:.3f}"])
    cmd.extend(
        [
            "-i",
            str(input_path),
            "-af",
            f"silencedetect=noise={noise}:duration={min_silence}",
        ]
    )
    if duration_limit is not None:
        cmd.extend(["-t", str(duration_limit)])
    cmd.extend(["-f", "null", "-"])

    # Progress is reported relative to the portion of the input ffmpeg
    # will actually decode — i.e. after any seek, the elapsed time
    # reported by ffmpeg starts from the seek point, so the divisor
    # must exclude the seeked-out head.
    base_divisor = duration_limit if duration_limit is not None else duration
    progress_divisor: float | None
    if base_divisor is not None and resume_from is not None:
        progress_divisor = max(0.0, base_divisor - resume_from)
    else:
        progress_divisor = base_divisor

    try:
        logger.info(
            f"Running ffmpeg silencedetect on {label}: "
            f"threshold={threshold}dB ({noise}), min_silence={min_silence}s"
            + (f", resume_from={resume_from:.2f}s" if resume_from else "")
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
    # On resume, `progressive_segments` starts with the pre-seeded initial
    # segments so the callback sees the full picture from the first
    # silence_end line and the throttled save covers everything detected
    # so far (initial + new), not just the new ones.
    progressive_segments: list[SilenceSegment] = list(initial_segments) if initial_segments else []

    # Pre-seed the callback with the initial segments so the GUI's live
    # overlay is correct from the moment the pipeline starts. This is
    # the one exception to the "callback fires on the drain thread"
    # rule — the GUI's callback is thread-safe (lock-protected dict
    # update), and firing here closes the gap between the worker
    # pre-seeding to [] and the first silence_end line arriving.
    if progressive_segments and on_segment is not None:
        on_segment(list(progressive_segments))

    pending_start: list[float | None] = [None]  # mutable container so the
    # closure can assign without `nonlocal`.

    # Throttled resume save state. Mutable lists let the closure update
    # them without `nonlocal` declarations.
    last_save_time: list[float] = [0.0]
    last_save_count: list[int] = [0]
    if resume_save_path is not None:
        last_save_time[0] = time.monotonic()

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
            _maybe_save_resume()

    def _maybe_save_resume() -> None:
        """Checkpoint the current segment list to disk if the throttle
        window has elapsed. Counts and timestamps tracked via mutable
        lists so the closure can update them without `nonlocal`.
        """
        if resume_save_path is None or resume_save_config is None:
            return
        new_count = len(progressive_segments) - len(initial_segments or [])
        if new_count <= 0:
            return
        now = time.monotonic()
        if (
            now - last_save_time[0] < _RESUME_THROTTLE_S
            and new_count - last_save_count[0] < _RESUME_THROTTLE_N
        ):
            return
        try:
            _save_cache(
                resume_save_path,
                input_path,
                progressive_segments,
                resume_save_config,
                indent=None,
                fsync=False,
            )
            last_save_time[0] = now
            last_save_count[0] = new_count
        except OSError as e:
            # Resume saves are best-effort — a failed checkpoint just
            # means the next run starts from a slightly earlier point.
            logger.warning(f"Resume cache save failed: {e}")

    if on_segment is not None:
        wait_for_drain = drain_stderr_lines(stderr_pipe, stderr_lines, on_line=_on_line)
    else:
        wait_for_drain = drain_stderr_lines(stderr_pipe, stderr_lines)
    drain_done = False

    try:
        with cancel_monitor(process, cancel_callback) as cancelled:
            for raw_line in iter(stdout_pipe.readline, b""):
                # Direct cancel poll: the cancel_monitor thread also
                # kills the process on cancel, but on a silent pipe (no
                # progress lines, short video, or `-t` reached without
                # progress events) the readline() above would block and
                # the thread's kill would only surface as EOF latency.
                # Polling the callback inline on every line keeps cancel
                # responsive once a line does arrive, matching concat.py.
                if cancel_callback is not None and cancel_callback():
                    process.kill()
                    raise SilenceCancelledError("silence detection cancelled")
                if cancelled.is_set():
                    raise SilenceCancelledError("silence detection cancelled")
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
                # Trailing silence: ffmpeg emitted a ``silence_start`` but
                # never the matching ``silence_end`` because the input
                # ended while still silent. Previously we just dropped
                # the pending segment — that lost real trailing silence
                # and made the cut plan shorter than reality. When the
                # caller passed a known ``duration`` (always the case
                # for the canonical pipeline via ``_probe_duration``),
                # close the segment at the end of the media so the cut
                # plan reflects what the user actually heard. See P1.12
                # in the fix plan.
                if pending_start[0] is not None and duration is not None and duration > 0:
                    pending_start_t = pending_start[0]
                    # ``pending_start_t`` may already exceed duration
                    # (ffmpeg clamps the reported time to the actual
                    # packet PTS, which on a truncated file can be a
                    # hair past the probed container duration). Clamp
                    # start to duration so we don't emit a (start>end)
                    # segment; in that degenerate case the segment is
                    # dropped.
                    clamped_start = min(pending_start_t, duration)
                    if clamped_start < duration:
                        logger.info(
                            f"Trailing silence_start at t={pending_start_t:.3f}s "
                            f"had no matching silence_end; closing at media "
                            f"duration {duration:.3f}s"
                        )
                        progressive_segments.append(
                            SilenceSegment(clamped_start, duration)
                        )
                        if on_segment is not None:
                            on_segment(list(progressive_segments))
                    else:
                        logger.debug(
                            f"Trailing silence_start at t={pending_start_t:.3f}s "
                            f"is at/after duration {duration:.3f}s; dropping"
                        )
                elif pending_start[0] is not None:
                    logger.warning(
                        "Unmatched silence_start (no silence_end) and no "
                        "media duration available; dropped — ffmpeg output "
                        "may be truncated"
                    )
                return list(progressive_segments)
            # Batch path: _parse_ffmpeg_output already logs mismatched
            # starts/ends; trailing silence there is handled by the same
            # warning path (can't recover in batch mode without the
            # progressive state machine).
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
    assert stderr_pipe is not None
    stderr_lines: list[str] = []
    wait_for_drain = drain_stderr_lines(stderr_pipe, stderr_lines)
    drain_done = False

    try:
        with cancel_monitor(process, cancel_callback) as cancelled:
            if cancelled.is_set():
                raise SilenceCancelledError("audio extraction cancelled")
            process.wait(timeout=_SILENCE_TIMEOUT)
            if cancelled.is_set():
                raise SilenceCancelledError("audio extraction cancelled")
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

    Additionally, a segment whose start is exactly 0 (after ffmpeg clamps a
    negative itsoffset on input) is symmetrical: D and A-sample would both
    report ``(0, _)`` whether the true start was 0, -0.5, or -2.0. We don't
    *require* matching start-0 counts (a real source can legitimately start
    silent on both paths), but if one list has a start-0 segment that the
    other doesn't, that's a sign the negative shift is masked — so we still
    compare start times strictly, which a masked shift would violate.
    """
    if len(seg_a) != len(seg_b):
        return False

    starts_a = sorted(s.start for s in seg_a)
    starts_b = sorted(s.start for s in seg_b)

    return all(
        abs(a_start - b_start) <= tolerance
        for a_start, b_start in zip(starts_a, starts_b, strict=True)
    )


def apply_margin(segments: list[SilenceSegment], margin: float, duration: float | None = None) -> list[SilenceSegment]:
    """Apply margin and merge overlapping segments.

    Positive margin shrinks silence (keep more audio around phrases).
    Negative margin expands silence (remove more audio around phrases).

    ``duration`` (optional) is the source media duration in seconds — when
    supplied, ``end`` is clamped to it so a negative margin can't expand a
    silence segment past the right neighbour (which the detector marked as
    loud) or past the end of the media. ``start`` is always clamped to 0.
    Without this, ``apply_margin([(1, 2)], -10)`` would return ``(0, 12)``,
    inventing silence over audio the detector said was loud. When
    ``duration`` is None the right clamp is skipped (callers that don't
    know the duration keep their old behaviour — namely the GUI's preview
    overlay path, where over-expansion is harmless and `duration` may not
    be known).
    """
    if not segments:
        return segments

    expanded = []
    for seg in segments:
        start = max(0.0, seg.start + margin)
        end = seg.end - margin
        if duration is not None:
            end = min(end, float(duration))
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


def _save_cache(
    cache_path: Path,
    video_path: Path,
    segments: list[SilenceSegment],
    config: dict,
    *,
    indent: int | None = 2,
    fsync: bool = True,
) -> None:
    """Atomically write a silence cache to `cache_path`.

    The temp file is created in the same directory as `cache_path` so
    `os.replace` is atomic on the same filesystem. Parent directories
    are created if needed.

    Args:
        indent: JSON indent level (None for compact, default 2).
        fsync: Whether to fsync after writing (True for final cache,
               False for ephemeral resume checkpoints).

    Note: with ``fsync=False`` (resume checkpoint path), a kernel crash
    between ``json.dump`` and ``os.replace`` could leave the previous
    file's bytes partially overwritten on disk. ``os.replace`` is still
    atomic for the rename so the *name* always points at a complete file
    or the old one — but the data is not fsync'd so on-disk contents may
    lag. Resume cache is best-effort by design; the canonical final
    cache (``fsync=True``) is the durable source of truth.
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
            json.dump(data, f, indent=indent)
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        os.replace(tmp_path, cache_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def save_silence_cache(
    video_path: Path,
    segments: list[SilenceSegment],
    output_dir: Path,
    config: dict,
):
    cache_path = _get_cache_path(video_path, output_dir)
    _save_cache(cache_path, video_path, segments, config)
    logger.info(f"Silence cache saved to {cache_path}")


def load_silence_cache(
    video_path: Path,
    output_dir: Path,
    config: dict,
) -> list[SilenceSegment] | None:
    """Load the final silence cache for `video_path` if fresh and config-matching.

    Convenience wrapper around `_load_silence_cache_from_path` that
    constructs the canonical final cache path. Returns margin-applied
    segments (margin is part of the cache key, so any hit was built
    with this exact margin).
    """
    cache_path = _get_cache_path(video_path, output_dir)
    segments = _load_silence_cache_from_path(cache_path, video_path, config)
    if segments is not None:
        logger.info(f"Loaded {len(segments)} silence segments from cache")
    return segments


def _load_silence_cache_from_path(
    cache_path: Path,
    video_path: Path,
    config: dict,
) -> list[SilenceSegment] | None:
    """Load and validate a silence cache file at `cache_path`.

    Returns the margin-applied segments on success, ``None`` on any
    failure: file missing, source newer than cache, malformed JSON,
    config mismatch, or malformed segments. The final cache stores
    margin-applied results; for resume, the caller uses the raw
    progressive_segments directly (no cache load).
    """
    if not cache_path.exists():
        return None
    if cache_path.stat().st_mtime < video_path.stat().st_mtime:
        logger.info(f"Silence cache outdated (source file newer): {cache_path.name}")
        return None
    try:
        with open(cache_path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Could not read silence cache: {e}")
        return None
    for key in ("threshold", "min_silence", "margin"):
        if data.get("config", {}).get(key) != config.get(key):
            logger.info(f"Silence cache ignored: config mismatch ({key})")
            return None
    try:
        return [SilenceSegment(s["start"], s["end"]) for s in data["segments"]]
    except (KeyError, TypeError, ValueError) as e:
        logger.warning(f"Invalid silence cache: {e}")
        return None
