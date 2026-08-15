"""Generic CLI-flag vs YAML-config resolver.

Extracted from ``cli.py`` to reduce the per-parameter ``_resolved_*``
boilerplate. Each parameter declares its shape once (enum, bool, int,
auto-or-int, etc.) and the resolver handles:

  1. CLI flag wins when the user passed it explicitly
     (``ParameterSource.COMMANDLINE``);
  2. Otherwise the YAML config value (already type-checked + range-validated
     by ``cli_config.load_config``);
  3. Otherwise the ``CONFIG_DEFAULTS`` fallback.

The declarative spec table below is the single source of truth for
"which parameters exist, what's their type, and what values are
allowed". Adding a new tunable is a one-line change here (plus the
matching ``@app.command()`` argument in ``cli.py``).
"""

from __future__ import annotations

from typing import Any, Literal

import typer

from stream2video.cli_helpers import ParameterSource
from stream2video.config import (
    CONFIG_DEFAULTS,
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
#   * ``auto_or_int`` — accepts the literal string "auto" OR a positive int
#                       (encoder_threads, memory_limit_mb)
#   * ``proxy``       — special: CLI --proxy implies proxy_active=True
ParamKind = Literal["enum", "bool", "int", "auto_or_int", "proxy"]


# The declarative spec table. Order matters only for readability; the
# resolver iterates over whatever order the caller passes names in.
#
# Keys:
#   kind   — ParamKind (see above)
#   valid  — for ``enum``: list of allowed values
#   min    — for ``auto_or_int`` / ``int``: minimum accepted value
#            (``0`` for "non-negative", ``1`` for "positive")
PARAM_SPECS: dict[str, dict[str, Any]] = {
    # String-enum parameters with VALID_* whitelists.
    "method": {"kind": "enum", "valid": VALID_METHODS},
    "encoder": {"kind": "enum", "valid": VALID_ENCODERS},
    "video_quality": {"kind": "enum", "valid": VALID_QUALITIES},
    "audio_quality": {"kind": "enum", "valid": VALID_QUALITIES},
    "download_quality": {"kind": "enum", "valid": VALID_DOWNLOAD_QUALITIES},
    "software_fallback": {"kind": "enum", "valid": VALID_SOFTWARE_FALLBACKS},
    "x264_preset": {"kind": "enum", "valid": VALID_X264_PRESETS},
    "output_fps": {"kind": "enum", "valid": VALID_OUTPUT_FPS},
    "output_format": {"kind": "enum", "valid": VALID_OUTPUT_FORMATS},
    # Bool toggle parameters. Tri-state on CLI (None = fall back to
    # config), stored as plain bool in YAML.
    "force": {"kind": "bool"},
    "delete_after": {"kind": "bool"},
    "per_video_dir": {"kind": "bool"},
    "x264_low_memory": {"kind": "bool"},
    "use_crf": {"kind": "bool"},
    "gapless_concat": {"kind": "bool"},
    "low_process_priority": {"kind": "bool"},
    "completion_sound": {"kind": "bool"},
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
    },
    "download_timeout": {
        "kind": "int",
        "min": CONFIG_RANGES["download_timeout"][0],
        "max": CONFIG_RANGES["download_timeout"][1],
    },
    "connect_timeout": {
        "kind": "int",
        "min": CONFIG_RANGES["connect_timeout"][0],
        "max": CONFIG_RANGES["connect_timeout"][1],
    },
    "no_progress_timeout": {
        "kind": "int",
        "min": CONFIG_RANGES["no_progress_timeout"][0],
        "max": CONFIG_RANGES["no_progress_timeout"][1],
    },
    "silence_timeout": {"kind": "int", "min": 1, "max": CONFIG_RANGES["silence_timeout"][1]},
    "segment_encode_timeout": {
        "kind": "int",
        "min": 1,
        "max": CONFIG_RANGES["segment_encode_timeout"][1],
    },
    "final_concat_timeout": {
        "kind": "int",
        "min": 1,
        "max": CONFIG_RANGES["final_concat_timeout"][1],
    },
    "stall_kill_timeout": {
        "kind": "int",
        "min": CONFIG_RANGES["stall_kill_timeout"][0],
        "max": CONFIG_RANGES["stall_kill_timeout"][1],
    },
    "stall_warning_timeout": {
        "kind": "int",
        "min": CONFIG_RANGES["stall_warning_timeout"][0],
        "max": CONFIG_RANGES["stall_warning_timeout"][1],
    },
    "waveform_timeout": {
        "kind": "int",
        "min": CONFIG_RANGES["waveform_timeout"][0],
        "max": CONFIG_RANGES["waveform_timeout"][1],
    },
    "batch_chunk_size": {"kind": "int", "min": 1, "max": CONFIG_RANGES["batch_chunk_size"][1]},
    "min_part_bytes": {"kind": "int", "min": 1, "max": CONFIG_RANGES["min_part_bytes"][1]},
    "rlimit_as_mb": {
        "kind": "int",
        "min": CONFIG_RANGES["rlimit_as_mb"][0],
        "max": CONFIG_RANGES["rlimit_as_mb"][1],
    },
    # Auto-or-int: the CLI flag arrives as a string; when COMMANDLINE
    # the resolver tries ``int(value)``, falling back to ``"auto"``
    # (case-insensitive). Config values are already coerced.
    "encoder_threads": {"kind": "auto_or_int", "min": 1},
    "memory_limit_mb": {"kind": "auto_or_int", "min": 0},
    # Proxy: CLI --proxy implies proxy_active=True. A YAML proxy without
    # proxy_active is inert (matches the GUI's checkbox contract).
    "proxy": {"kind": "proxy"},
    # Preset: handled separately via apply_preset() after spec resolution,
    # but listed here so the resolver can validate its enum-ness.
    "preset": {"kind": "enum", "valid": list(PRESET_NAMES)},
}


class _Resolver:
    """Resolve CLI flags against the YAML config.

    Single-use: instantiate once per CLI invocation, then call
    :meth:`resolve` for each parameter. Typer's ``Context`` is passed in
    at construction time so each call is a one-liner at the call site.
    """

    def __init__(
        self,
        ctx: typer.Context | None,
        config: dict[str, Any],
        console: Any,
    ) -> None:
        self._ctx = ctx
        self._config = config
        self._console = console

    def _source_is_commandline(self, name: str) -> bool:
        """True when the user explicitly passed the flag on the command line."""
        if ParameterSource is None or self._ctx is None:
            return False
        try:
            return self._ctx.get_parameter_source(name) == ParameterSource.COMMANDLINE
        except (AttributeError, ValueError):
            # Typer < 0.9 lacks get_parameter_source; treat as "not from CLI"
            return False

    def _fail(self, name: str, value: Any, msg: str) -> None:
        """Print a red validation error and exit with code 1."""
        self._console.print(f"[red]Invalid {name}:[/red] {value!r} {msg}")
        raise typer.Exit(1)

    def resolve(self, name: str, flag_value: Any) -> Any:
        """Resolve parameter ``name`` against the config.

        ``flag_value`` is the raw CLI value (str / int / bool / None for
        tri-state bools). Returns the resolved Python value.
        """
        spec = PARAM_SPECS.get(name)
        if spec is None:
            raise KeyError(f"No PARAM_SPECS entry for {name!r}")

        kind: ParamKind = spec["kind"]
        from_cli = self._source_is_commandline(name)
        value = flag_value if from_cli else self._config.get(name, CONFIG_DEFAULTS.get(name))

        if kind == "enum":
            if value not in spec["valid"]:
                self._fail(name, value, f"(use {' or '.join(repr(v) for v in spec['valid'])})")
            return value

        if kind == "bool":
            # Tri-state CLI flag: None means "no explicit choice".
            # YAML's type-checked bool passes through unchanged. A stray
            # *string* ("" / "false") would otherwise be coerced by Python's
            # truthiness to True/"False" silently — a config authored as
            # ``force: "false"`` (quoted) used to resolve to True.
            if value is None:
                return False
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                v = value.strip().lower()
                if v in ("1", "true", "yes", "on"):
                    return True
                if v in ("0", "false", "no", "off", ""):
                    return False
                self._fail(name, value, "(must be a boolean: true/false/yes/no/1/0)")
            return bool(value)

        if kind == "int":
            if value is None or isinstance(value, bool):
                self._fail(name, value, "(must be an integer)")
            try:
                iv = int(value)  # type: ignore[arg-type]
            except (TypeError, ValueError) as e:
                self._fail(name, value, "(must be an integer)")
                raise AssertionError("unreachable") from e
            min_val = spec.get("min")
            if min_val is not None and iv < min_val:
                self._fail(name, iv, f"(must be >= {min_val})")
            max_val = spec.get("max")
            if max_val is not None and iv > max_val:
                self._fail(name, iv, f"(must be <= {max_val})")
            return iv

        if kind == "auto_or_int":
            # Config-side values are already coerced by load_config; CLI
            # strings arrive as str and need parsing.
            if isinstance(value, int) and not isinstance(value, bool):
                # Already an int (either YAML int or CLI int via Typer).
                min_val = spec.get("min")
                if min_val is not None and value < min_val:
                    self._fail(name, value, f"(must be >= {min_val} or 'auto')")
                max_val = spec.get("max")
                if max_val is not None and value > max_val:
                    self._fail(name, value, f"(must be <= {max_val} or 'auto')")
                return value
            # String path: "auto" or parseable int.
            if not isinstance(value, str):
                self._fail(name, value, "(must be 'auto' or an integer)")
                return "auto"  # unreachable; _fail raises typer.Exit
            v = value.strip().lower()
            if v == "auto":
                return "auto"
            try:
                iv = int(v)
            except ValueError as e:
                self._fail(name, value, "(use 'auto' or an integer)")
                raise AssertionError("unreachable") from e
            min_val = spec.get("min")
            if min_val is not None and iv < min_val:
                self._fail(name, iv, f"(must be >= {min_val} or 'auto')")
            max_val = spec.get("max")
            if max_val is not None and iv > max_val:
                self._fail(name, iv, f"(must be <= {max_val} or 'auto')")
            return iv

        if kind == "proxy":
            if from_cli:
                # CLI --proxy URL explicitly enables the proxy. YAML
                # numbers (``proxy: 8080``) are coerced to str so yt-dlp
                # receives a URL-ish value, never an int (an
                # int proxy leaked into the subprocess argv as "8080" —
                # yt-dlp then mis-parsed it as an option).
                return str(value) if value else ""
            # YAML: proxy_active is the gate. Without it the address
            # stays inert — the user can toggle it on in the GUI later.
            if self._config.get("proxy_active", False):
                value = self._config.get("proxy", "")
                return str(value) if value else ""
            return ""

        raise AssertionError(f"Unknown ParamKind {kind!r}")


def make_resolver(
    ctx: typer.Context | None,
    config: dict[str, Any],
    console: Any,
) -> _Resolver:
    """Public factory so tests can introspect the resolver's spec table."""
    return _Resolver(ctx, config, console)
