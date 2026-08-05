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
ENCODER_CHECK_TIMEOUT = 10
_FINAL_CONCAT_TIMEOUT = 86400
_SEGMENT_ENCODE_TIMEOUT = 600
_STDERR_TRUNCATE = 1000
_STALL_WARNING = 120
_STALL_KILL = 300
# Minimum size (bytes) for a resumed part to be considered valid.
# Exposed via CONFIG_DEFAULTS (``min_part_bytes``).
_MIN_PART_BYTES = 1024
