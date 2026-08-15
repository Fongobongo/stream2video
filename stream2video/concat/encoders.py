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
from stream2video.tools import ffmpeg_path, run_with_retry
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
# CRF/QP per quality preset (used when ``use_crf=True`` on any encoder).
# MF uses ``-rate_control quality``, NVENC ``-rc vbr -cq``, AMF
# ``-rc cqp`` with uniform QP; x264 keeps native ``-crf``.
_CRF_PER_QUALITY: dict[str, str] = {
    "high": "18",
    "medium": "23",
    "low": "28",
}
_MF_QUALITY_PER_QUALITY: dict[str, str] = {
    "high": "100",
    "medium": "75",
    "low": "50",
}

_encoder_check_cache: dict[str, bool] = {}
_encoder_check_lock = threading.Lock()


def reset_encoder_check_cache() -> None:
    """Drop cached smoke-test results.

    A transient spawn failure (winget shim target briefly
    blocked by AV/filter drivers) cached ``False`` for the whole process
    — the encoder then stayed "unavailable" even after the PATH fix the
    retry logic performed. Called alongside ``reset_tool_cache`` in the
    spawn-retry path so a re-resolved ffmpeg gets re-smoke-tested.
    """
    with _encoder_check_lock:
        _encoder_check_cache.clear()


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


def _bitrate_for_quality(quality: str, source_bitrate: int | None) -> str:
    """Resolve effective bitrate string for HW encoders at quality, with source fallback."""
    if quality == "source" and source_bitrate is not None and source_bitrate > 0:
        kbps = max(500, min(20000, round(source_bitrate / 1000)))
        return f"{kbps}k"
    if quality == "source":
        # Probe failed — caller logs and chooses fallback before calling us;
        # but for direct encoder_opts(source) without probe, fall back to high.
        return _VIDEO_BITRATES["high"]
    return _VIDEO_BITRATES[quality]


def encoder_opts(
    encoder: str,
    quality: str = "medium",
    x264_preset: str = "medium",
    encoder_threads: str | int = "auto",
    x264_low_memory: bool = False,
    source_bitrate: int | None = None,
    use_crf: bool = False,
) -> list[str]:
    """Return the ffmpeg encoder options for ``encoder`` at ``quality`` preset.

    quality: ``source`` / ``high`` / ``medium`` / ``low``. ``high``/
    ``medium``/``low`` affect bitrate (HW encoders) and CRF (libx264).
    ``source`` means "keep source bitrate" — for HW encoders the probed
    source bit_rate is passed via ``source_bitrate`` and emitted as
    ``-b:v``; libx264 gets the same constrained ``-b:v`` so source parity
    is encoder-independent. ``medium`` reproduces the previously
    hard-coded options exactly so existing output is unchanged. When
    ``use_crf=True`` there is no CRF equivalent of "keep source bitrate",
    so ``source`` is treated as an alias for ``high`` and a warning is
    logged to make the substitution explicit.

    ``x264_preset`` (libx264 only): one of ``VALID_X264_PRESETS``. Default
    ``medium`` preserves historical behaviour; users with unstable /
    overclocked CPUs can pass ``ultrafast``/``veryfast`` for a lighter
    load.

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

    ``source_bitrate``: bits/s probed from source (ffprobe stream
    bit_rate). Only used when quality=="source" and encoder is HW
    (h264_mf/amf/nvenc); caller decides fallback when None.
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
    effective_quality = "high" if quality == "source" else quality
    if use_crf and quality == "source":
        # CRF mode has no honest "match the source" mode — a CRF value
        # fixes quality, not bits. Substituting ``high`` silently would
        # make the user think they got source parity when they actually
        # got a quality-fixed encode, so say it out loud.
        logger.warning(
            "quality='source' with use_crf=True has no source-parity mode "
            "(CRF fixes quality, not bitrate) — encoding with CRF=high instead"
        )

    if use_crf:
        # Quality-fixed mode. x264/NVENC/AMF use CRF/QP-like values;
        # MF uses its own 0..100 quality scale.
        crf = _CRF_PER_QUALITY[effective_quality]
        low_mem = _x264_low_memory_opts() if encoder == "libx264" and x264_low_memory else []
        if encoder == "libx264":
            return ["-crf", crf, "-preset", x264_preset, *threads_opt, *low_mem]
        if encoder == "h264_mf":
            return [
                "-rate_control",
                "quality",
                "-quality",
                _MF_QUALITY_PER_QUALITY[effective_quality],
                *threads_opt,
            ]
        if encoder == "h264_nvenc":
            # CRF-like constant-quality mode (P0 audit): ``-rc vbr`` +
            # ``-cq`` alone makes the wrapper fall back to its default
            # bitrate model and the quality value is ignored (or the
            # encode fails). ``-b:v 0`` is the documented way to disable
            # the target bitrate so ``-cq`` becomes the sole control.
            return ["-preset", "p7", "-rc", "vbr", "-cq", crf, "-b:v", "0", *threads_opt]
        # h264_amf
        return [
            "-usage",
            "transcoding",
            "-quality",
            "quality",
            "-rc",
            "cqp",
            "-qp_i",
            crf,
            "-qp_p",
            crf,
            "-qp_b",
            crf,
            *threads_opt,
        ]

    if quality == "source":
        # Honest source: HW → probed -b:v, libx264 → constrained bitrate too
        # (not CRF) so quality doesn't depend on encoder. libx264 -b:v is
        # less efficient than CRF but gives encoder-independent size/quality
        # at source preset; users wanting quality-per-size should use high/medium.
        if encoder == "libx264":
            bitrate = _bitrate_for_quality("source", source_bitrate)
            low_mem = _x264_low_memory_opts() if x264_low_memory else []
            # Constant bitrate for source parity: use -b:v + -maxrate like HW.
            # Keep preset for speed trade-off, but cap bitrate to source.
            return [
                "-b:v",
                bitrate,
                "-maxrate",
                bitrate,
                "-bufsize",
                bitrate,
                "-preset",
                x264_preset,
                *threads_opt,
                *low_mem,
            ]
        # HW encoders
        bitrate = _bitrate_for_quality("source", source_bitrate)
        if encoder == "h264_mf":
            return ["-b:v", bitrate, "-quality", "100", *threads_opt]
        if encoder == "h264_amf":
            return ["-usage", "transcoding", "-quality", "speed", "-b:v", bitrate, *threads_opt]
        if encoder == "h264_nvenc":
            # Constrained VBR (NVIDIA's recommended offline model):
            # ``-b:v`` is the target, ``-maxrate`` the worst-case cap.
            # No ``-cq`` here — a hard-coded quality floor would fight
            # the user's quality preset (CRF 18 floor on a 3500k "low"
            # encode defeats the point of the ladder); use_crf=True is
            # the dedicated constant-quality path.
            return [
                "-preset",
                "p7",
                "-rc",
                "vbr",
                "-b:v",
                bitrate,
                "-maxrate",
                bitrate,
                *threads_opt,
            ]
        return [*threads_opt]

    bitrate = _VIDEO_BITRATES[quality]
    if encoder == "h264_mf":
        # Media Foundation: no preset/threads control via -preset; pass
        # -threads only when the user pinned a count (auto = omit).
        return ["-b:v", bitrate, "-quality", "100", *threads_opt]
    if encoder == "h264_amf":
        return ["-usage", "transcoding", "-quality", "speed", "-b:v", bitrate, *threads_opt]
    if encoder == "h264_nvenc":
        # NVENC rate-control model: constrained VBR via
        # ``-rc vbr`` with ``-b:v`` (target) and ``-maxrate`` (cap)
        # both set to the preset bitrate. VBR lets the encoder spend
        # bits where they're needed (motion, detail) while ``-maxrate``
        # guarantees a worst-case size. ``-preset p7`` is the slowest /
        # highest-quality NVENC preset (lookahead enabled). On a 6h
        # stream this is ~5-10x faster than libx264 -preset medium at
        # similar quality. (The old hard-coded ``-cq 18`` quality floor
        # was dropped in the P0 audit: it contradicted the low/medium
        # bitrate targets and belongs to the use_crf=True path only.)
        return [
            "-preset",
            "p7",
            "-rc",
            "vbr",
            "-b:v",
            bitrate,
            "-maxrate",
            bitrate,
            *threads_opt,
        ]
    # libx264 -- encoder-independent quality: use the same bitrate
    # targets as HW encoders so high/medium/low give the same size
    # regardless of encoder. Previously libx264 used CRF 18/23/28 which
    # is more efficient (better quality per bit) than HW CBR, so the same
    # preset gave different sizes. For parity we use constrained bitrate
    # (CBR) with preset still controlling speed/CPU. CRF option now lives
    # behind use_crf=True (see below).
    bitrate = _bitrate_for_quality(quality, source_bitrate)
    low_mem = _x264_low_memory_opts() if x264_low_memory else []
    return [
        "-b:v",
        bitrate,
        "-maxrate",
        bitrate,
        "-bufsize",
        bitrate,
        "-preset",
        x264_preset,
        *threads_opt,
        *low_mem,
    ]


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


# Public back-compat registry: maps each supported encoder to
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

    The smoke test itself runs WITHOUT the lock — it can take up to
    ``ENCODER_CHECK_TIMEOUT``, and concurrent callers (e.g. the GUI's
    encoder tester racing a pipeline start) would otherwise serialize
    on the check. Only the cache read/write is locked (double-checked
    locking): a duplicate in-flight smoke test for the same encoder is
    a wasted subprocess, not a correctness bug.
    """
    with _encoder_check_lock:
        if name in _encoder_check_cache:
            return _encoder_check_cache[name]
    try:
        r = run_with_retry(
            [
                ffmpeg_path(),
                "-y",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=64x64:d=0.1",
                # Match the real pipeline's pixel format: lavfi `color`
                # produces RGB, and min-cut ffmpeg builds without the
                # auto-inserted format conversion would fail libx264
                # (yuv420p-only encoder) here even though the actual
                # encode commands (`vcodec_opts` add `-pix_fmt yuv420p`)
                # would succeed -- a false-negative that then raised
                # EncoderUnavailableError on a working ffmpeg.
                "-pix_fmt",
                "yuv420p",
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
        with _encoder_check_lock:
            _encoder_check_cache[name] = False
        return False
    except FileNotFoundError:
        # ffmpeg binary missing/blocked at spawn time (winget shim
        # target, AV filter driver, PATH break). ``run_with_retry``
        # re-raises after its retries. Report "unavailable" instead
        # of crashing -- the caller (``--doctor``, encoder tester)
        # is a diagnostics surface that must degrade gracefully
        # (``--doctor`` without ffmpeg crashed with an
        # unhandled FileNotFoundError).
        logger.warning(f"{name} smoke test failed to spawn ffmpeg (missing or blocked)")
        with _encoder_check_lock:
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
    with _encoder_check_lock:
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
    source_bitrate: int | None = None,
    use_crf: bool = False,
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
            source_bitrate=source_bitrate,
            use_crf=use_crf,
        )

    # If the requested encoder IS libx264 and it failed its smoke test,
    # falling back to libx264 again would guarantee a mid-pipeline crash
    # (the ffmpeg build is broken). Surface a clear startup error instead.
    if preferred == "libx264":
        raise EncoderUnavailableError(
            "libx264 was requested but the ffmpeg smoke test failed "
            "(encoder not compiled in / broken ffmpeg installation). "
            "Install a working ffmpeg or select a hardware encoder."
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
            source_bitrate=source_bitrate,
            use_crf=use_crf,
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
                source_bitrate=source_bitrate,
                use_crf=use_crf,
            )
        raise EncoderUnavailableError(f"{preferred} not available; user declined libx264 fallback")
    # software_fallback == "disabled"
    raise EncoderUnavailableError(
        f"{preferred} not available; software_fallback='disabled' -- refusing libx264"
    )
