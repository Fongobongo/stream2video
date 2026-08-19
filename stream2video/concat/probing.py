"""ffprobe-based validity helpers for resume-skip logic."""

import contextlib
import logging
import math
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from stream2video.concat.errors import CancelledError
from stream2video.tools import ffmpeg_path, ffprobe_path, popen_with_retry
from stream2video.utils import (
    drain_stderr_lines,
    kill_and_reap,
    no_window_kwargs,
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
_PROBE_TIMEOUT = 10.0

# Poll cadence of the cancellable probe loop (audit round 33 P2).
_PROBE_POLL_SECONDS = 0.2


def _run_ffprobe(
    args: list[str],
    *,
    timeout: float = _PROBE_TIMEOUT,
    cancel_callback: "Callable[[], bool] | None" = None,
) -> tuple[int, str]:
    """Run ONE metadata ffprobe: cancellable, bounded, torn down on
    every exit path (audit round 33 P2).

    The old shape — a blocking ``subprocess`` run call with a flat
    timeout — noticed a user cancel only AFTER the timeout expired: a probe
    stalled on a wedged source (network share, broken container) held
    the GUI's Cancel for up to the whole ceiling even though a cancel
    check ran between probes. One small popen loop instead (same
    teardown shape as ``_ffmpeg_full_decode``, lighter — probe stdout
    goes through the shared ring-bounded drain, so even a
    stream-spamming container can neither wedge the pipe nor grow the
    sink unboundedly): poll ``cancel_callback`` every 0.2 s, bounded
    by ``timeout``.

    Returns ``(returncode, stdout)``. Infrastructure faults PROPAGATE:
    ``CancelledError`` on cancel, ``subprocess.TimeoutExpired`` on the
    deadline, spawn faults out of ``popen_with_retry`` — "validation
    unavailable" must reach the caller's fail-safe logic, never become
    a verdict here.
    """
    proc = popen_with_retry(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        **no_window_kwargs(),
    )
    stdout_lines: list[str] = []
    assert proc.stdout is not None
    wait_for_drain = drain_stderr_lines(proc.stdout, stdout_lines)
    deadline = time.monotonic() + timeout
    rc: int | None = None
    with registered_process(proc):
        try:
            while True:
                try:
                    rc = proc.wait(timeout=_PROBE_POLL_SECONDS)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if cancel_callback is not None and cancel_callback():
                    raise CancelledError("metadata probe cancelled") from None
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(args, timeout) from None
        finally:
            # Unconditional child teardown on EVERY exit path (normal
            # completion, cancel, timeout — audit round 31 P1-1 shape).
            if proc.poll() is None:
                with contextlib.suppress(OSError):
                    kill_and_reap(proc, timeout=5.0)
            # The child is exited/reaped now, so its stdout write end is
            # closed and EOF is arriving on the pipe. Join the drain to
            # EOF BEFORE closing the pipe (audit round 34 P1-2): the
            # drain reads in a separate thread, and a close racing it
            # loses whatever output is still buffered — the read then
            # raises into the drain's swallowed-error path and a HEALTHY
            # rc=0 probe turns into an empty stdout → false invalid
            # verdict (reproduced live with a slow drain).
            if not wait_for_drain(2.0):
                # The drain did not reach EOF within the bound (e.g. a
                # grandchild inherited the pipe handle): close the pipe
                # to force EOF and give the drain one short extra grace
                # period instead of leaving it on a live fd forever.
                with contextlib.suppress(OSError, ValueError):
                    proc.stdout.close()
                wait_for_drain(0.5)
            with contextlib.suppress(OSError, ValueError):
                proc.stdout.close()
    assert rc is not None
    return rc, "".join(stdout_lines)


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
        [
            ffprobe_path(),
            "-v",
            "error",
            "-select_streams",
            stream_type,
            "-show_entries",
            "stream=codec_name",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
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
        [
            ffprobe_path(),
            "-v",
            "error",
            "-select_streams",
            stream_type,
            "-show_entries",
            "stream=codec_name:format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
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


def _ffprobe_stream_duration(
    path: Path,
    stream_type: str,
    cancel_callback: "Callable[[], bool] | None" = None,
) -> float | None:
    """Duration of the FIRST stream of the requested type (v/a), or
    None for a genuine negative verdict (audit rounds 32/33 P1).

    Container duration cannot detect an audio track truncated much
    earlier than the video (audit round 30 P5): the stream-level
    comparison between video and audio durations is what catches a
    12 s video carrying a 2 s audio track.

    Fail-closed on ambiguity (audit round 32 P1): the selector is
    ``<type>:0`` so exactly ONE duration is returned — a multi-track
    container previously made ``-select_streams a`` emit one value per
    track, ``float(r.stdout.strip())`` failed to parse, and the
    resulting ``None`` let the duration mismatch check silently pass.
    ``-select_streams a:0`` also makes the probe agree with the decode
    gate, which ``-map``s ``0:v:0`` / ``0:a:0``.

    Error contract (audit round 33 P1): ``None`` is ONLY a normal
    ffprobe verdict — non-zero rc, an empty / ``N/A`` / non-numeric
    duration. Infrastructure faults (timeout, spawn failure, cancel)
    PROPAGATE, exactly like the codec probe: a transient ffprobe hiccup
    returning ``None`` here once fell into the fail-closed branch of
    ``_media_is_valid`` and made the controller delete a fully
    downloaded multi-GB file — "validation unavailable" must never be
    mistaken for "invalid media". ``_media_is_valid``'s ``fail_safe``
    covers this probe the same way it covers the codec probe.
    """
    rc, stdout = _run_ffprobe(
        [
            ffprobe_path(),
            "-v",
            "error",
            "-select_streams",
            f"{stream_type}:0",
            "-show_entries",
            "stream=duration:stream_tags=DURATION",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        cancel_callback=cancel_callback,
    )
    if rc != 0:
        return None
    # Matroska/WebM report stream duration as ``N/A`` — the real value
    # lives in the ``TAG:DURATION`` stream tag (``HH:MM:SS.fraction``,
    # verified live: a healthy 2.008 s VP9+Opus WebM and a healthy
    # H.264+AAC MKV both read ``N/A`` at stream level and
    # ``00:00:02.008000000`` in the tag — audit round 35 P0). Prefer
    # the numeric stream duration when present; fall back to the tag
    # line, then ``None`` (genuine negative verdict, fail-closed for
    # the both-streams gate).
    for line in stdout.splitlines():
        value = _parse_ffprobe_duration(line)
        if value is not None:
            return value
    return None


def _ffmpeg_full_decode(
    path: Path,
    stream_type: str = "v",
    timeout: float = 43200,
    cancel_callback: "Callable[[], bool] | None" = None,
    *,
    low_process_priority: bool = False,
    rlimit_as_mb: int = 0,
) -> bool:
    """Decode the WHOLE requested stream into the null sink.

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
      * stderr goes through the shared ring-bounded drain
        (``drain_stderr_lines``), never ``capture_output`` — the
        integrity check itself must not be the OOM vector;
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
        zombies), stderr pipe close, drain-thread join (audit round
        31 P1-1/P1-2).

    Spawn faults, timeout and cancellations re-raise: "validation
    unavailable" must never be mistaken for "invalid media".
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
    ]
    proc = popen_with_retry(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **subprocess_kwargs(low_process_priority, rlimit_as_mb),
    )
    stderr_chunks: list[str] = []
    assert proc.stderr is not None
    wait_for_drain = drain_stderr_lines(proc.stderr, stderr_chunks)
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
            # stderr pipe closed and the drain thread joined. Only
            # then can the exception propagate. The drain is joined to
            # EOF BEFORE the pipe is closed (audit round 34 P1-2 — the
            # close raced the still-reading drain thread and lost
            # buffered output).
            if proc.poll() is None:
                with contextlib.suppress(OSError):
                    kill_and_reap(proc, timeout=5.0)
                if proc.poll() is None:
                    logger.warning("media validation process did not exit after kill: %s", path)
            if not wait_for_drain(2.0):
                with contextlib.suppress(OSError, ValueError):
                    proc.stderr.close()
                wait_for_drain(0.5)
            with contextlib.suppress(OSError, ValueError):
                proc.stderr.close()
    if proc.returncode != 0:
        return False
    stderr = "".join(stderr_chunks).lower()
    return not any(marker in stderr for marker in _TRUNCATION_MARKERS)


# Tolerance shared by the two duration gates below. Fixed 2 s was a
# huge hole for short media: a 1.8 s video carrying a 0.1 s audio
# track (nearly the whole audio body lost) passed the gate. The
# allowance is now bounded BOTH ways:
#   * never above 2 s (a six-hour encode's legitimate mux/flush drift
#     stays accepted — audit round 30 P5's counterexample),
#   * never below 100 ms (AAC priming + frame rounding on very short
#     clips — a healthy 1 fps/short encode routinely reports a few
#     frames of stream-duration offset),
#   * and 2 % of the expected/longer duration in between (proportional
#     to the amount of content that could actually be missing).
# The A/V drift gate (audit rounds 30 P5 / 33 P1) and the resume-part
# duration gate (audit round 35 P1 — a fixed 1 s slack accepted a
# 0.8 s part holding only 0.1 s, losing 87.5 % of the fragment) both
# derive their allowed offset from these constants.
_DRIFT_FLOOR_SECONDS = 0.10
_DRIFT_CEILING_SECONDS = 2.0
_DRIFT_RELATIVE = 0.02


def _allowed_stream_drift(video_dur: float, audio_dur: float) -> float:
    """Bounded absolute+relative tolerance for the v/a duration match
    (audit round 33 P1) — see the constants above for the rationale."""
    return min(
        _DRIFT_CEILING_SECONDS,
        max(_DRIFT_FLOOR_SECONDS, max(video_dur, audio_dur) * _DRIFT_RELATIVE),
    )


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
) -> bool:
    """Unified integrity gate for the EXPECTED stream set of ``path``.

    One shared validator for the three gates that previously drifted
    (audit round 31 P1-4): the fresh-download publish, the staged
    final output and — through ``_media_is_valid`` re-exported on the
    concat facade — resume part reuse. Per required stream: the stream
    must exist (codec probe) and fully decode into the null sink; when
    BOTH streams are required, their stream-level durations must agree
    within a bounded tolerance (a 12 s video carrying a 2 s audio
    track is a truncated audio body, not media — audit round 30 P5;
    a 1.8 s video carrying a 0.1 s audio track must ALSO fail — audit
    round 33 P1, ``_allowed_stream_drift``).

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
    uniformly — the codec probe AND the duration probe (round 32 only
    covered the codec one, so a transient duration-probe fault still
    fell into the fail-closed branch and deleted the download) — while
    decode cancellations / phase timeouts and spawn faults out of
    ``_ffmpeg_full_decode`` always propagate regardless of it.
    """
    if not require_video and not require_audio:
        return True
    video_dur: float | None = None
    audio_dur: float | None = None
    for stream_type, required in (("v", require_video), ("a", require_audio)):
        if not required:
            continue
        if cancel_callback is not None and cancel_callback():
            raise CancelledError("media validation cancelled")
        try:
            if not _ffprobe_is_valid_media(
                path, stream_type=stream_type, cancel_callback=cancel_callback
            ):
                return False
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            if not fail_safe:
                raise
            return False
        if not _ffmpeg_full_decode(
            path,
            stream_type=stream_type,
            timeout=timeout,
            cancel_callback=cancel_callback,
            low_process_priority=low_process_priority,
            rlimit_as_mb=rlimit_as_mb,
        ):
            return False
        if cancel_callback is not None and cancel_callback():
            raise CancelledError("media validation cancelled")
        try:
            stream_dur = _ffprobe_stream_duration(
                path, stream_type, cancel_callback=cancel_callback
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            if not fail_safe:
                raise
            return False
        if stream_type == "v":
            video_dur = stream_dur
        else:
            audio_dur = stream_dur
    if require_video and require_audio:
        # FAIL-CLOSED: a required stream whose duration cannot be read
        # is NOT valid media (audit round 32 P1) — and a mismatch
        # beyond the bounded tolerance is a truncated secondary track
        # (audit rounds 30 P5 / 33 P1).
        if video_dur is None or audio_dur is None:
            return False
        if abs(video_dur - audio_dur) > _allowed_stream_drift(video_dur, audio_dur):
            return False
    return True


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
            [
                ffprobe_path(),
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
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
