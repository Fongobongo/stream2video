"""Single declarative parameter table shared by the CLI and the GUI's
"Copy CLI command" builder.

This module is the ONE source of truth for "which tunables exist,
what's their type, what CLI flag they map to, and what values are
allowed". It must stay dependency-light (config only — no typer/rich)
so the GUI can import it without pulling the CLI stack into a Tk
process.

Consumers:

  * ``cli_resolver`` — resolves a parameter name to its final value
    (CLI flag > YAML config > default) using ``kind`` / ``valid`` /
    ``min`` / ``max``;
  * ``gui_helpers.build_cli_command`` — emits the ``flag`` / aliases
    for every parameter whose value diverges from the effective
    defaults, so a copied command can't drift from the CLI's flag
    names.

Adding a new tunable is a one-line entry here plus the matching
``@app.command()`` argument in ``cli.py``; the copied-command builder
picks it up automatically once the GUI passes the value through.
"""

from __future__ import annotations

from typing import Any, Literal

from stream2video.config import (
    CONFIG_RANGES,
    PRESET_NAMES,
    VALID_DOWNLOAD_QUALITIES,
    VALID_ENCODERS,
    VALID_METHODS,
    VALID_OUTPUT_FORMATS,
    VALID_OUTPUT_FPS,
    VALID_QUALITIES,
    VALID_SOFTWARE_FALLBACKS,
    VALID_X264_PRESETS,
)

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
# always spelled out — the positive flag when True, the ``--no-*`` flag when
# False — so a pasted command can never silently fall back to a user-default
# override the GUI didn't show (audit P1: a ``proxy_active: true`` in
# user_defaults.json re-enabled a proxy the GUI had switched off because the
# copied command carried no negative flag).

# The declarative spec table. Order matters only for readability; the
# resolver iterates over whatever order the caller passes names in.
#
# Keys:
#   kind       — ParamKind (see above)
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
    # String-enum parameters with VALID_* whitelists.
    "method": {"kind": "enum", "valid": VALID_METHODS, "flag": "--method"},
    "encoder": {"kind": "enum", "valid": VALID_ENCODERS, "flag": "--encoder"},
    "video_quality": {"kind": "enum", "valid": VALID_QUALITIES, "flag": "--video-quality"},
    "audio_quality": {"kind": "enum", "valid": VALID_QUALITIES, "flag": "--audio-quality"},
    "download_quality": {
        "kind": "enum",
        "valid": VALID_DOWNLOAD_QUALITIES,
        "flag": "--download-quality",
    },
    "software_fallback": {
        "kind": "enum",
        "valid": VALID_SOFTWARE_FALLBACKS,
        "flag": "--software-fallback",
    },
    "x264_preset": {"kind": "enum", "valid": VALID_X264_PRESETS, "flag": "--x264-preset"},
    "output_fps": {"kind": "enum", "valid": VALID_OUTPUT_FPS, "flag": "--output-fps"},
    "output_format": {"kind": "enum", "valid": VALID_OUTPUT_FORMATS, "flag": "--output-format"},
    # Bool toggle parameters. Tri-state on CLI (None = fall back to
    # config), stored as plain bool in YAML. ``flag`` / ``flag_off`` are
    # the positive / negative spellings; the copied-command builder
    # chooses by comparing the value to the effective defaults.
    "force": {"kind": "bool", "flag": "-f", "flag_off": "--no-force"},
    "delete_after": {"kind": "bool", "flag": "--delete-after", "flag_off": "--no-delete-after"},
    "per_video_dir": {"kind": "bool", "flag": "--per-video-dir", "flag_off": "--no-per-video-dir"},
    "x264_low_memory": {
        "kind": "bool",
        "flag": "--x264-low-memory",
        "flag_off": "--no-x264-low-memory",
    },
    "use_crf": {"kind": "bool", "flag": "--use-crf", "flag_off": "--no-use-crf"},
    "gapless_concat": {
        "kind": "bool",
        "flag": "--gapless-concat",
        "flag_off": "--no-gapless-concat",
    },
    "low_process_priority": {
        "kind": "bool",
        "flag": "--low-process-priority",
        "flag_off": "--no-low-process-priority",
    },
    "completion_sound": {
        "kind": "bool",
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
        "flag": "--proxy-active",
        "flag_off": "--no-proxy-active",
    },
    # Float-typed silence-tuning keys (GUI sliders). ``min``/``max``
    # come from CONFIG_RANGES — the same limits cli_config applies to
    # YAML.
    "threshold": {
        "kind": "float",
        "min": CONFIG_RANGES["threshold"][0],
        "max": CONFIG_RANGES["threshold"][1],
        "flag": "--threshold",
    },
    "min_silence": {
        "kind": "float",
        "min": CONFIG_RANGES["min_silence"][0],
        "max": CONFIG_RANGES["min_silence"][1],
        "flag": "--min-silence",
    },
    "margin": {
        "kind": "float",
        "min": CONFIG_RANGES["margin"][0],
        "max": CONFIG_RANGES["margin"][1],
        "flag": "--margin",
    },
    # The ``min``/``max`` bounds come from CONFIG_RANGES — the SAME
    # limits cli_config applies to YAML, so ``--stall-kill-timeout
    # 99999`` is rejected exactly like its YAML twin
    # ``stall_kill_timeout: 99999`` (the CLI used to accept
    # values the config file rejected — a silent divergence between the
    # two configuration surfaces). The floors exist so a typo'd timeout
    # can't turn the watchdog into a kill-on-startup on slow media.
    "memory_reserve_mb": {
        "kind": "int",
        "min": CONFIG_RANGES["memory_reserve_mb"][0],
        "max": CONFIG_RANGES["memory_reserve_mb"][1],
        "flag": "--memory-reserve-mb",
    },
    "download_timeout": {
        "kind": "int",
        "min": CONFIG_RANGES["download_timeout"][0],
        "max": CONFIG_RANGES["download_timeout"][1],
        "flag": "--download-timeout",
    },
    "connect_timeout": {
        "kind": "int",
        "min": CONFIG_RANGES["connect_timeout"][0],
        "max": CONFIG_RANGES["connect_timeout"][1],
        "flag": "--connect-timeout",
    },
    "no_progress_timeout": {
        "kind": "int",
        "min": CONFIG_RANGES["no_progress_timeout"][0],
        "max": CONFIG_RANGES["no_progress_timeout"][1],
        "flag": "--no-progress-timeout",
    },
    "silence_timeout": {
        "kind": "int",
        "min": 1,
        "max": CONFIG_RANGES["silence_timeout"][1],
        "flag": "--silence-timeout",
    },
    "segment_encode_timeout": {
        "kind": "int",
        "min": 1,
        "max": CONFIG_RANGES["segment_encode_timeout"][1],
        "flag": "--segment-timeout",
    },
    "final_concat_timeout": {
        "kind": "int",
        "min": 1,
        "max": CONFIG_RANGES["final_concat_timeout"][1],
        "flag": "--final-concat-timeout",
    },
    "stall_kill_timeout": {
        "kind": "int",
        "min": CONFIG_RANGES["stall_kill_timeout"][0],
        "max": CONFIG_RANGES["stall_kill_timeout"][1],
        "flag": "--stall-timeout",
    },
    "stall_warning_timeout": {
        "kind": "int",
        "min": CONFIG_RANGES["stall_warning_timeout"][0],
        "max": CONFIG_RANGES["stall_warning_timeout"][1],
        "flag": "--stall-warning-timeout",
    },
    "waveform_timeout": {
        "kind": "int",
        "min": CONFIG_RANGES["waveform_timeout"][0],
        "max": CONFIG_RANGES["waveform_timeout"][1],
        "flag": "--waveform-timeout",
    },
    "batch_chunk_size": {
        "kind": "int",
        "min": 1,
        "max": CONFIG_RANGES["batch_chunk_size"][1],
        "flag": "--batch-chunk-size",
    },
    "min_part_bytes": {
        "kind": "int",
        "min": 1,
        "max": CONFIG_RANGES["min_part_bytes"][1],
        "flag": "--min-part-bytes",
    },
    "rlimit_as_mb": {
        "kind": "int",
        "min": CONFIG_RANGES["rlimit_as_mb"][0],
        "max": CONFIG_RANGES["rlimit_as_mb"][1],
        "flag": "--rlimit-as-mb",
    },
    # Auto-or-int: the CLI flag arrives as a string; when COMMANDLINE
    # the resolver tries ``int(value)``, falling back to ``"auto"``
    # (case-insensitive). Config values are already coerced. The
    # ``min``/``max`` bounds come from CONFIG_RANGES — the same limits
    # coerce_typed_value / cli_config apply, so ``--encoder-threads
    # 99999`` is rejected exactly like its YAML twin (previously the
    # upper bound was only enforced for ints from the flag path, and
    # not at all on the config path).
    "encoder_threads": {
        "kind": "auto_or_int",
        "min": CONFIG_RANGES["encoder_threads"][0],
        "max": CONFIG_RANGES["encoder_threads"][1],
        "flag": "--encoder-threads",
    },
    "memory_limit_mb": {
        "kind": "auto_or_int",
        "min": CONFIG_RANGES["memory_limit_mb"][0],
        "max": CONFIG_RANGES["memory_limit_mb"][1],
        "flag": "--memory-limit-mb",
    },
    # Proxy: CLI --proxy implies proxy_active=True. A YAML proxy without
    # proxy_active is inert (matches the GUI's checkbox contract).
    "proxy": {"kind": "proxy", "flag": "--proxy"},
    # Preset: handled separately via apply_preset() after spec resolution,
    # but listed here so the resolver can validate its enum-ness.
    "preset": {"kind": "enum", "valid": list(PRESET_NAMES), "flag": "--preset"},
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
