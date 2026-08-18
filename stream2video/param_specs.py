"""Single declarative parameter table shared by the CLI, the GUI and the
config layer.

This module is the ONE source of truth for "which tunables exist, what's
their type, what CLI flag they map to, what values are allowed and what
their defaults are" (audit round 31 P3: before this consolidation the
defaults / ranges / enum whitelists lived in a SECOND table in
``config.py`` and had to be kept in sync by hand).

Consumers:

  * ``config.py`` — derives its compatibility views ``CONFIG_DEFAULTS`` /
    ``CONFIG_RANGES`` / ``ENUM_VALIDATORS`` from ``PARAM_SPECS`` so no
    value exists twice;
  * ``cli_resolver`` — resolves a parameter name to its final value
    (CLI flag > YAML config > default) using ``kind`` / ``valid`` /
    ``min`` / ``max``;
  * ``gui_helpers.build_cli_command`` — emits the ``flag`` / aliases
    for every parameter whose value diverges from the effective
    defaults, so a copied command can't drift from the CLI's flag
    names.

Adding a new tunable is a one-line entry here plus the matching
``@app.command()`` argument in ``cli.py``; the copied-command builder
and the config-layer views pick it up automatically.

The module must stay dependency-light (stdlib only — no typer/rich) so
the GUI can import it without pulling the CLI stack into a Tk process.
For the same reason it NEVER imports ``stream2video.config`` — the
derivation direction is ``param_specs → config`` (audit round 31 P3).
"""

from __future__ import annotations

from typing import Any, Literal

# ---------------------------------------------------------------------------
# Value whitelists. Kept in THIS module (not config.py) so the enum
# choices live next to the PARAM_SPECS entry that references them.
# config.py re-exports the same names for its ENUM_VALIDATORS view and
# for the modules (encoders, gui, controller) that import them directly.
# ---------------------------------------------------------------------------

VALID_METHODS: list[str] = ["segment", "batch", "cut_then_encode"]

VALID_ENCODERS: list[str] = ["h264_nvenc", "h264_amf", "h264_mf", "libx264"]

VALID_QUALITIES: list[str] = ["source", "high", "medium", "low"]

VALID_DOWNLOAD_QUALITIES: list[str] = ["best", "1080p", "720p", "480p", "360p"]

# Encoder fallback policy when the user-selected HW encoder (AMF/NVENC/MF)
# is unavailable or fails mid-run. ``ask`` (default) refuses silent
# fallback to libx264 — heavy CPU workload can overload an overclocked
# machine, so the user must explicitly confirm. ``disabled`` raises
# immediately. ``enabled`` preserves the legacy silent-fallback behaviour
# for users running on a known-stable CPU.
VALID_SOFTWARE_FALLBACKS: list[str] = ["ask", "disabled", "enabled"]

# x264 preset ladder. Kept narrow: ffmpeg accepts ultrafast..placebo but
# we only expose the slice that matches a CPU quality/speed/size trade-off
# the user can reason about. The CLI/GUI passes one of these verbatim to
# ffmpeg ``-preset``.
VALID_X264_PRESETS: list[str] = [
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
]

# Output FPS policy. ``source`` preserves the input's frame
# cadence; the integer values force a CFR conversion.
VALID_OUTPUT_FPS: list[str] = ["source", "24", "25", "30", "50", "60"]

# Output container/codec policy. ``video`` keeps the historical
# H.264 + AAC MP4 behaviour; the other values produce standalone
# audio files (video stream dropped). See OUTPUT_FORMAT_SPECS in
# config.py for the codec/container mapping.
VALID_OUTPUT_FORMATS: list[str] = ["video", "mp3", "opus", "aac", "wav", "flac"]

# GUI-only theme choices. Deliberately OUTSIDE ``PARAM_SPECS``: a theme
# is session GUI state, not a pipeline parameter (audit round 31 P3 —
# do not merge session-only GUI state with pipeline config). The config
# layer exposes it through its ENUM_VALIDATORS view.
VALID_THEMES: list[str] = ["dark", "light", "system"]

# ---------------------------------------------------------------------------
# Resource presets. Bundle existing tunables (x264_low_memory,
# memory_limit_mb, memory_reserve_mb, batch_chunk_size,
# low_process_priority, encoder_threads) into three named profiles so a
# user can pick a goal at a glance instead of toggling six flags.
# ``balanced`` is the empty identity preset — applying it changes
# nothing, so a user's config (YAML values, GUI checkbox choices)
# survives untouched.
# Each preset overrides only the tunables listed below — pipeline-only
# settings (method, encoder, *_quality, threshold, min_silence, margin,
# timeouts, gapless_concat) always come from the user's existing config
# and are *never* touched by apply_preset.
#
# ``low_memory`` trades speed for stability on 4-8 GB machines:
#   * x264_low_memory=True → rc-lookahead=10 / ref=1 / bframes=0 (smaller
#     frame-buffer footprint, slightly larger files).
#   * batch_chunk_size=20 (was 40) → smaller filter graphs → fewer
#     decoded frames in RAM per batch invocation.
#   * low_process_priority=True → ffmpeg doesn't compete with the OS / GUI.
#
# ``low_cpu`` minimizes CPU usage for background/unattended encoding:
#   * x264_preset="ultrafast" → fastest encode, larger files.
#   * encoder_threads=2 → limits parallel frame processing.
#   * x264_low_memory=True → further reduces frame-buffer footprint.
#   * low_process_priority=True → ffmpeg runs at below-normal priority.
#
# ``maximum_performance`` trades RAM for throughput:
#   * x264_low_memory=False → full x264 defaults (larger frame buffer).
#   * memory_limit_mb=0 → disables the in-process pre-flight memory budget
#     (the OS reserve is still honoured). Only safe on machines that
#     won't swap; otherwise the Low memory preset is more appropriate.
#   * batch_chunk_size=80 (was 40) → larger batch chunks → fewer filter
#     invocations → less per-chunk startup overhead on long sources.
# ---------------------------------------------------------------------------
PRESETS: dict[str, dict[str, Any]] = {
    "low_memory": {
        "x264_low_memory": True,
        "batch_chunk_size": 20,
        "low_process_priority": True,
    },
    "low_cpu": {
        "x264_preset": "ultrafast",
        "encoder_threads": 2,
        "x264_low_memory": True,
        "low_process_priority": True,
    },
    # balanced: identity preset — applies no overrides, so a user's YAML
    # (``x264_low_memory: true`` etc.) and the GUI's checkbox choices are
    # never silently overwritten by the default preset.
    "balanced": {},
    "maximum_performance": {
        "x264_low_memory": False,
        "memory_limit_mb": 0,
        "batch_chunk_size": 80,
    },
}

PRESET_NAMES = tuple(PRESETS.keys())
DEFAULT_PRESET = "balanced"

# Parameter kind determines the resolution + validation strategy.
#   * ``enum``        — value must be in a whitelist (method, encoder, ...)
#   * ``bool``        — CLI flag is tri-state (None = fall through to config)
#   * ``int``         — plain int (timeouts, sizes)
#   * ``float``       — plain float (threshold, min_silence, margin)
#   * ``auto_or_int`` — accepts the literal string "auto" OR a positive int
#                       (encoder_threads, memory_limit_mb)
#   * ``proxy``       — special: CLI --proxy implies proxy_active=True
ParamKind = Literal["enum", "bool", "int", "float", "auto_or_int", "proxy"]

# Bool parameters carry BOTH spellings in the spec table because a copied
# command must be able to pin either direction: the emitted form is chosen
# by comparing the GUI value against the EFFECTIVE defaults (CONFIG_DEFAULTS
# + user_defaults.json). A value that diverges from the effective default is
# always spelled out, the positive flag when True, the ``--no-*`` flag when
# False — so a pasted command can never silently fall back to a user-default
# override the GUI didn't show (audit P1: a ``proxy_active: true`` in
# user_defaults.json re-enabled a proxy the GUI had switched off because the
# copied command carried no negative flag).

# The declarative spec table. Order matters only for readability; the
# resolver iterates over whatever order the caller passes names in.
#
# Keys:
#   kind       — ParamKind (see above)
#   default    — the config-layer default (config.CONFIG_DEFAULTS is a
#                DERIVED view of this column; a key may carry no
#                default when it is not persisted config — none do
#                currently)
#   valid      — for ``enum``: list of allowed values
#   min        — for ``auto_or_int`` / ``int`` / ``float``: minimum
#                accepted value (``0`` for "non-negative", ``1`` for
#                "positive")
#   max        — for ``int`` / ``float`` / ``auto_or_int``: maximum
#                accepted value
#   flag       — the CLI flag emitted for this parameter in a copied
#                command (the positive form; for bools see ``flag_off``)
#   flag_off   — for ``bool``: the negative (``--no-*``) form; emitted
#                when the value diverges from the effective default and
#                is False
PARAM_SPECS: dict[str, dict[str, Any]] = {
    # String-enum parameters with whitelists.
    "method": {"kind": "enum", "default": "segment", "valid": VALID_METHODS, "flag": "--method"},
    "encoder": {"kind": "enum", "default": "h264_mf", "valid": VALID_ENCODERS, "flag": "--encoder"},
    "video_quality": {
        "kind": "enum",
        "default": "source",
        "valid": VALID_QUALITIES,
        "flag": "--video-quality",
    },
    "audio_quality": {
        "kind": "enum",
        "default": "source",
        "valid": VALID_QUALITIES,
        "flag": "--audio-quality",
    },
    "download_quality": {
        "kind": "enum",
        "default": "best",
        "valid": VALID_DOWNLOAD_QUALITIES,
        "flag": "--download-quality",
    },
    "software_fallback": {
        "kind": "enum",
        "default": "ask",
        "valid": VALID_SOFTWARE_FALLBACKS,
        "flag": "--software-fallback",
    },
    "x264_preset": {
        "kind": "enum",
        "default": "medium",
        "valid": VALID_X264_PRESETS,
        "flag": "--x264-preset",
    },
    "output_fps": {
        "kind": "enum",
        "default": "source",
        "valid": VALID_OUTPUT_FPS,
        "flag": "--output-fps",
    },
    "output_format": {
        "kind": "enum",
        "default": "video",
        "valid": VALID_OUTPUT_FORMATS,
        "flag": "--output-format",
    },
    # Bool toggle parameters. Tri-state on CLI (None = fall back to
    # config), stored as plain bool in YAML. ``flag`` / ``flag_off`` are
    # the positive / negative spellings; the copied-command builder
    # chooses by comparing the value to the effective defaults.
    "force": {"kind": "bool", "default": False, "flag": "-f", "flag_off": "--no-force"},
    "delete_after": {
        "kind": "bool",
        "default": False,
        "flag": "--delete-after",
        "flag_off": "--no-delete-after",
    },
    "per_video_dir": {
        "kind": "bool",
        "default": True,
        "flag": "--per-video-dir",
        "flag_off": "--no-per-video-dir",
    },
    "x264_low_memory": {
        "kind": "bool",
        "default": False,
        "flag": "--x264-low-memory",
        "flag_off": "--no-x264-low-memory",
    },
    "use_crf": {"kind": "bool", "default": False, "flag": "--use-crf", "flag_off": "--no-use-crf"},
    "gapless_concat": {
        "kind": "bool",
        "default": True,
        "flag": "--gapless-concat",
        "flag_off": "--no-gapless-concat",
    },
    "low_process_priority": {
        "kind": "bool",
        "default": False,
        "flag": "--low-process-priority",
        "flag_off": "--no-low-process-priority",
    },
    "completion_sound": {
        "kind": "bool",
        "default": True,
        "flag": "--completion-sound",
        "flag_off": "--no-completion-sound",
    },
    # Proxy gate. ``proxy_active: true`` in user_defaults.json must not
    # re-enable the proxy under a copied command whose GUI had the proxy
    # switched off — the builder emits ``--no-proxy-active`` whenever the
    # GUI value (False) diverges from the effective default. The flag only
    # gates; the address still travels via ``--proxy``.
    "proxy_active": {
        "kind": "bool",
        "default": False,
        "flag": "--proxy-active",
        "flag_off": "--no-proxy-active",
    },
    # Float-typed silence-tuning keys (GUI sliders). The floors/ceilings
    # are the SAME limits cli_config applies to YAML and
    # ``validate_pipeline_config`` enforces on PipelineConfig.
    "threshold": {
        "kind": "float",
        "default": -30.0,
        "min": -60,
        "max": -5,
        "flag": "--threshold",
    },
    "min_silence": {
        "kind": "float",
        "default": 2.0,
        "min": 0.1,
        "max": 60,
        "flag": "--min-silence",
    },
    "margin": {
        "kind": "float",
        "default": 0.5,
        "min": -3,
        "max": 5,
        "flag": "--margin",
    },
    # Pipeline phase timeouts. Lower bounds reject typos / accidental
    # zero (a 0s stall watchdog is a kill-on-startup on slow media);
    # upper bound 7 days accommodates pathological long-running encodes
    # without disabling the watchdog.
    "memory_reserve_mb": {
        "kind": "int",
        "default": 2048,
        "min": 0,
        "max": 1048576,
        "flag": "--memory-reserve-mb",
    },
    "download_timeout": {
        "kind": "int",
        "default": 28800,  # 8h
        "min": 1,
        "max": 604800,
        "flag": "--download-timeout",
    },
    "connect_timeout": {
        "kind": "int",
        "default": 300,  # 5 min pre-first-byte
        "min": 1,
        "max": 3600,
        "flag": "--connect-timeout",
    },
    "no_progress_timeout": {
        "kind": "int",
        "default": 1800,  # 30 min mid-download stall
        "min": 1,
        "max": 86400,
        "flag": "--no-progress-timeout",
    },
    "silence_timeout": {
        "kind": "int",
        "default": 36000,  # 10h silence detection ceiling
        "min": 1,
        "max": 604800,
        "flag": "--silence-timeout",
    },
    "segment_encode_timeout": {
        "kind": "int",
        "default": 600,  # 10 min per segment encode
        "min": 1,
        "max": 604800,
        "flag": "--segment-timeout",
    },
    "final_concat_timeout": {
        "kind": "int",
        "default": 86400,  # 24h absolute ceiling on final concat
        "min": 1,
        "max": 604800,
        "flag": "--final-concat-timeout",
    },
    "stall_kill_timeout": {
        "kind": "int",
        "default": 300,  # 5 min no-progress -> kill ffmpeg
        "min": 10,
        "max": 3600,
        "flag": "--stall-timeout",
    },
    "stall_warning_timeout": {
        "kind": "int",
        "default": 120,  # 2 min no-progress -> warn
        "min": 5,
        "max": 1800,
        "flag": "--stall-warning-timeout",
    },
    "waveform_timeout": {
        "kind": "int",
        "default": 300,  # 5 min waveform preview decode
        "min": 10,
        "max": 3600,
        "flag": "--waveform-timeout",
    },
    "batch_chunk_size": {
        "kind": "int",
        "default": 40,
        "min": 1,
        "max": 500,
        "flag": "--batch-chunk-size",
    },
    "min_part_bytes": {
        "kind": "int",
        "default": 1024,
        "min": 1,
        "max": 10485760,
        "flag": "--min-part-bytes",
    },
    "rlimit_as_mb": {
        "kind": "int",
        "default": 0,
        "min": 0,
        "max": 1048576,
        "flag": "--rlimit-as-mb",
    },
    # Auto-or-int: the CLI flag arrives as a string; when COMMANDLINE
    # the resolver tries ``int(value)``, falling back to ``"auto"``
    # (case-insensitive). Config values are already coerced. The
    # ``min``/``max`` bounds are enforced on EVERY surface (the upper
    # bound used to be checked only for ints from the flag path).
    "encoder_threads": {
        "kind": "auto_or_int",
        "default": "auto",
        "min": 1,
        "max": 1024,
        "flag": "--encoder-threads",
    },
    "memory_limit_mb": {
        "kind": "auto_or_int",
        "default": "auto",
        "min": 0,
        "max": 1048576,
        "flag": "--memory-limit-mb",
    },
    # Proxy: CLI --proxy implies proxy_active=True. A YAML proxy without
    # proxy_active is inert (matches the GUI's checkbox contract).
    "proxy": {"kind": "proxy", "default": "", "flag": "--proxy"},
    # Preset: handled separately via config.apply_preset() after spec
    # resolution, but listed here so the resolver can validate its
    # enum-ness.
    "preset": {
        "kind": "enum",
        "default": DEFAULT_PRESET,
        "valid": list(PRESET_NAMES),
        "flag": "--preset",
    },
}

# ---------------------------------------------------------------------------
# Compatibility views derived from PARAM_SPECS (audit round 31 P3).
# config.py builds its public CONFIG_DEFAULTS / CONFIG_RANGES /
# ENUM_VALIDATORS from these so the default / range / choice column
# cannot drift from the spec table. Tests assert the views round-trip —
# a spec entry that loses a column fails the suite, not a runtime
# validator.
# ---------------------------------------------------------------------------

SPEC_DEFAULTS: dict[str, Any] = {key: spec["default"] for key, spec in PARAM_SPECS.items()}

SPEC_RANGES: dict[str, tuple[float, float]] = {
    key: (spec["min"], spec["max"])
    for key, spec in PARAM_SPECS.items()
    if "min" in spec and "max" in spec
}

SPEC_ENUM_CHOICES: dict[str, tuple[str, ...]] = {
    key: tuple(spec["valid"])
    for key, spec in PARAM_SPECS.items()
    if spec.get("kind") == "enum" and "valid" in spec
}

# Emission order for value-carrying parameters in a copied CLI command.
# Mirrors the CLI's flag order so the pasted command reads top-to-bottom
# the same way ``stream2video --help`` lists the flags. Consumed by
# ``gui_helpers.build_cli_command``; the required-always flags (method /
# encoder / video_quality / download_quality / proxy) stay in that
# function's preamble.
CLI_VALUE_FLAG_ORDER: tuple[str, ...] = (
    "audio_quality",
    "software_fallback",
    "x264_preset",
    "encoder_threads",
    "output_fps",
    "output_format",
    "threshold",
    "min_silence",
    "margin",
    "memory_limit_mb",
    "memory_reserve_mb",
    "rlimit_as_mb",
    "download_timeout",
    "connect_timeout",
    "no_progress_timeout",
    "segment_encode_timeout",
    "final_concat_timeout",
    "silence_timeout",
    "stall_kill_timeout",
    "stall_warning_timeout",
    "waveform_timeout",
    "batch_chunk_size",
    "min_part_bytes",
    "preset",
)

# Emission order for bool parameters in a copied CLI command.
CLI_BOOL_FLAG_ORDER: tuple[str, ...] = (
    "x264_low_memory",
    "use_crf",
    "gapless_concat",
    "low_process_priority",
    "per_video_dir",
    "completion_sound",
    "force",
    "delete_after",
    "proxy_active",
)
