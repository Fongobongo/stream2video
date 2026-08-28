"""Shared constants for the concat pipeline."""

_VIDEO_BITRATE = "7000k"
# Audio bitrate presets. ``medium`` is the default 192k preset; ``high``/``low``
# give the user a real choice so a 192k/256k/320k source is no longer silently
# downgraded to 128k. ``source`` skips the bitrate/resample/downmix policy.
# P1 audit v0.3 §6.1: callers pass ``audio_quality`` explicitly via
# ``_audio_bitrate_opts(q)`` / ``_audio_opts(q)``, eliminating the module-level
# ``_audio_quality`` global that made consecutive runs share mutable state.
_AUDIO_BITRATE = "128k"
_AUDIO_BITRATES: dict[str, str] = {
    "high": "256k",
    "medium": "192k",
    "low": "128k",
}
# Sample rate / channel policy. ``-ar 48000 -ac 2`` historically
# normalised everything to stereo 48 kHz AAC -- the source was never
# preserved, but output was at least consistent across segments. Keep
# that explicit conversion so the audio path is documented, but route
# it through ``_audio_opts(q)`` so an explicit "preserve source" preset
# can be added as a follow-up without rewriting every call site.
_AUDIO_SAMPLE_RATE = "48000"
_AUDIO_CHANNELS = "2"
_BATCH_CHUNK_SIZE = 40
# Minimum chunk size used for small files that would produce too many
# tiny chunks; also protects against zero-length chunk lists.
_BATCH_CHUNK_MIN = 5
# Maximum length (seconds) of a single stream passed through one
# concat-filter input (benchmark 2026-08, findings #7 and #8): ffmpeg 9.x
# loses or livelocks the VIDEO stream of a concat-filter graph when ONE
# input carries a very long stream alongside its audio chain (a 12536 s
# trim truncated the output to 2.9% of its frames; the same graph with the
# input capped at 347 s was frame-exact). Applies to BOTH concat-filter
# paths: batch (a long ``trim`` in the chunk graph, finding #7) and
# segment+gapless (a long encoded part fed as one concat-filter input,
# finding #8). Long keep blocks are split into contiguous sub-segments of
# at most this length before the graph is built — contiguous pieces concat
# back to identical content. 300 s sits below the proven-safe 347 s with
# margin, and far above typical keep segments (seconds to a minute), so
# normal content is untouched.
_CONCAT_TRIM_MAX = 300.0
ENCODER_CHECK_TIMEOUT = 10
_FINAL_CONCAT_TIMEOUT = 86400
_SEGMENT_ENCODE_TIMEOUT = 600
# Per-second-of-content factor used to scale the segment encode/verify
# timeout (benchmark 2026-08, finding #1): a FLAT 600 s cap killed a
# legitimate encode of a 3.5-hour keep block (and would time out its
# whole-stream resume-validation decode on a re-run). The effective
# timeout is ``max(base, duration * factor)``. The factor budgets a
# slower-than-realtime encoder (a libx264 ``veryslow``-class preset at
# ~1/6 realtime) with a wide margin; a genuinely HUNG encode is caught
# far sooner by ``stall_kill`` (no-progress watchdog), so this backstop
# can stay generous without masking a stall.
_SEGMENT_TIMEOUT_PER_SECOND = 6.0


def scaled_part_timeout(base_timeout: float, duration: float) -> float:
    """Effective encode/validate timeout for a part of ``duration``
    seconds: ``max(base_timeout, duration * _SEGMENT_TIMEOUT_PER_SECOND)``.

    Short parts keep the caller's flat base (a 2 s segment must not wait
    hours for a hung encode); long parts scale with their own length so
    a multi-hour keep block is no longer killed mid-encode by a cap that
    made sense only when every part was a few seconds (benchmark 2026-08
    finding #1). Pure — trivially unit-testable.
    """
    return max(float(base_timeout), max(0.0, float(duration)) * _SEGMENT_TIMEOUT_PER_SECOND)


def _split_long_segments(
    chunk: list[tuple[float, float]], max_trim: float
) -> list[tuple[float, float]]:
    """Split keep segments longer than ``max_trim`` into contiguous
    sub-segments of at most ``max_trim`` seconds (benchmark 2026-08,
    findings #7 and #8).

    The ffmpeg 9.x concat filter loses/livelocks the video stream when ONE
    input passes a very long stream alongside its audio chain. Shared by
    both concat-filter paths: batch (a long ``trim`` in the chunk graph)
    and segment+gapless (a long encoded part fed as one concat-filter
    input). The pieces produced here are adjacent in the source, so the
    concat filter glues them back into identical content — the split is
    lossless. Segments at or under ``max_trim`` pass through unchanged, so
    typical content (keep segments of seconds to a minute) is untouched.
    Pure and side-effect free so the split can be unit tested without
    running ffmpeg.
    """
    out: list[tuple[float, float]] = []
    for s, e in chunk:
        if e - s <= max_trim:
            out.append((s, e))
            continue
        cur = s
        while e - cur > max_trim:
            out.append((cur, cur + max_trim))
            cur += max_trim
        out.append((cur, e))
    return out


_STDERR_TRUNCATE = 1000
_STALL_WARNING = 120
_STALL_KILL = 300
# Minimum size (bytes) for a resumed part to be considered valid.
# Exposed via CONFIG_DEFAULTS (``min_part_bytes``).
_MIN_PART_BYTES = 1024

# User-facing hint appended to every OOM-class error. Single source of
# truth: runner.py / silence detect previously carried several hand-copied
# variants of this line that drifted in wording (one omitted the
# --batch-chunk-size branch).
_OOM_HINT = "try --preset low_memory / lowering --memory-limit-mb / reducing --batch-chunk-size"
