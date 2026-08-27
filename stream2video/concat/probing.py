"""ffprobe-based validity helpers for resume-skip logic."""

import contextlib
import logging
import math
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from stream2video.concat.errors import CancelledError
from stream2video.tools import ffmpeg_path, popen_with_retry
from stream2video.utils import (
    _run_ffprobe,
    drain_stderr_lines,
    ffprobe_show_entries_args,
    kill_and_reap,
    registered_process,
    subprocess_kwargs,
)

logger = logging.getLogger(__name__)

# stderr markers a decode treats as failure even when ffmpeg exits 0
# (a truncated payload warns "partial file" and still returns rc=0 —
# observed live, audit round 28 P1).
_TRUNCATION_MARKERS = (
    "partial file",
    "moov atom not found",
    "packet corrupt",
    "invalid data",
    "error while decoding",
    "truncated",
)

# Hard ceiling for a single metadata probe (audit round 32 P2). ffprobe
# on a readable container answers in well under a second; a probe that
# hangs for the old 30 s ceiling is almost always stalled on a wedged
# source (network path, broken pipe). The whole-stream decode is the
# expensive step and has its own caller-bounded timeout; probes must not
# be the thing that keeps a cancelled run alive. Ten seconds is far above
# any sane local read and far below a visibly frozen GUI.
# NOTE: the cancellable runner ``_run_ffprobe`` (and its
# ``_PROBE_TIMEOUT`` / ``_PROBE_POLL_SECONDS`` constants) now live in
# ``stream2video.utils`` — the same runner serves the legacy sync
# helpers there (audit round 36 P2); this module re-imports it.


def _ffprobe_is_valid_media(
    path: Path, stream_type: str = "v", cancel_callback: "Callable[[], bool] | None" = None
) -> bool:
    """Quick validity check: ffprobe can read codec + duration for the
    requested stream type.

    Used by the unified media gate to reject a chunk that exists and is
    large enough but is internally corrupt (e.g. ffmpeg crashed
    mid-write and the moov atom is missing). Without this, the concat
    demuxer would accept the file but emit a broken segment in the
    middle of the output.

    ``stream_type`` selects the ffprobe ``-select_streams`` filter:
    ``"v"`` for video segments (the historical default, used by the
    concat segment/cut/raw paths) and ``"a"`` for audio segments
    (audio-extract resume — an audio-only file has no video stream and
    would otherwise fail video validation → resume always re-encoded
    everything, see the P0 audit in the v0.3 release plan).

    Error contract (audit round 32 P1): only a normal ffprobe VERDICT is
    turned into a bool — ``returncode != 0`` or an empty stream list
    means invalid media. Infrastructure faults (timeout, spawn failure)
    PROPAGATE instead of becoming ``False``: "validation unavailable"
    must never be mistaken for "invalid media" — turning a transient
    ffprobe hiccup into ``False`` once made the controller delete a
    fully downloaded multi-GB file (the ``_ffprobe_media_complete`` /
    download gate already had this contract; the secondary-stream probe
    inside the unified gate did not). Callers that want the fail-safe
    re-encode fallback (resume gates, the gapless tree) catch around
    the call themselves. ``cancel_callback`` reaches the probe loop —
    a cancel fires immediately instead of after the 10 s ceiling
    (audit round 33 P2).
    """
    rc, stdout = _run_ffprobe(
        ffprobe_show_entries_args(
            path,
            "stream=codec_name",
            select_streams=stream_type,
        ),
        cancel_callback=cancel_callback,
    )
    return rc == 0 and bool(stdout.strip())


def _ffprobe_media_complete(
    path: Path, stream_type: str = "v", cancel_callback: "Callable[[], bool] | None" = None
) -> bool:
    """Strict freshness check for a file about to be PUBLISHED as the
    stable source (audit round 27 P2): ``_ffprobe_is_valid_media``
    proves only that the requested stream has a readable codec name —
    a file with a valid header but a truncated/corrupt body passes it,
    and the publish would then atomically replace the previous good
    copy with garbage. This probe additionally requires a finite,
    non-zero container duration, which a truncated body cannot supply
    (ffprobe reads the duration from the actual media, not from a
    header field alone).

    Fail-closed like its sibling: non-zero rc, empty output and
    unparseable output all return False — the caller must then refuse
    to publish. Exceptions of the "ffprobe cannot run at all" kind
    (transient spawn failure, a hung probe hitting the ceiling) are NOT
    swallowed here: they re-raise so the caller can distinguish
    "invalid media" from "validation unavailable" and must not delete
    the download (audit round 27 P1). ``cancel_callback`` reaches the
    probe loop like the codec probe's (audit round 34 P1-1 — the
    callback was accepted but dropped before the runner).
    """
    rc, stdout = _run_ffprobe(
        ffprobe_show_entries_args(
            path,
            "stream=codec_name:format=duration",
            select_streams=stream_type,
        ),
        cancel_callback=cancel_callback,
    )
    if rc != 0:
        return False
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines or not lines[0]:
        return False
    duration: float | None = None
    for line in lines[1:]:
        try:
            value = float(line)
        except ValueError:
            continue
        if math.isfinite(value) and value > 0:
            duration = value
            break
    return duration is not None


def _parse_ffprobe_duration(text: str) -> float | None:
    """Parse one ffprobe duration line to seconds.

    Two shapes arrive from ``-show_entries stream=duration:stream_tags=DURATION``:
      * bare seconds — ``2.008`` (MP4/MOV stream durations);
      * a Matroska/WebM ``TAG:DURATION`` — ``00:00:02.008000000``
        (``HH:MM:SS.fraction``; the stream-level ``duration`` is ``N/A``
        for these containers, so the tag is the only stream duration
        source — audit round 35 P0).

    Returns ``None`` for N/A / garbage / non-finite values — the same
    negative-verdict contract as the pre-fallback parse.
    """
    text = text.strip()
    if not text or text.upper() == "N/A":
        return None
    if ":" in text:
        parts = text.split(":")
        if len(parts) != 3:
            return None
        try:
            seconds = float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        except ValueError:
            return None
    else:
        try:
            seconds = float(text)
        except ValueError:
            return None
    return seconds if math.isfinite(seconds) else None


def _ffprobe_stream_timing(
    path: Path,
    stream_type: str,
    cancel_callback: "Callable[[], bool] | None" = None,
) -> tuple[float | None, float | None]:
    """(start_time, duration) of the FIRST stream of the requested type
    (v/a), either component None for a genuine negative verdict
    (audit rounds 32/33/35/36 P1).

    Container duration cannot detect an audio track truncated much
    earlier than the video (audit round 30 P5): the stream-level
    comparison between video and audio tracks is what catches a
    12 s video carrying a 2 s audio track.

    Fail-closed on ambiguity (audit round 32 P1): the selector is
    ``<type>:0`` so exactly ONE stream is probed — a multi-track
    container previously made ``-select_streams a`` emit one value per
    track, ``float(r.stdout.strip())`` failed to parse, and the
    resulting ``None`` let the duration mismatch check silently pass.
    ``-select_streams a:0`` also makes the probe agree with the decode
    gate, which ``-map``s ``0:v:0`` / ``0:a:0``.

    Duration sources, in order: the numeric ``stream=duration`` field;
    then the Matroska/WebM ``TAG:DURATION`` stream tag
    (``HH:MM:SS.fraction`` — ``stream=duration`` is ``N/A`` for these
    containers, audit round 35 P0); then ``None``. ``start_time`` is
    read from ``stream=start_time`` (``N/A``/absent → None), so the
    gate can compare track ENDS: MPEG-PS tracks legitimately start at
    different timestamps and a duration-only comparison falsely rejects
    healthy files (audit round 36 P1).

    Error contract (audit round 33 P1): ``None`` is ONLY a normal
    ffprobe verdict — non-zero rc, an empty / ``N/A`` / non-numeric
    value. Infrastructure faults (timeout, spawn failure, cancel)
    PROPAGATE, exactly like the codec probe: a transient ffprobe hiccup
    returning ``None`` here once fell into the fail-closed branch of
    ``_media_is_valid`` and made the controller delete a fully
    downloaded multi-GB file — "validation unavailable" must never be
    mistaken for "invalid media". ``_media_is_valid``'s ``fail_safe``
    covers this probe the same way it covers the codec probe.
    """
    rc, stdout = _run_ffprobe(
        ffprobe_show_entries_args(
            path,
            "stream=start_time,duration:stream_tags=DURATION",
            select_streams=f"{stream_type}:0",
        ),
        cancel_callback=cancel_callback,
    )
    if rc != 0:
        return None, None
    # Line order is positional: ``start_time``, then ``duration``, then
    # the tag (verified live on MP4/MKV/WebM/FLV/MPEG-PS): first line →
    # start (N/A → None), second line → duration (N/A → None, bare
    # seconds), any following line → TAG:DURATION fallback for duration
    # (a duration that is N/A at stream level never becomes None while
    # a tag still carries it).
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        return None, None
    start = _parse_ffprobe_duration(lines[0])
    duration: float | None = None
    if len(lines) >= 2:
        duration = _parse_ffprobe_duration(lines[1])
    if duration is None:
        for line in lines[2:]:
            value = _parse_ffprobe_duration(line)
            if value is not None:
                duration = value
                break
    return start, duration


def _ffprobe_nominal_fps(
    path: Path,
    cancel_callback: "Callable[[], bool] | None" = None,
) -> float | None:
    """Nominal frame rate (``r_frame_rate``) of the first video stream,
    or None when it cannot be determined (audit: benchmark 2026-08 P0).

    Feeds the frame-hole check in ``_media_is_valid``: the expected
    decoded frame count is ``decoded_seconds * nominal fps``. The
    nominal rate is what the container/encoder DECLARED (``60/1`` on a
    60 fps encode) — a file whose video body is mostly missing still
    carries the declaration, so a decoded count far below the
    expectation exposes the hole. ``avg_frame_rate`` is NOT used: it is
    ``nb_frames / duration`` from the same possibly-lying moov and is
    circular for this check.

    Error contract matches ``_ffprobe_stream_timing``: ``None`` is ONLY
    a normal ffprobe verdict (non-zero rc, empty/``N/A``/non-numeric/
    zero rate); infrastructure faults (timeout, spawn failure, cancel)
    PROPAGATE so "validation unavailable" is never mistaken for
    "invalid media". The caller treats ``None`` as "frame-hole check
    skipped" — an unmeasurable rate must not reject media on its own.
    """
    rc, stdout = _run_ffprobe(
        ffprobe_show_entries_args(
            path,
            "stream=r_frame_rate",
            select_streams="v:0",
        ),
        cancel_callback=cancel_callback,
    )
    if rc != 0:
        return None
    value = stdout.strip()
    if not value or value == "N/A":
        return None
    # r_frame_rate is a fraction ("60/1", "30000/1001") or a bare number.
    try:
        if "/" in value:
            num, den = value.split("/", 1)
            den_f = float(den)
            if den_f <= 0:
                return None
            fps = float(num) / den_f
        else:
            fps = float(value)
    except ValueError:
        return None
    return fps if fps > 0 else None


def _ffmpeg_decode_timing(
    path: Path,
    stream_type: str = "v",
    timeout: float = 43200,
    cancel_callback: "Callable[[], bool] | None" = None,
    *,
    low_process_priority: bool = False,
    rlimit_as_mb: int = 0,
    frame_count_out: "list[int] | None" = None,
) -> tuple[bool, float | None]:
    """Decode the WHOLE requested stream into the null sink; return
    ``(ok, decoded_seconds)``.

    ``ok`` is the integrity verdict; ``decoded_seconds`` is the ACTUAL
    amount of media the decoder consumed (last ``out_time_ms`` from
    ``-progress pipe:1``), or None when it could not be measured. The
    measured length is what the both-streams gate falls back to when
    the container carries NO stream-level durations at all (FLV and
    friends, audit round 37 P1): a container duration cannot
    distinguish a healthy FLV from one whose audio track is 0.1 s
    against 12 s of video — both tracks "exist" and each present
    fragment decodes cleanly — but the DECODED lengths expose the
    truncation (verified live: healthy FLV decodes to ~2.033 s video
    / ~2.026 s audio; a truncated-audio FLV measures ~12 s vs ~0.1 s).

    Head+tail probes cannot prove body integrity: zeroing a 256-byte
    window in the MIDDLE of an MP4 leaves the header, head decode and
    tail decode all clean (reproduced live, audit round 29 P3/P4). A
    full decode is the only check that reads every packet, so it is
    used by the fresh-download publish gate, every resume-reuse
    decision and the final-output validation.

    Hardened for long media (audit round 30 P7/P8, round 31 P1):

      * ``-xerror`` — stop at the FIRST decode error instead of
        printing a per-packet error storm from a damaged multi-hour
        file;
      * stdout (the progress feed) and stderr both go through the
        shared ring-bounded drain (``drain_stderr_lines``), never
        ``capture_output`` — the integrity check itself must not be
        the OOM vector;
      * the wait loop polls ``cancel_callback`` — a user Cancel kills
        ffmpeg immediately (raises ``CancelledError``) instead of
        holding the pipeline for up to the 12 h ceiling;
      * ``timeout`` is the caller's config (phase timeout), bounded
        by a hard ceiling;
      * the subprocess honours the caller's resource policy
        (``low_process_priority`` / ``rlimit_as_mb`` via
        ``subprocess_kwargs``) and is registered in the shared process
        registry (``registered_process``) so the GUI's shutdown kill
        covers it like every other pipeline ffmpeg;
      * ONE unconditional ``finally`` tears the child down on EVERY
        exit path — normal completion, cancel, timeout and an
        exception raised BY the cancel callback alike: kill-if-running
        via ``kill_and_reap`` (bounded reap — no lingering handles or
        zombies), both pipes closed, drain-thread joins (audit round
        31 P1-1/P1-2, round 34 P1-2).

    Spawn faults, timeout and cancellations re-raise: "validation
    unavailable" must never be mistaken for "invalid media".

    ``frame_count_out`` (benchmark 2026-08 P0): when a list is passed,
    the FINAL decoded frame count (last ``frame=`` line of the same
    ``-progress`` feed) is appended to it. The caller compares it
    against ``decoded_seconds * nominal fps`` to catch PTS-hole files:
    a part whose video frames are mostly missing still decodes without
    errors and reports a full ``out_time_ms`` (the tail PTS survives),
    so the duration-based gates pass it — only the frame count exposes
    the hole (a real VOD resume part lost 92% of its video this way and
    sailed through every previous check). The count is best-effort: a
    missing/garbled ``frame=`` feed simply appends nothing, and the
    caller must treat "no count" as "check skipped", never as failure.
    """
    cmd = [
        ffmpeg_path(),
        "-v",
        "error",
        "-xerror",
        "-i",
        str(path),
        "-map",
        "0:v:0" if stream_type == "v" else "0:a:0",
        "-f",
        "null",
        "-",
        "-progress",
        "pipe:1",
    ]
    proc = popen_with_retry(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **subprocess_kwargs(low_process_priority, rlimit_as_mb),
    )
    decoded_ms: list[str] = []
    frame_counts: list[str] = []

    def _capture_out_time_ms(line: str) -> None:
        if line.startswith("out_time_ms="):
            decoded_ms.append(line.split("=", 1)[1].strip())
        elif line.startswith("frame="):
            frame_counts.append(line.split("=", 1)[1].strip())

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    assert proc.stdout is not None and proc.stderr is not None
    wait_for_stdout = drain_stderr_lines(proc.stdout, stdout_chunks, on_line=_capture_out_time_ms)
    wait_for_stderr = drain_stderr_lines(proc.stderr, stderr_chunks)
    deadline = time.monotonic() + timeout
    with registered_process(proc):
        try:
            while True:
                try:
                    proc.wait(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if cancel_callback is not None and cancel_callback():
                    raise CancelledError("media validation cancelled")
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(cmd, timeout)
        finally:
            # Unconditional child teardown (audit round 31 P1-1/P1-2):
            # no matter HOW we leave the loop — finished, cancelled,
            # timed out, or an exception from the cancel callback —
            # the child must be killed (if still alive), reaped, its
            # pipes closed and the drain threads joined. Only then can
            # the exception propagate. Each drain is joined to EOF
            # BEFORE its pipe is closed (audit round 34 P1-2 — the
            # close raced the still-reading drain thread and lost
            # buffered output).
            if proc.poll() is None:
                with contextlib.suppress(OSError):
                    kill_and_reap(proc, timeout=5.0)
                if proc.poll() is None:
                    logger.warning("media validation process did not exit after kill: %s", path)
            for pipe, drain in (
                (proc.stdout, wait_for_stdout),
                (proc.stderr, wait_for_stderr),
            ):
                if not drain(2.0):
                    with contextlib.suppress(OSError, ValueError):
                        pipe.close()
                    drain(0.5)
                with contextlib.suppress(OSError, ValueError):
                    pipe.close()
    if proc.returncode != 0:
        return False, None
    stderr = "".join(stderr_chunks).lower()
    ok = not any(marker in stderr for marker in _TRUNCATION_MARKERS)
    decoded_seconds: float | None = None
    if decoded_ms:
        try:
            decoded_seconds = float(decoded_ms[-1]) / 1_000_000.0
        except ValueError:
            decoded_seconds = None
    if frame_count_out is not None and frame_counts:
        try:
            frame_count_out.append(int(frame_counts[-1]))
        except ValueError:
            pass  # garbage feed — caller treats "no count" as "skip"
    return ok, decoded_seconds


# Tolerance shared by the two duration gates below. Fixed 2 s was a
# huge hole for short media: a 1.8 s video carrying a 0.1 s audio
# track (nearly the whole audio body lost) passed the gate. The
# allowance is now bounded BOTH ways:
#   * never above 2 s (a six-hour encode's legitimate mux/flush drift
#     stays accepted — audit round 30 P5's counterexample),
#   * never below 150 ms (AAC priming + frame rounding on very short
#     clips — a healthy 1 fps/short encode routinely reports a few
#     frames of stream-duration offset; the floor was raised from
#     100 ms because MPEG-PS muxers pad their per-track end times with
#     a half-frame more slack than MP4 does, and a healthy 2.5 s MPG
#     once reported a 100.5 ms end offset — audit round 36 P1),
#   * and 2 % of the expected/longer duration in between (proportional
#     to the amount of content that could actually be missing).
# The A/V drift gate (audit rounds 30 P5 / 33 P1 — now comparing
# track ENDS, audit round 36 P1: MPEG-PS tracks start at different
# timestamps by design, so raw durations can differ by half a second
# on a perfectly healthy file while their ends stay aligned) and the
# resume-part duration gate (audit round 35 P1 — a fixed 1 s slack
# accepted a 0.8 s part holding only 0.1 s, losing 87.5 % of the
# fragment) both derive their allowed offset from these constants.
_DRIFT_FLOOR_SECONDS = 0.15
_DRIFT_CEILING_SECONDS = 2.0
_DRIFT_RELATIVE = 0.02


def _allowed_stream_drift(video_dur: float, audio_dur: float) -> float:
    """Bounded absolute+relative tolerance for the v/a duration match
    (audit round 33 P1) — see the constants above for the rationale."""
    return min(
        _DRIFT_CEILING_SECONDS,
        max(_DRIFT_FLOOR_SECONDS, max(video_dur, audio_dur) * _DRIFT_RELATIVE),
    )


# Frame-hole gate (benchmark 2026-08 P0, findings #6/#7): a video whose
# decoded frame count falls below this fraction of
# ``decoded_seconds * nominal fps`` is rejected. The real corruption that
# motivated the gate kept 19% of its frames (segment path) and 14%
# (batch path) — both far below any threshold; 0.5 catches catastrophic
# loss with a wide margin while staying lenient enough for encoder flush
# rounding and mild cadence variance. Deliberately NOT tighter: the gate
# also guards the final output, where a false positive throws away a
# good multi-hour encode, and genuinely VFR content can average well
# below its nominal rate (such sources are gated with the check off).
_FRAME_HOLE_MIN_RATIO = 0.5


def _media_is_valid(
    path: Path,
    *,
    require_video: bool,
    require_audio: bool,
    timeout: float = 43200,
    cancel_callback: "Callable[[], bool] | None" = None,
    low_process_priority: bool = False,
    rlimit_as_mb: int = 0,
    fail_safe: bool = False,
    check_frame_holes: bool = False,
) -> bool:
    """Unified integrity gate for the EXPECTED stream set of ``path``.

    One shared validator for the three gates that previously drifted
    (audit round 31 P1-4): the fresh-download publish, the staged
    final output and — through ``_media_is_valid`` re-exported on the
    concat facade — resume part reuse. Per required stream: the stream
    must exist (codec probe) and fully decode into the null sink; when
    BOTH streams are required, their track ENDS (start + duration)
    must agree within a bounded tolerance — a 12 s video carrying a
    2 s audio track is a truncated audio body, not media (audit round
    30 P5; a 1.8 s video carrying a 0.1 s audio track must ALSO fail —
    audit round 33 P1, ``_allowed_stream_drift``); MPEG-PS-style
    containers whose tracks start at different timestamps stay
    accepted because the muxer keeps ends aligned (audit round 36
    P1). Streams whose durations are genuinely N/A (FLV and friends)
    are compared by their DECODED lengths instead (audit round 37 P1):
    the decode that already ran measures how much media each track
    actually carries (``out_time``), and a container duration cannot
    do that — a 12 s video with a 0.1 s audio track passes a codec
    probe + per-fragment decode + positive-container check, but its
    measured lengths expose the truncation.

    ``require_video`` / ``require_audio`` are chosen by the caller:
      * audio-only output → audio only;
      * video output from an audio-carrying source → both;
      * a genuinely video-only source → video only.

    Error contract (audit rounds 32/33 P1): a METADATA-PROBE
    infrastructure fault (ffprobe timeout / spawn failure) RAISES
    instead of returning False — "validation unavailable" must never
    be mistaken for "invalid media" (a transient ffprobe hiccup once
    made the fresh-download gate delete a multi-GB completed
    download). The ``fail_safe`` fallback covers BOTH metadata probes
    uniformly — the codec probe AND the stream-timing probe (round 32
    only covered the codec one, so a transient duration-probe fault
    still fell into the fail-closed branch and deleted the download) —
    while decode cancellations / phase timeouts and spawn faults out
    of ``_ffmpeg_decode_timing`` always propagate regardless of it.

    ``check_frame_holes`` (benchmark 2026-08 P0): additionally reject a
    video whose DECODED FRAME COUNT is far below ``decoded_seconds *
    nominal fps``. Duration-based checks cannot see a PTS-hole file: a
    part that lost 92% of its video frames still decodes cleanly and
    reports a full ``out_time_ms`` (the surviving tail carries the last
    timestamp), so every previous gate passed it and the corrupt part
    was published into the final output (real VOD benchmark, findings
    #6/#7). The check is OPT-IN: it is safe for pipeline-encoded parts
    and outputs (CFR, declared fps is the true cadence) but would
    false-positive on genuinely VFR sources, so the fresh-download gate
    leaves it off while the resume gates and the final-output gate
    enable it. The threshold ``_FRAME_HOLE_MIN_RATIO`` is deliberately
    lenient (catches catastrophic loss, tolerates encoder flush
    rounding); an unmeasurable fps or frame count SKIPS the check
    rather than rejecting — the other gates still apply.
    """
    if not require_video and not require_audio:
        return True
    video_dur: float | None = None
    audio_dur: float | None = None
    video_measured: float | None = None
    audio_measured: float | None = None
    if require_video:
        if cancel_callback is not None and cancel_callback():
            raise CancelledError("media validation cancelled")
        try:
            if not _ffprobe_is_valid_media(path, stream_type="v", cancel_callback=cancel_callback):
                return False
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            if not fail_safe:
                raise
            return False
        video_frame_counts: list[int] = []
        video_ok, video_measured = _ffmpeg_decode_timing(
            path,
            stream_type="v",
            timeout=timeout,
            cancel_callback=cancel_callback,
            low_process_priority=low_process_priority,
            rlimit_as_mb=rlimit_as_mb,
            frame_count_out=video_frame_counts if check_frame_holes else None,
        )
        if not video_ok:
            return False
        if (
            check_frame_holes
            and video_frame_counts
            and video_measured is not None
            and video_measured > 0
        ):
            try:
                nominal_fps = _ffprobe_nominal_fps(path, cancel_callback=cancel_callback)
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                if not fail_safe:
                    raise
                nominal_fps = None
            if nominal_fps is not None:
                expected_frames = video_measured * nominal_fps
                if (
                    expected_frames > 0
                    and video_frame_counts[0] < expected_frames * _FRAME_HOLE_MIN_RATIO
                ):
                    logger.warning(
                        "frame-hole gate rejected %s: decoded %d frames, "
                        "expected ~%.0f at %.3f fps (%.1f%% missing)",
                        path,
                        video_frame_counts[0],
                        expected_frames,
                        nominal_fps,
                        (1.0 - video_frame_counts[0] / expected_frames) * 100.0,
                    )
                    return False
        if cancel_callback is not None and cancel_callback():
            raise CancelledError("media validation cancelled")
        try:
            video_start, video_dur = _ffprobe_stream_timing(
                path, "v", cancel_callback=cancel_callback
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            if not fail_safe:
                raise
            return False
    if require_audio:
        if cancel_callback is not None and cancel_callback():
            raise CancelledError("media validation cancelled")
        try:
            if not _ffprobe_is_valid_media(path, stream_type="a", cancel_callback=cancel_callback):
                return False
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            if not fail_safe:
                raise
            return False
        audio_ok, audio_measured = _ffmpeg_decode_timing(
            path,
            stream_type="a",
            timeout=timeout,
            cancel_callback=cancel_callback,
            low_process_priority=low_process_priority,
            rlimit_as_mb=rlimit_as_mb,
        )
        if not audio_ok:
            return False
        if cancel_callback is not None and cancel_callback():
            raise CancelledError("media validation cancelled")
        try:
            audio_start, audio_dur = _ffprobe_stream_timing(
                path, "a", cancel_callback=cancel_callback
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            if not fail_safe:
                raise
            return False
    if require_video and require_audio:
        # FAIL-CLOSED: a required stream whose timing cannot be read is
        # NOT valid media (audit round 32 P1) — except the one
        # LEGITIMATE negative-verdict gap: containers with no
        # stream-level duration at all (FLV and friends). For those,
        # the DECODED lengths (measured during the full decode above)
        # take the place of the metadata durations — the same end
        # comparison, on the same tolerance (audit round 37 P1: a
        # container duration cannot distinguish a healthy FLV from one
        # with a 0.1 s audio track against 12 s of video — the decoded
        # lengths can). A mismatch beyond the bounded tolerance is a
        # truncated secondary track (audit rounds 30 P5 / 33 P1).
        if video_dur is None or audio_dur is None:
            # Genuine N/A on at least one side: fall back to the
            # measured lengths. Unmeasurable (None) or empty (<= 0)
            # measured length on EITHER side stays fail-closed — a
            # required track we cannot prove any length for is not
            # valid media.
            if (
                video_measured is None
                or audio_measured is None
                or video_measured <= 0.0
                or audio_measured <= 0.0
            ):
                return False
            video_end = video_measured
            audio_end = audio_measured
        else:
            # Compare track ENDS, not raw durations: MPEG-PS (and other
            # multiplexed formats) legitimately start the audio track
            # later than the video (reproduced live: a healthy 2.5 s
            # MPEG-PS with video start 0.533 / audio start 1.112
            # carries a 532 ms DURATION difference yet ends within 47
            # ms of the video — a duration comparison falsely rejects
            # it; the end comparison is what the muxer actually keeps
            # aligned, audit round 36 P1). Any component missing falls
            # back to the duration comparison, which is what the
            # historical gate did.
            video_end = video_start + video_dur if video_start is not None else video_dur
            audio_end = audio_start + audio_dur if audio_start is not None else audio_dur
        if abs(video_end - audio_end) > _allowed_stream_drift(video_end, audio_end):
            return False
    return True


def resume_part_ok(
    part_path: Path,
    *,
    expected_duration: float,
    min_part_bytes: int,
    require_video: bool,
    require_audio: bool,
    timeout_seconds: float,
    cancel_callback: Callable[[], bool] | None = None,
    low_process_priority: bool = False,
    rlimit_as_mb: int = 0,
) -> bool:
    """Unified resume gate for a pipeline part file.

    One chain, five call sites (segment / batch / cut_encode parts and
    raw concat, audio extract): size floor → duration sanity →
    whole-stream media validity. The old hand-copied quadruplicates
    drifted the moment any kwarg changed. Semantics:

      * ``min_part_bytes`` — a crash can leave a tiny non-empty file;
        below this floor it's re-encoded without probing.
      * ``_ffprobe_duration_ok`` (slack=1.0, matching batch.py /
        audio.py) rejects a moov-bearing part whose BODY was truncated
        by a mid-flush kill — the moov records the PLANNED length while
        the content is shorter.
      * ``_media_is_valid`` runs the codec probe + whole-stream decode
        (``-xerror``, audit round 31 P1-4): video when required, AND the
        audio body too when required (a video-valid part can still
        carry a truncated audio track, audit round 30 P6). Mid-body
        corruption passes every header-level probe; the decode reads
        every packet.
      * Cancellable, ``timeout_seconds`` bounded (audit round 30 P7),
        honours the caller's resource policy (low priority / rlimit,
        audit round 31 P1-3), ``fail_safe=True`` so an infrastructure
        fault re-encodes instead of reusing a suspect file.
    """
    if not part_path.exists():
        return False
    try:
        if part_path.stat().st_size < min_part_bytes:
            return False
    except OSError:
        return False
    if not _ffprobe_duration_ok(part_path, expected_duration, cancel_callback=cancel_callback):
        return False
    return _media_is_valid(
        part_path,
        require_video=require_video,
        require_audio=require_audio,
        timeout=timeout_seconds,
        cancel_callback=cancel_callback,
        low_process_priority=low_process_priority,
        rlimit_as_mb=rlimit_as_mb,
        fail_safe=True,
        # Parts are pipeline-encoded CFR: the declared fps is the true
        # cadence, so a frame count far below duration x fps is a hole,
        # not VFR. A corrupt part reused here injects the hole into the
        # final output (benchmark 2026-08 finding #6).
        check_frame_holes=True,
    )


def _ffprobe_duration_ok(
    path: Path,
    expected_seconds: float,
    *,
    slack: float = 1.0,
    cancel_callback: "Callable[[], bool] | None" = None,
) -> bool:
    """Check that a resume part's ffprobe duration is close to the expected value.

    ffmpeg killed mid-write can leave a valid moov atom (the file passes
    ``_ffprobe_is_valid_media``) but a truncated body — the duration read
    from the moov reflects the planned length, not the actual content. Comparing
    against the expected duration catches holes in the middle of the final
    video. ``slack`` is the caller's ceiling in seconds; the effective
    tolerance is ``min(slack, max(0.10, expected * 0.02))`` — a flat 1 s
    slack was larger than the short speech islands it guarded, and a
    0.8 s part holding only 0.1 s passed the gate with 87.5 % of the
    fragment lost (audit round 35 P1).

    When ffprobe cannot determine the duration (corrupt file, timeout,
    non-media data, transient spawn fault), returns ``False`` — fail-closed
    so a resume part whose integrity cannot be verified is re-encoded
    instead of silently accepted (audit round 32 P1-3: the historical
    behaviour returned ``True``, which let a truncated-but-readable resume
    part pass the integrity gate and inject a hole into the final output).

    Cancel is the one exception that PROPAGATES rather than returning
    False: a user cancel during resume must stop the whole run, not just
    cause every remaining part to be re-encoded (audit round 33 P2).
    """
    try:
        rc, stdout = _run_ffprobe(
            ffprobe_show_entries_args(
                path,
                "format=duration",
            ),
            cancel_callback=cancel_callback,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False  # duration unreadable — do not trust the part
    if rc != 0:
        return False
    duration_str = stdout.strip()
    if not duration_str:
        return False
    try:
        actual = float(duration_str)
    except ValueError:
        return False  # N/A or garbage — do not trust the part
    # Bounded tolerance (audit round 35 P1): a flat 1 s slack accepted
    # a resume part holding only a fraction of its expected length —
    # expected 0.8 s / actual 0.1 s passed, silently dropping 87.5 % of
    # the fragment (reproduced on a real MP4; short speech islands are
    # real at small min_silence / aggressive margins). The allowance is
    # now min(slack, max(100 ms floor, 2 % of the expected length)) —
    # the same bounded shape as the A/V drift gate: long parts keep the
    # caller's slack (encoder flush jitter), a part can never be
    # accepted with more than 2 % of its own content missing, and the
    # floor keeps tiny parts from being rejected by codec-level rounding.
    allowed = min(slack, max(_DRIFT_FLOOR_SECONDS, expected_seconds * _DRIFT_RELATIVE))
    return abs(actual - expected_seconds) <= allowed
