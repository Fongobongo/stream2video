"""ffmpeg silencedetect driver: WAV extraction, batch + progressive runs."""

import logging
import queue
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from stream2video import silence as _c
from stream2video.silence.cache import _save_cache
from stream2video.silence.parser import (
    _RESUME_THROTTLE_N,
    _RESUME_THROTTLE_S,
    _SILENCE_END_RE,
    _SILENCE_START_RE,
    _SILENCE_TIMEOUT,
    SilenceCancelledError,
    SilenceDetectionError,
    SilenceOutOfMemoryError,
    SilenceParser,
    SilenceSegment,
    _noop_on_segment,
    _parse_ffmpeg_output,
    _to_float,
)
from stream2video.utils import (
    CANCEL_POLL_INTERVAL,
    cancel_monitor,
    drain_stderr_lines,
    looks_like_oom,
    no_window_kwargs,
    read_lines_queue,
    registered_process,
)

logger = logging.getLogger(__name__)


def detect_silence_stream(
    input_path: Path,
    threshold: float,
    min_silence: float,
    *,
    on_segment: Callable[[list[SilenceSegment]], None] | None = None,
    timeout: int = _SILENCE_TIMEOUT,
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
        _c.ffmpeg_path(),
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
        proc = _c.subprocess.Popen(
            cmd,
            stdout=_c.subprocess.DEVNULL,
            stderr=_c.subprocess.PIPE,
            bufsize=-1,
            **no_window_kwargs(),
        )
    except FileNotFoundError as e:
        raise SilenceDetectionError("ffmpeg not found in PATH") from e

    assert proc.stderr is not None
    pipe = proc.stderr

    # P2.5: unified parser. Previously this function had its own
    # inline ``m_s = _SILENCE_START_RE.search(line)`` loop with a
    # ``float()`` call that broke on decimal commas (P1.13). Using
    # :class:`SilenceParser` here keeps the parsing logic in one place
    # so a future change (e.g. a new ``silence_duration`` field)
    # only needs to be made once.
    parser = SilenceParser(on_segment=on_segment)

    try:
        with registered_process(proc, owner="preview"):
            # P1 audit v0.3 §5.2: replace the blocking
            # ``iter(pipe.readline, b"")`` with read_lines_queue +
            # ``get(timeout=...)``. A hung ffmpeg that stops writing to
            # stderr used to block readline indefinitely, so the ``timeout``
            # parameter never actually fired — preview hung forever. With
            # the queue + deadline poll, the ``timeout`` reaches wait()
            # below via the elapsed-time guard.
            line_queue, _reader_thread = read_lines_queue(pipe)
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # Force the wait() / TimeoutExpired path now; the loop
                    # below raises SilenceDetectionError on timeout.
                    break
                try:
                    raw = line_queue.get(timeout=CANCEL_POLL_INTERVAL)
                except queue.Empty:
                    if proc.poll() is not None:
                        # Process exited — let the next get drain the
                        # reader's trailing None (EOF).
                        continue
                    continue
                if raw is None:
                    break  # EOF — reader saw the pipe close.
                line = raw.decode("utf-8", errors="replace")
                parser.feed(line)

            proc.wait(timeout=timeout)
            if proc.returncode != 0:
                if looks_like_oom(proc.returncode, ""):
                    raise SilenceOutOfMemoryError(
                        f"ffmpeg silencedetect OOM (rc={proc.returncode}); "
                        "try --preset low_memory / lowering --memory-limit-mb"
                    )
                raise SilenceDetectionError(f"ffmpeg silencedetect failed (rc={proc.returncode})")
            # ``detect_silence_stream`` is a preview-only helper that
            # doesn't always know the media duration (callers may pass a
            # URL or a file we haven't probed). Pass ``duration=None`` so
            # a trailing ``silence_start`` is dropped with a warning
            # instead of guessing. Callers that know the duration can
            # post-process the returned list to add the trailing segment,
            # or call ``detect_silence`` instead (which probes the
            # duration and closes it via ``SilenceParser.finalize``).
            return parser.finalize(duration=None)
    except subprocess.TimeoutExpired as e:
        proc.kill()
        raise SilenceDetectionError(f"ffmpeg timeout after {e.timeout}s") from e
    finally:
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
    timeout: int = _SILENCE_TIMEOUT,
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
    cmd = [_c.ffmpeg_path(), "-copyts", "-progress", "pipe:1"]
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
    if progress_callback is not None and (progress_divisor is None or progress_divisor <= 0):
        logger.info(
            "Silence progress disabled for %s: input duration is unavailable from ffprobe",
            label,
        )

    try:
        logger.info(
            f"Running ffmpeg silencedetect on {label}: "
            f"threshold={threshold}dB ({noise}), min_silence={min_silence}s"
            + (f", resume_from={resume_from:.2f}s" if resume_from else "")
        )
        process = _c.subprocess.Popen(
            cmd,
            stdout=_c.subprocess.PIPE,
            stderr=_c.subprocess.PIPE,
            bufsize=-1,
            **no_window_kwargs(),
        )
    except FileNotFoundError as e:
        raise SilenceDetectionError("ffmpeg not found in PATH") from e

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
        with registered_process(process), cancel_monitor(process, cancel_callback) as cancelled:
            # P1.5: use queue-based reader so cancel checks run between
            # reads without blocking on readline().
            line_queue, _reader_thread = read_lines_queue(stdout_pipe)
            while True:
                try:
                    raw_line = line_queue.get(timeout=CANCEL_POLL_INTERVAL)
                except queue.Empty:
                    # No new line — check cancel inline.
                    if cancel_callback is not None and cancel_callback():
                        process.kill()
                        raise SilenceCancelledError("silence detection cancelled") from None
                    if cancelled.is_set():
                        raise SilenceCancelledError("silence detection cancelled") from None
                    continue
                if raw_line is None:
                    break  # EOF
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

            process.wait(timeout=timeout)
            wait_for_drain()
            drain_done = True

            if process.returncode != 0:
                stderr_text = "".join(stderr_lines)
                if looks_like_oom(process.returncode, stderr_text):
                    raise SilenceOutOfMemoryError(
                        f"ffmpeg silencedetect OOM (rc={process.returncode}); "
                        "try --preset low_memory / lowering --memory-limit-mb"
                    )
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
                        progressive_segments.append(SilenceSegment(clamped_start, duration))
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
        stdout_pipe.close()
        stderr_pipe.close()


def _extract_audio_wav(
    video_path: Path,
    wav_path: Path,
    cancel_callback: Callable[[], bool] | None = None,
    timeout: int = _SILENCE_TIMEOUT,
    progress_callback: Callable[[float], None] | None = None,
    duration: float | None = None,
) -> None:
    """Extract audio from `video_path` to a 16kHz mono PCM WAV at `wav_path`.

    Uses `-fflags +copyts` to preserve input PTS so that timestamps in the WAV
    match the original video's timeline (required for silence detection results
    to align with the video when used as cut points in cut_and_concat).

    The WAV is the cached artifact for the D (audio-only) path. On broken-PTS
    sources the verification pass at the call site detects the mismatch and
    deletes this file.

    When ``progress_callback`` + ``duration`` are given, reports 0..1 via
    ``-progress pipe:1`` (used for Silence phase thin bar) so the WAV
    extraction does not look frozen on 15GB sources.
    """
    wav_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        _c.ffmpeg_path(),
        "-y",
        "-copyts",
        "-progress",
        "pipe:1",
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
        process = _c.subprocess.Popen(
            cmd,
            stdout=_c.subprocess.PIPE,
            stderr=_c.subprocess.PIPE,
            bufsize=-1,
            **no_window_kwargs(),
        )
    except FileNotFoundError as e:
        raise SilenceDetectionError("ffmpeg not found in PATH") from e

    stdout_pipe = process.stdout
    stderr_pipe = process.stderr
    assert stdout_pipe is not None and stderr_pipe is not None
    stderr_lines: list[str] = []
    wait_for_drain = drain_stderr_lines(stderr_pipe, stderr_lines)
    drain_done = False

    try:
        with registered_process(process), cancel_monitor(process, cancel_callback) as cancelled:
            if duration is not None and duration > 0 and progress_callback is not None:
                from stream2video.utils import read_lines_queue as _rlq

                q, _thr = _rlq(stdout_pipe)
                deadline = None
                if timeout:
                    import time as _time

                    deadline = _time.monotonic() + timeout
                while True:
                    if deadline is not None:
                        import time as _time

                        if _time.monotonic() > deadline:
                            process.kill()
                            raise SilenceDetectionError(f"ffmpeg extract timeout after {timeout}s")
                    try:
                        raw = q.get(timeout=0.2)
                    except queue.Empty:
                        if cancelled.is_set() or (cancel_callback and cancel_callback()):
                            process.kill()
                            raise SilenceCancelledError("audio extraction cancelled") from None
                        if process.poll() is not None:
                            break
                        continue
                    if raw is None:
                        break
                    if cancelled.is_set():
                        raise SilenceCancelledError("audio extraction cancelled")
                    try:
                        line = raw.decode("utf-8", errors="replace").strip()
                    except Exception:
                        continue
                    if line.startswith("out_time_us="):
                        try:
                            us = int(line.split("=", 1)[1])
                            progress_callback(min(us / 1_000_000 / duration, 1.0))
                        except Exception:
                            pass
                # Ensure process finished
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    raise SilenceDetectionError(f"ffmpeg extract timeout after {timeout}s") from None
            else:
                if cancelled.is_set():
                    raise SilenceCancelledError("audio extraction cancelled")
                process.wait(timeout=timeout)
            if cancelled.is_set():
                raise SilenceCancelledError("audio extraction cancelled")
            wait_for_drain()
            drain_done = True

            if process.returncode != 0:
                stderr_text = "".join(stderr_lines)
                if looks_like_oom(process.returncode, stderr_text):
                    wav_path.unlink(missing_ok=True)
                    raise SilenceOutOfMemoryError(
                        f"ffmpeg extract OOM (rc={process.returncode}); "
                        "try --preset low_memory / lowering --memory-limit-mb"
                    )
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
        try:
            stdout_pipe.close()
        except Exception:
            pass
        stderr_pipe.close()


def _sample_segments_match(
    seg_a: list[SilenceSegment],
    seg_b: list[SilenceSegment],
    tolerance: float = 0.05,
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
