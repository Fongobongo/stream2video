"""``load_config`` — load and validate the optional YAML config file.

Extracted from ``cli.py`` to keep that module under the 1000-line
ceiling. The function takes the Rich ``console`` as an explicit
parameter (instead of importing it) so ``patch("stream2video.cli.console")``
works the same as before.
"""

import logging
from pathlib import Path
from typing import Any

import typer
import yaml

from stream2video.config import (
    AUTO_OR_INT_KEYS,
    CONFIG_DEFAULTS,
    CONFIG_RANGES,
    ENUM_VALIDATORS,
    effective_defaults,
)
from stream2video.gui_helpers import mask_proxy

logger = logging.getLogger("stream2video")


def load_config(config_file: Path | None, console: Any) -> dict:
    """Load and validate configuration file.

    Validates BOTH numeric ranges (``CONFIG_RANGES``) AND enum keys
    (``method``, ``encoder``, ``video_quality``, ``download_quality``,
    ``theme``) against their ``VALID_*`` lists. This is the single
    chokepoint for config-file validation — the CLI flag-path goes
    through its own ``_resolved_*`` check downstream, so an invalid
    YAML value is rejected here regardless of whether the matching
    CLI flag was passed.
    """
    # Start from the EFFECTIVE defaults (CONFIG_DEFAULTS + any
    # user_defaults.json overrides), not a bare CONFIG_DEFAULTS.copy():
    # the GUI and CLI must agree on the starting point, and .copy() is
    # shallow — a YAML config would share the ``recent_projects`` list
    # object with every other config dict in the process.
    config = effective_defaults()
    # Raw YAML dict (before merging into ``config``). Kept so bool-key
    # validation below can distinguish "user wrote 1 in YAML" (int,
    # rejected) from "default value absent" (skip). ``file_config`` is
    # only assigned inside the try/except when the file loads cleanly.
    file_config: dict = {}

    if config_file:
        if not config_file.exists():
            console.print(f"[yellow]Warning:[/yellow] Config file not found: {config_file}")
        else:
            try:
                # The project writes every config/JSON file as UTF-8
                # (settings_io, config writers); let the reader match so
                # a non-ASCII path/comment doesn't mojibake or raise
                # UnicodeDecodeError on a cp1251 Windows host.
                with open(config_file, encoding="utf-8") as f:
                    loaded = yaml.safe_load(f) or {}

                if not isinstance(loaded, dict):
                    raise ValueError("Config file must contain a dictionary")

                file_config = loaded
                config.update(file_config)

                logger.info(f"Loaded config from {config_file}")

            except yaml.YAMLError as e:
                console.print(f"[red]Error parsing config file:[/red] {e}")
                raise typer.Exit(1) from None

            except Exception as e:
                console.print(f"[red]Error loading config file:[/red] {e}")
                raise typer.Exit(1) from None

    # Validate numeric ranges. Preserve the input's int/float type —
    # ``float(config[key])`` silently converts every integer-keyed
    # tunable (``batch_chunk_size`` etc.) to float, and a resolver that
    # later hands ``PipelineConfig(batch_chunk_size=40.0)`` an int
    # slot gets a subtle type mismatch.
    # The auto_or_int keys (``encoder_threads`` / ``memory_limit_mb``)
    # default to the literal string ``"auto"`` in CONFIG_DEFAULTS —
    # that's a legitimate value, not a number, so it's skipped here
    # (the resolver's ``auto_or_int`` path handles it). ``"auto"`` is
    # matched case-insensitively and normalised to lowercase — the SAME
    # rule the CLI flag (cli_resolver) and the GUI's Advanced entries
    # (settings_io / coerce_typed_value) apply, so YAML is no longer the
    # only surface where ``AUTO`` is rejected. See
    # ``config.AUTO_OR_INT_KEYS`` for the single source of truth.
    for key, (min_val, max_val) in CONFIG_RANGES.items():
        if key in config:
            original = config[key]
            # ``bool`` is a subclass of ``int`` and ``float(True) == 1.0``,
            # so without this guard a YAML ``batch_chunk_size: true`` slips
            # past ``float()`` and into ``PipelineConfig`` as a bool on an
            # int-typed slot — a subtle downstream mismatch the
            # cli_resolver-path keys don't get to audit.
            if isinstance(original, bool):
                console.print(f"[red]Invalid {key}:[/red] {original} is a bool, expected a number")
                raise typer.Exit(1)
            if (
                key in AUTO_OR_INT_KEYS
                and isinstance(original, str)
                and original.strip().lower() == "auto"
            ):
                # Canonical lowercase spelling for downstream ``== "auto"``
                # comparisons (concat/encoders, memory budget,
                # validate_pipeline_config). Non-"auto" strings fall
                # through to float() below, same as before.
                config[key] = "auto"
                continue
            try:
                value = float(original)

                if not min_val <= value <= max_val:
                    console.print(
                        f"[red]Invalid {key}:[/red] {value} not in range [{min_val}, {max_val}]"
                    )
                    raise typer.Exit(1)

                # Only rewrite when the type changed meaningfully —
                # YAML ``40`` parses as int and should stay int, and a
                # YAML ``40.0`` on an INT-typed key (timeouts, sizes)
                # must not leak a float into the int-typed PipelineConfig
                # slots downstream. Float-typed keys (threshold, margin)
                # keep their float even when integral (``-30.0``).
                if value.is_integer():
                    if isinstance(original, int):
                        config[key] = original
                    elif isinstance(CONFIG_DEFAULTS.get(key), int):
                        config[key] = int(value)
                    else:
                        config[key] = value
                else:
                    config[key] = value

            except (ValueError, TypeError):
                console.print(f"[red]Invalid {key}:[/red] {config[key]} is not a number")
                raise typer.Exit(1) from None

    # Validate bool keys. YAML booleans (``force: false``) parse to Python
    # bool, but quoted strings (``force: "false"``) parse to the string
    # ``"false"`` which is truthy under ``bool(...)`` — so ``_resolved_bool``
    # later in the run would read it as ``True`` even though the user wrote
    # ``false``. Same hazard for ``0``/``1`` ints: PyYAML keeps them as
    # integers, not bools. Reject any non-bool value the user explicitly
    # wrote in the YAML so downstream ``bool(value)`` matches intent. Keys
    # the user didn't write keep their bool default from CONFIG_DEFAULTS.
    bool_keys = (
        "force",
        "delete_after",
        "per_video_dir",
        "completion_sound",
        "x264_low_memory",
        "use_crf",
        "gapless_concat",
        "low_process_priority",
        "proxy_active",
    )
    for key in bool_keys:
        if key in file_config:
            bool_val = file_config[key]
            if not isinstance(bool_val, bool):
                console.print(f"[red]Invalid {key}:[/red] {bool_val!r} must be true or false")
                raise typer.Exit(1)

    # Validate enum keys against their VALID_* lists. A bad value in
    # either the YAML or CONFIG_DEFAULTS is rejected here so downstream
    # code can assume the value is one of the allowed tokens. The list is
    # derived from config.ENUM_VALIDATORS — the single source of truth —
    # minus ``theme``, which is GUI-only: the CLI never reads or applies
    # it, so a bad theme in a YAML config that the CLI loads shouldn't
    # abort the run.
    enum_specs = [(k, list(v)) for k, v in ENUM_VALIDATORS.items() if k != "theme"]
    for key, valid in enum_specs:
        enum_val: Any = config.get(key)
        if enum_val is None:
            continue
        if enum_val not in valid:
            console.print(
                f"[red]Invalid {key}:[/red] {enum_val!r} "
                f"(use {' or '.join(repr(x) for x in valid)})"
            )
            raise typer.Exit(1)

    # Debug dump must not print the proxy credentials (they can embed
    # user:pass): log a copy with the proxy value masked.
    _config_log = dict(config)
    _config_log["proxy"] = mask_proxy(str(_config_log.get("proxy", "")))
    logger.debug(f"Final config: {_config_log}")
    return config
