"""ffmpeg silencedetect driver: WAV extraction, batch + progressive runs."""

import logging
import queue
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

from stream2video import silence as _c
from stream2video.concat.constants import _OOM_HINT
from stream2video.silence.cache import _save_cache
from stream2video.silence.parser import (
    _RESUME_THROTTLE_N,
    _RESUME_THROTTLE_S,
    _SILENCE_TIMEOUT,
    SilenceCancelledError,
    SilenceDetectionError,
    SilenceOutOfMemoryError,
    SilenceParser,
    SilenceSegment,
    _noop_on_segment,
    _parse_ffmpeg_output,
)
from stream2video.tools import popen_with_retry
from stream2video.utils import (
    CANCEL_POLL_INTERVAL,
    cancel_monitor,
    drain_stderr_lines,
    kill_and_reap,
    looks_like_oom,
    no_window_kwargs,
    read_lines_queue,
    registered_process,
)

logger = logging.getLogger(__name__)


def _kill_and_raise(proc: subprocess.Popen, exc: BaseException) -> NoReturn:
    """Kill the ffmpeg child and reap it before propagating ``exc``.

    Thin wrapper around :func:`stream2video.utils.kill_and_reap` kept so
    the raise-from-None semantics stay at the call sites. On Windows
    ``kill()`` (TerminateProcess) is asynchronous, and letting the
    exception escape without a bounded ``wait()`` keeps the process
    handles — and any file the child had open — alive long enough for
    the caller's cleanup (unlink of a partial WAV, rmtree of a work
    dir) to trip WinError 32 (file busy). The 30s bound matches
    runner.py; a child that ignores the kill is un-reapable anyway.
    """
    kill_and_reap(proc)
    raise exc from None


def detect_silence_stream(
    input_path: Path,
    threshold: float,
    min_silence: float,
    *,
    on_segment: Callable[[list[SilenceSegment]], None] | None = None,
    timeout: int = _SILENCE_TIMEOUT,
    cancel_callback: Callable[[], bool] | None = None,
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

    ``cancel_callback`` is polled during the run (and checked after the
    process exits). When it returns True the ffmpeg child is killed and
    :class:`SilenceCancelledError` is raised instead of a misleading
    :class:`SilenceOutOfMemoryError` — a preview cancelled via
    ``cancel_process("preview")`` exits with rc=-9 (SIGKILL), which
    ``looks_like_oom`` would otherwise claim.

    Returns the final list of segments. Hard ffmpeg failures raise
    :class:`SilenceDetectionError`; the callback is not invoked on
    sources that have no parseable silencedetect output.
    """
    if on_segment is None:
        # Fast path: reuse the existing batch parser (production code).
        # IMPORTANT: forward ``timeout`` — a hung ffmpeg in the preview
        # otherwise sits on ``_run_silencedetect``'s default
        # ``_SILENCE_TIMEOUT`` (10h) because the progressive path's
        # deadline loop is the only place the parameter is honoured.
        return _run_silencedetect(
            input_path,
            threshold=threshold,
            min_silence=min_silence,
            duration=None,
            progress_callback=None,
            cancel_callback=cancel_callback,
            label="video (preview)",
            duration_limit=None,
            timeout=timeout,
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
        proc = popen_with_retry(
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

    # Unified parser. Previously this function had its own
    # inline ``m_s = _SILENCE_START_RE.search(line)`` loop with a
    # ``float()`` call that broke on decimal commas. Using
    # :class:`SilenceParser` here keeps the parsing logic in one place
    # so a future change (e.g. a new ``silence_duration`` field)
    # only needs to be made once.
    parser = SilenceParser(on_segment=on_segment)

    try:
        with (
            registered_process(proc, owner="preview"),
            cancel_monitor(proc, cancel_callback) as cancelled,
        ):
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
                    # Kill immediately: falling through to
                    # ``proc.wait(timeout=timeout)`` would block up to a
                    # SECOND full timeout (~10h by default) on an ffmpeg
                    # that has already proven itself hung. Bound the reap
                    # too: on Windows TerminateProcess is async and a
                    # wedged child can ignore it, leaving an unbounded
                    # wait() here blocked forever (the runner.py paths all
                    # use a 30s bound for the same reason).
                    _kill_and_raise(
                        proc,
                        SilenceDetectionError(
                            f"ffmpeg timeout after {timeout}s (no stderr output)"
                        ),
                    )
                try:
                    raw = line_queue.get(timeout=CANCEL_POLL_INTERVAL)
                except queue.Empty:
                    # No new line in the poll window — check cancel
                    # inline (mirrors ``_run_silencedetect``), then loop
                    # back and re-check the wall-clock deadline above.
                    # (The reader thread is the sole sender of None; a
                    # merely-exited process still has a trailing EOF
                    # sentinel to drain.)
                    if cancel_callback is not None and cancel_callback():
                        _kill_and_raise(proc, SilenceCancelledError("silence detection cancelled"))
                    if cancelled.is_set():
                        _kill_and_raise(proc, SilenceCancelledError("silence detection cancelled"))
                    continue
                if raw is None:
                    break  # EOF — reader saw the pipe close.
                line = raw.decode("utf-8", errors="replace")
                parser.feed(line)

            # stderr reached EOF — the process should already be exiting.
            # A short bounded wait is enough: waiting the full `timeout`
            # (up to 10h by default) after EOF would hang the preview
            # worker on a stuck ffmpeg, exactly the bug the deadline in
            # ``_run_silencedetect`` (process.wait(timeout=30)) guards
            # against. Mirror that short bound here.
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                _kill_and_raise(
                    proc,
                    SilenceDetectionError(
                        "ffmpeg silencedetect did not exit 30s after stderr EOF — killed"
                    ),
                )
            if proc.returncode != 0:
                # A kill from ``cancel_process("preview")`` (or the
                # cancel_monitor thread) lands here as rc=-9 on POSIX —
                # check the cancel flag BEFORE ``looks_like_oom`` claims
                # the kill as an OOM and the user sees "ffmpeg
                # silencedetect OOM" instead of a cancel.
                if (cancel_callback is not None and cancel_callback()) or cancelled.is_set():
                    raise SilenceCancelledError("silence detection cancelled")
                if looks_like_oom(proc.returncode, ""):
                    raise SilenceOutOfMemoryError(
                        f"ffmpeg silencedetect OOM (rc={proc.returncode}); {_OOM_HINT}"
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
        _kill_and_raise(proc, SilenceDetectionError(f"ffmpeg timeout after {e.timeout}s"))
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
    # `-copyts` is added ONLY when seeking (resume path): without it,
    # input `-ss` before `-i` resets the output PTS to zero, so
    # silencedetect would report timestamps *relative to the seek
    # point* instead of absolute source time — silently corrupting
    # the `initial + new` merge on real videos. With `-copyts`, ffmpeg
    # preserves the source PTS and the segments are directly
    # concatenable. It must NOT be added otherwise: on non-resume runs
    # it would turn ffmpeg's `-progress` out_time_us into an ABSOLUTE
    # source timestamp, making the progress jump to 100% (the
    # out_time_us already includes any source start offset while the
    # divisor stays the full duration), and it changes the meaning of
    # the `-t` limit used for sample-verification.
    cmd = [_c.ffmpeg_path(), "-progress", "pipe:1"]
    if resume_from is not None and resume_from > 0:
        cmd.append("-copyts")
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
    # will actually decode. NOTE: ``-copyts`` (added above for resume so
    # silencedetect timestamps stay in source PTS) also makes ffmpeg's
    # ``-progress`` out_time_us an ABSOLUTE source timestamp that starts
    # at ``resume_from``, not at 0 — so the resume offset must be
    # subtracted from the reported value as well as from the divisor.
    base_divisor = duration_limit if duration_limit is not None else duration
    progress_divisor: float | None
    progress_offset_us: float = 0.0
    if base_divisor is not None and resume_from is not None:
        # ``resume_from >= duration`` (a stale resume file asking to skip
        # past the end) would underflow the divisor to 0 and turn every
        # subsequent fraction into inf/NaN instead of a clean no-progress
        # signal. Treat a non-positive remainder as "nothing useful left"
        # and disable the callback — the caller's logging already explains
        # why no progress fires.
        remaining = base_divisor - resume_from
        progress_divisor = remaining if remaining > 0 else None
        progress_offset_us = resume_from * 1_000_000
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
        process = popen_with_retry(
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

    # Progressive parsing state. Only used when on_segment is set; the
    # batch path ignores this and re-parses the accumulated stderr at
    # the end. All segment-list mutation happens inside ``SilenceParser``
    # (parser.py — the unified parser), whose ``feed`` runs only on the
    # drain thread, so the segment list itself needs no lock. On resume
    # the parser is pre-seeded with the initial segments so the callback
    # sees the full picture from the first silence_end line and the
    # throttled save covers everything detected so far (initial + new),
    # not just the new ones.
    #
    # Throttled resume save state. Mutable lists let the closures update
    # them without `nonlocal` declarations.
    last_save_time: list[float] = [0.0]
    last_save_count: list[int] = [0]
    if resume_save_path is not None:
        last_save_time[0] = time.monotonic()

    # The resume save runs from TWO threads — the drain thread (a new
    # silence_end line) and the stdout loop (a moving probe frontier on
    # a clean source). Two concurrent ``_save_cache`` writes to the same
    # path would tear the checkpoint file, so the save is serialized.
    _save_lock = threading.Lock()

    def _maybe_save_resume() -> None:
        """Checkpoint the current segment list + probe position to disk
        if the throttle window has elapsed. Counts and timestamps tracked
        via mutable lists so the closure can update them without
        ``nonlocal``.
        """
        if resume_save_path is None or resume_save_config is None:
            return
        with _save_lock:
            segments = parser.segments
            new_count = len(segments) - len(initial_segments or [])
            now = time.monotonic()
            # Save when N new segments arrived OR the throttle interval
            # passed AND the probe moved (a clean source accumulates no
            # segments but still scans forward — that progress must be
            # checkpointed, or a multi-hour silent scan is lost on
            # cancel).
            moved = last_progress_pos[0] - (float(resume_from) if resume_from is not None else 0.0)
            if new_count <= 0 and not (moved > 0 and now - last_save_time[0] >= _RESUME_THROTTLE_S):
                return
            if (
                now - last_save_time[0] < _RESUME_THROTTLE_S
                and new_count - last_save_count[0] < _RESUME_THROTTLE_N
            ):
                return
            try:
                _save_cache(
                    resume_save_path,
                    input_path,
                    segments,
                    resume_save_config,
                    indent=None,
                    fsync=False,
                    probe_position=last_progress_pos[0],
                )
                last_save_time[0] = now
                last_save_count[0] = new_count
            except OSError as e:
                # Resume saves are best-effort — a failed checkpoint just
                # means the next run starts from a slightly earlier point.
                logger.warning(f"Resume cache save failed: {e}")

    def _on_new_segment(segments: list[SilenceSegment]) -> None:
        if on_segment is not None:
            on_segment(segments)
        _maybe_save_resume()

    parser = SilenceParser(on_segment=_on_new_segment)
    if initial_segments:
        parser.seed_segments(initial_segments)

    # Pre-seed the callback with the initial segments so the GUI's live
    # overlay is correct from the moment the pipeline starts. This is
    # the one exception to the "callback fires on the drain thread"
    # rule — the GUI's callback is thread-safe (lock-protected dict
    # update), and firing here closes the gap between the worker
    # pre-seeding to [] and the first silence_end line arriving.
    if initial_segments and on_segment is not None:
        on_segment(list(initial_segments))

    # Last decoded source position (absolute seconds, -copyts space).
    # Updated from ``out_time_us`` progress lines and recorded into resume
    # checkpoints as ``probe_position`` so a resume that found ZERO
    # segments still restarts from the probe frontier instead of t=0
    # (previously a clean source's hours-long scan was
    # lost because ``resume_from`` was derived only from segment ends).
    last_progress_pos: list[float] = [float(resume_from) if resume_from is not None else 0.0]

    if on_segment is not None:
        wait_for_drain = drain_stderr_lines(stderr_pipe, stderr_lines, on_line=parser.feed)
    else:
        wait_for_drain = drain_stderr_lines(stderr_pipe, stderr_lines)
    drain_done = False

    try:
        with registered_process(process), cancel_monitor(process, cancel_callback) as cancelled:
            # Use queue-based reader so cancel checks run between
            # reads without blocking on readline().
            line_queue, _reader_thread = read_lines_queue(stdout_pipe)
            # Wall-clock deadline measured from spawn — matches the
            # concat runner (runner.py). Without this a hung ffmpeg that
            # never closes stdout would block the read loop forever and
            # the ``timeout`` parameter (``silence_timeout``, 10h
            # default) would be dead code: ``process.wait(timeout=...)``
            # below is only reached AFTER stdout EOF.
            deadline = (time.monotonic() + timeout) if timeout else None
            while True:
                if deadline is not None and time.monotonic() > deadline:
                    _kill_and_raise(
                        process,
                        SilenceDetectionError(
                            f"ffmpeg silencedetect timeout after {timeout}s (no stdout EOF — killed)"
                        ),
                    )
                try:
                    raw_line = line_queue.get(timeout=CANCEL_POLL_INTERVAL)
                except queue.Empty:
                    # No new line — check cancel inline.
                    if cancel_callback is not None and cancel_callback():
                        _kill_and_raise(
                            process, SilenceCancelledError("silence detection cancelled")
                        )
                    if cancelled.is_set():
                        _kill_and_raise(
                            process, SilenceCancelledError("silence detection cancelled")
                        )
                    if deadline is not None and time.monotonic() > deadline:
                        _kill_and_raise(
                            process,
                            SilenceDetectionError(
                                f"ffmpeg silencedetect timeout after {timeout}s (no stdout EOF — killed)"
                            ),
                        )
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
                    _kill_and_raise(process, SilenceCancelledError("silence detection cancelled"))
                if cancelled.is_set():
                    _kill_and_raise(process, SilenceCancelledError("silence detection cancelled"))
                line = raw_line.decode("utf-8", errors="replace").strip()
                if line.startswith("out_time_us="):
                    try:
                        us = int(line.split("=", 1)[1])
                        last_progress_pos[0] = us / 1_000_000
                        # A moving probe frontier is itself worth
                        # checkpointing (clean source → zero segments but
                        # hours of progress); the throttle inside
                        # _maybe_save_resume keeps this cheap.
                        _maybe_save_resume()
                        if progress_callback and progress_divisor and progress_divisor > 0:
                            rel_us = max(0, us - int(progress_offset_us))
                            progress_callback(min(rel_us / 1_000_000 / progress_divisor, 1.0))
                    except (ValueError, IndexError):
                        pass
                if cancelled.is_set():
                    raise SilenceCancelledError("silence detection cancelled")

            if cancelled.is_set():
                raise SilenceCancelledError("silence detection cancelled")

            # stdout reached EOF — the process should already be exiting.
            # A short bounded wait is enough; if ffmpeg still refuses to
            # die, the wall-clock deadline above was about total runtime,
            # not about a post-EOF hang, so report it distinctly.
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                _kill_and_raise(
                    process,
                    SilenceDetectionError(
                        "ffmpeg silencedetect did not exit 30s after stdout EOF — killed"
                    ),
                )
            wait_for_drain()
            drain_done = True

            if process.returncode != 0:
                # Same guard the preview path has: an external kill
                # (cancel_process / cancel_monitor) can land here between
                # the drain loop's last cancelled.is_set() poll and this
                # rc check — rc=-9 with empty stderr would otherwise be
                # misread by looks_like_oom as "silencedetect OOM"
                # instead of a clean cancel.
                if (cancel_callback is not None and cancel_callback()) or cancelled.is_set():
                    raise SilenceCancelledError("silence detection cancelled")
                stderr_text = "".join(stderr_lines)
                if looks_like_oom(process.returncode, stderr_text):
                    raise SilenceOutOfMemoryError(
                        f"ffmpeg silencedetect OOM (rc={process.returncode}); {_OOM_HINT}"
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
                # plan reflects what the user actually heard.
                # ``SilenceParser.finalize`` implements this and
                # fires ``on_segment`` for the appended trailing segment.
                return parser.finalize(duration=duration)
            # Batch path: the parser closes a trailing silence_start at
            # the *effective* media duration when we know it. When a
            # ``duration_limit`` clip was applied (sample-verify probes
            # with ``-t``), the clip end — not the full container
            # duration — is the right ceiling: without it a trailing
            # silence_start inside the sample window is DROPPED by the
            # parser even though the source genuinely has it, and the
            # downstream sample-vs-full verify in silence/pipeline.py
            # then fires a false-positive mismatch (price: full re-detect
            # on the video, hours on a multi-hour VOD). Falls back to
            # the true ``duration`` and then to dropping the dangling
            # start (with a warning) when neither is known — the preview
            # path.
            effective_duration = duration_limit if duration_limit is not None else duration
            return _parse_ffmpeg_output("".join(stderr_lines), duration=effective_duration)

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

    # P0: only add ``-progress pipe:1`` when a caller actually consumes
    # stdout (progress_callback + duration known). When we don't read
    # stdout the pipe buffer fills (~64KB on Windows) and ffmpeg blocks
    # mid-write — the else-branch below would deadlock until the
    # ``timeout`` fires (~7min in a real 4:1 source observed).
    _progressive_wav = duration is not None and duration > 0 and progress_callback is not None
    cmd = [
        _c.ffmpeg_path(),
        "-y",
        "-copyts",
    ]
    if _progressive_wav:
        cmd.extend(["-progress", "pipe:1"])
    cmd.extend(
        [
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
    )

    try:
        logger.info(
            f"Extracting audio: {video_path.name} → {wav_path.name} "
            f"(16kHz mono pcm_s16le, -fflags +copyts)"
        )
        process = popen_with_retry(
            cmd,
            # stdout=DEVNULL when nobody consumes -progress; otherwise the
            # 64KB OS pipe buffers fill and ffmpeg blocks on write → hang
            # until timeout (see _progressive_wav above).
            stdout=(_c.subprocess.PIPE if _progressive_wav else _c.subprocess.DEVNULL),
            stderr=_c.subprocess.PIPE,
            bufsize=-1,
            **no_window_kwargs(),
        )
    except FileNotFoundError as e:
        raise SilenceDetectionError("ffmpeg not found in PATH") from e

    stdout_pipe = process.stdout
    stderr_pipe = process.stderr
    assert stderr_pipe is not None
    stderr_lines: list[str] = []
    wait_for_drain = drain_stderr_lines(stderr_pipe, stderr_lines)
    drain_done = False
    # Track non-clean exits so the partial WAV artifact is always removed.
    # A cancel / deadline-kill / wait-timeout raises BEFORE the unlink at
    # the rc!=0 branch; without this a fresh-mtime truncated WAV stays on
    # disk and ``_is_wav_cache_valid`` (mtime-only) treats it as a valid
    # cache on the next run — silently truncating every subsequent
    # silence-detection result until the user deletes the file by hand.
    completed = False

    try:
        with registered_process(process), cancel_monitor(process, cancel_callback) as cancelled:
            if _progressive_wav:
                assert stdout_pipe is not None
                q, _thr = read_lines_queue(stdout_pipe)
                deadline = None
                if timeout:
                    deadline = time.monotonic() + timeout
                while True:
                    if deadline is not None and time.monotonic() > deadline:
                        _kill_and_raise(
                            process,
                            SilenceDetectionError(f"ffmpeg extract timeout after {timeout}s"),
                        )
                    try:
                        raw = q.get(timeout=0.2)
                    except queue.Empty:
                        if cancelled.is_set() or (cancel_callback and cancel_callback()):
                            _kill_and_raise(
                                process, SilenceCancelledError("audio extraction cancelled")
                            )
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
                        except (ValueError, IndexError):
                            continue
                        # mypy: ``_progressive_wav`` guarantees both are
                        # non-None here, but narrowing through the flag
                        # doesn't reach this closure — check directly.
                        if progress_callback is not None and duration:
                            try:
                                progress_callback(min(us / 1_000_000 / duration, 1.0))
                            except Exception:
                                pass
                # Ensure process finished. This is a post-EOF hang, not
                # the wall-clock runtime limit — report it distinctly
                # instead of misleadingly citing the configured timeout.
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    _kill_and_raise(
                        process,
                        SilenceDetectionError(
                            "ffmpeg extract did not exit 5s after progress EOF — killed"
                        ),
                    )
            else:
                if cancelled.is_set():
                    raise SilenceCancelledError("audio extraction cancelled")
                process.wait(timeout=timeout)
            if cancelled.is_set():
                raise SilenceCancelledError("audio extraction cancelled")
            wait_for_drain()
            drain_done = True

            if process.returncode != 0:
                # Same cancel-before-OOM guard as the silencedetect paths:
                # an external kill landing in this window must surface as
                # a clean cancel, not "ffmpeg extract OOM".
                if (cancel_callback is not None and cancel_callback()) or cancelled.is_set():
                    raise SilenceCancelledError("audio extraction cancelled")
                stderr_text = "".join(stderr_lines)
                if looks_like_oom(process.returncode, stderr_text):
                    wav_path.unlink(missing_ok=True)
                    raise SilenceOutOfMemoryError(
                        f"ffmpeg extract OOM (rc={process.returncode}); {_OOM_HINT}"
                    )
                error_msg = stderr_text or "Unknown error"
                wav_path.unlink(missing_ok=True)
                raise SilenceDetectionError(f"ffmpeg extract failed: {error_msg}")
            # rc == 0 and no cancel: the WAV is complete and cacheable.
            completed = True
    except subprocess.TimeoutExpired as e:
        wav_path.unlink(missing_ok=True)
        _kill_and_raise(
            process, SilenceDetectionError(f"ffmpeg extract timeout after {e.timeout}s")
        )
    finally:
        if not drain_done:
            wait_for_drain()
        try:
            if stdout_pipe is not None:
                stdout_pipe.close()
        except Exception:
            pass
        stderr_pipe.close()
        if not completed:
            # Poisoned-cache guard: drop the partial WAV left behind by a
            # cancel / deadline kill / wait timeout. unlink() is a no-op
            # when the file was never created (e.g. Popen succeeded but
            # ffmpeg failed before opening the output). The verified
            # sidecar (if a stale one somehow exists) must go too —
            # an unverified WAV must never look cache-valid.
            try:
                wav_path.unlink(missing_ok=True)
            except OSError:
                pass
            from stream2video.silence.cache import clear_wav_verified

            clear_wav_verified(wav_path)


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
