"""Encoder selection, option building, and smoke-test probe."""

import logging
import subprocess
import threading
from collections.abc import Callable

from stream2video.concat.constants import (
    _VIDEO_BITRATE,
    ENCODER_CHECK_TIMEOUT,
)
from stream2video.concat.errors import (
    ConcatError,
    EncoderUnavailableError,
)
from stream2video.config import (
    VALID_ENCODERS,
    VALID_OUTPUT_FPS,
    VALID_QUALITIES,
    VALID_SOFTWARE_FALLBACKS,
    VALID_X264_PRESETS,
)
from stream2video.tools import ffmpeg_path
from stream2video.utils import no_window_kwargs

logger = logging.getLogger(__name__)


# Bitrate (HW encoders) and CRF (libx264) per ``video_quality`` preset.
# ``medium`` keeps the values previously hard-coded in ENCODER_OPTS so
# existing output size/quality is unchanged on upgrade.
_VIDEO_BITRATES: dict[str, str] = {
    "high": "10000k",
    "medium": _VIDEO_BITRATE,
    "low": "3500k",
}
_X264_CRF: dict[str, str] = {
    "high": "18",
    "medium": "23",
    "low": "28",
}

_encoder_check_cache: dict[str, bool] = {}
_encoder_check_lock = threading.Lock()


def _x264_low_memory_opts() -> list[str]:
    """Return extra x264 options that reduce peak memory usage.

    These tune the lookahead, reference frames, and B-frame pyramid so
    the encoder holds fewer frame buffers in RAM simultaneously. The
    trade-off is slightly worse compression efficiency (larger file for
    the same CRF), which is acceptable when the alternative is an OOM
    kill on a memory-constrained machine.
    """
    return [
        "-x264-params",
        "rc-lookahead=10:ref=1:bframes=0",
    ]


def encoder_opts(
    encoder: str,
    quality: str = "medium",
    x264_preset: str = "medium",
    encoder_threads: str | int = "auto",
    x264_low_memory: bool = False,
) -> list[str]:
    """Return the ffmpeg encoder options for ``encoder`` at ``quality`` preset.

    quality: ``source`` / ``high`` / ``medium`` / ``low``. ``high``/
    ``medium``/``low`` affect bitrate (HW encoders) and CRF (libx264).
    ``source`` omits the project bitrate/CRF policy and lets ffmpeg's
    encoder defaults apply. ``medium`` reproduces the previously hard-coded
    options exactly so existing output is unchanged.

    ``x264_preset`` (libx264 only): one of ``VALID_X264_PRESETS``. Default
    ``medium`` preserves historical behaviour; users with unstable /
    overclocked CPUs can pass ``ultrafast``/``veryfast`` for a lighter
    load. See P0.5 in the fix plan.

    ``encoder_threads``: ``"auto"`` (no ``-threads`` flag, ffmpeg chooses)
    or a positive int. For libx264 the flag goes AFTER the encoder in the
    constructed command (``-c:v libx264 ... -threads N``) so it applies to
    the encoder, not to the decoder (a ``-threads`` before ``-i`` would
    bound the decoder's thread pool instead -- different effect).

    ``x264_low_memory`` (libx264 only): when True, appends
    ``-x264-params rc-lookahead=10:ref=1:bframes=0`` to reduce the
    encoder's frame-buffer footprint. Useful on memory-constrained
    machines (4-8 GB RAM) where a default-medium encode of a long
    stream could push the process into swap.
    """
    if encoder not in VALID_ENCODERS:
        raise ConcatError(f"Unknown encoder {encoder!r} (known: {', '.join(VALID_ENCODERS)})")
    if quality not in VALID_QUALITIES:
        raise ConcatError(
            f"Unknown video quality {quality!r} (use {' or '.join(repr(q) for q in VALID_QUALITIES)})"
        )
    if x264_preset not in VALID_X264_PRESETS:
        raise ConcatError(
            f"Unknown x264 preset {x264_preset!r} "
            f"(use {' or '.join(repr(p) for p in VALID_X264_PRESETS)})"
        )
    threads_opt = _threads_opt(encoder_threads)

    if quality == "source":
        low_mem = _x264_low_memory_opts() if encoder == "libx264" and x264_low_memory else []
        if encoder == "libx264":
            return ["-preset", x264_preset, *threads_opt, *low_mem]
        return [*threads_opt]

    bitrate = _VIDEO_BITRATES[quality]
    if encoder == "h264_mf":
        # Media Foundation: no preset/threads control via -preset; pass
        # -threads only when the user pinned a count (auto = omit).
        return ["-b:v", bitrate, "-quality", "100", *threads_opt]
    if encoder == "h264_amf":
        return ["-usage", "transcoding", "-quality", "speed", "-b:v", bitrate, *threads_opt]
    if encoder == "h264_nvenc":
        # NVENC rate-control model (P2.12): constrained VBR via
        # ``-rc vbr`` with ``-b:v`` (target) and ``-maxrate`` (cap)
        # both set to the preset bitrate, plus ``-cq 18`` as the
        # quality floor. This is NVIDIA's recommended RC model for
        # offline encoding: VBR lets the encoder spend bits where
        # they're needed (motion, detail) while ``-maxrate``
        # guarantees a worst-case size, and ``-cq`` prevents quality
        # from dropping below 18 even when the bitrate budget would
        # allow it. ``-preset p7`` is the slowest / highest-quality
        # NVENC preset (lookahead enabled, 2-pass). On a 6h stream
        # this is ~5-10x faster than libx264 -preset medium at
        # similar quality.
        return [
            "-preset",
            "p7",
            "-rc",
            "vbr",
            "-b:v",
            bitrate,
            "-maxrate",
            bitrate,
            "-cq",
            "18",
            *threads_opt,
        ]
    # libx264 -- CRF-driven, bitrate is ignored. ``-preset`` controls the
    # speed/size trade-off AND the CPU load (slower presets do more
    # lookahead/motion search). ``-threads`` caps the encoder's parallel
    # frame evaluations so an 8-core machine doesn't saturate to 100% on
    # a long encode.
    crf = _X264_CRF[quality]
    low_mem = _x264_low_memory_opts() if x264_low_memory else []
    return ["-crf", crf, "-preset", x264_preset, *threads_opt, *low_mem]


def _threads_opt(encoder_threads: str | int) -> list[str]:
    """Return the ffmpeg ``-threads`` arg list for the encoder position.

    ``"auto"`` → no flag (ffmpeg picks, same as before).
    Positive int → ``["-threads", "N"]``. Anything else (zero, negative,
    non-int string) is dropped with a warning so the encoder doesn't get
    a malformed flag that ffmpeg would reject.
    """
    if encoder_threads == "auto":
        return []
    if isinstance(encoder_threads, bool):
        return []
    if isinstance(encoder_threads, int) and encoder_threads > 0:
        return ["-threads", str(encoder_threads)]
    logger.warning(f"encoder_threads={encoder_threads!r} is not 'auto' or a positive int; ignoring")
    return []


def _fps_filter_chain(output_fps: str) -> str:
    """Return the ffmpeg filter chain fragment that enforces ``output_fps``.

    ``source`` (default) returns an empty string -- the encoder receives
    the original PTS without any fps filter, so a 30 FPS source comes
    out at 30 FPS without frame duplication. This is the historical
    behaviour preserved by the trim+concat rewrite.

    A numeric value (``24`` / ``25`` / ``30`` / ``50`` / ``60``) returns
    a ``fps=<target>`` filter fragment. Callers must splice it into the
    filter chain AFTER ``setpts=PTS-STARTPTS`` so the new PTS cadence
    is the source's, not the synthetic ``N/FRAME_RATE`` one.

    The ``fps`` filter duplicates or drops frames to match the target
    CFR; the docs warn that this changes file size and quality.
    """
    if output_fps == "source":
        return ""
    if output_fps in VALID_OUTPUT_FPS:
        return f",fps={output_fps}"
    logger.warning(
        f"output_fps={output_fps!r} is not 'source' or one of {VALID_OUTPUT_FPS}; ignoring"
    )
    return ""


# Public back-compat registry (P2.11): maps each supported encoder to
# its default (medium) options. Kept as a documented public API because:
#   1. Tests use it as a sanity check that VALID_ENCODERS and the
#      encoder registry stay in sync.
#   2. Downstream tools that import stream2video as a library may rely
#      on it to enumerate available encoders and their default options.
# Runtime callers should prefer ``encoder_opts(encoder, quality)`` so
# the ``video_quality`` / ``x264_preset`` / ``encoder_threads`` presets
# are applied; ENCODER_OPTS always returns the ``medium`` defaults.
ENCODER_OPTS: dict[str, list[str]] = {enc: encoder_opts(enc) for enc in VALID_ENCODERS}


def check_encoder(name: str) -> bool:
    """Smoke test: verify the encoder works by encoding 1 frame. Cached per process.

    Previously ``libx264`` returned ``True`` without actually invoking
    ffmpeg -- that hid cases where the build's libx264 was broken or
    missing (e.g. a stripped-down distro build, or ffmpeg linked with
    GPL-removed codecs). We now run the same 1-frame lavfi smoke test
    for every encoder; the cache keeps it free for the test-encoder
    button's repeated calls.
    """
    with _encoder_check_lock:
        if name in _encoder_check_cache:
            return _encoder_check_cache[name]
        try:
            from stream2video import concat as _c  # lazy to avoid cycle

            r = _c.run_with_retry(
                [
                    ffmpeg_path(),
                    "-y",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=64x64:d=0.1",
                    "-c:v",
                    name,
                    "-frames:v",
                    "1",
                    "-f",
                    "null",
                    "-",
                ],
                capture_output=True,
                text=True,
                timeout=ENCODER_CHECK_TIMEOUT,
                **no_window_kwargs(),
            )
        except subprocess.TimeoutExpired:
            logger.warning(f"{name} smoke test timed out after {ENCODER_CHECK_TIMEOUT}s")
            _encoder_check_cache[name] = False
            return False
        ok = r.returncode == 0
        if not ok and name == "libx264":
            # libx264 is the safety net -- if it's gone, the user needs to
            # reinstall ffmpeg with x264 support, not be told to "pick
            # a different encoder".
            logger.error(
                f"libx264 smoke test FAILED (rc={r.returncode}); "
                f"stderr: {r.stderr[:300]!r}. Your ffmpeg build is missing the "
                f"x264 library -- reinstall ffmpeg (e.g. `winget install "
                f"Gyan.FFmpeg` on Windows) or install a build with libx264 support."
            )
        _encoder_check_cache[name] = ok
        return ok


def get_video_encoder(
    preferred: str,
    video_quality: str = "medium",
    software_fallback: str = "ask",
    on_unavailable: Callable[[], bool] | None = None,
    x264_preset: str = "medium",
    encoder_threads: str | int = "auto",
    x264_low_memory: bool = False,
) -> tuple[str, list[str]]:
    """Resolve the encoder to use for this run.

    If ``preferred`` is available, return it. If not (smoke test fails),
    consult ``software_fallback``:

      * ``enabled`` -- return libx264 (legacy silent-fallback behaviour).
      * ``disabled`` -- raise EncoderUnavailableError immediately.
      * ``ask`` (default) -- call ``on_unavailable`` if provided; the
        callback returns True to allow libx264 fallback, False to
        raise. With ``on_unavailable=None``, ``ask`` behaves as
        ``disabled`` so a pipeline run can't silently switch to a
        CPU-heavy encoder without consent.

    ``x264_preset`` and ``encoder_threads`` are forwarded to
    :func:`encoder_opts` so the resolved options match the user's
    settings (also on a fallback retry -- the fallback call site keeps
    the same values). ``x264_low_memory`` reduces x264 frame buffer
    usage (see ``encoder_opts`` for details).
    """
    if preferred not in VALID_ENCODERS:
        raise ConcatError(f"Unknown encoder {preferred!r} (known: {', '.join(VALID_ENCODERS)})")
    if video_quality not in VALID_QUALITIES:
        raise ConcatError(
            f"Unknown video quality {video_quality!r} "
            f"(use {' or '.join(repr(q) for q in VALID_QUALITIES)})"
        )
    if software_fallback not in VALID_SOFTWARE_FALLBACKS:
        raise ConcatError(
            f"Unknown software_fallback {software_fallback!r} "
            f"(use {' or '.join(repr(s) for s in VALID_SOFTWARE_FALLBACKS)})"
        )

    if check_encoder(preferred):
        return preferred, encoder_opts(
            preferred,
            video_quality,
            x264_preset=x264_preset,
            encoder_threads=encoder_threads,
            x264_low_memory=x264_low_memory,
        )

    # HW encoder unavailable -- apply fallback policy.
    if software_fallback == "enabled":
        logger.warning(f"{preferred} not available, falling back to libx264")
        return "libx264", encoder_opts(
            "libx264",
            video_quality,
            x264_preset=x264_preset,
            encoder_threads=encoder_threads,
            x264_low_memory=x264_low_memory,
        )
    if software_fallback == "ask":
        if on_unavailable is None:
            raise EncoderUnavailableError(
                f"{preferred} not available; install the encoder or "
                f"select a different one (software_fallback='ask' but "
                f"no consent handler was provided)"
            )
        if on_unavailable():
            logger.warning(f"{preferred} not available; user consented to libx264 fallback")
            return "libx264", encoder_opts(
                "libx264",
                video_quality,
                x264_preset=x264_preset,
                encoder_threads=encoder_threads,
                x264_low_memory=x264_low_memory,
            )
        raise EncoderUnavailableError(f"{preferred} not available; user declined libx264 fallback")
    # software_fallback == "disabled"
    raise EncoderUnavailableError(
        f"{preferred} not available; software_fallback='disabled' -- refusing libx264"
    )
