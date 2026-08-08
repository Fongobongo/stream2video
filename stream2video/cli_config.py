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
    CONFIG_DEFAULTS,
    CONFIG_RANGES,
    VALID_DOWNLOAD_QUALITIES,
    VALID_ENCODERS,
    VALID_METHODS,
    VALID_OUTPUT_FORMATS,
    VALID_OUTPUT_FPS,
    VALID_QUALITIES,
    VALID_SOFTWARE_FALLBACKS,
    VALID_X264_PRESETS,
)

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
    config = CONFIG_DEFAULTS.copy()
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
                with open(config_file) as f:
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

    # Validate numeric ranges.
    for key, (min_val, max_val) in CONFIG_RANGES.items():
        if key in config:
            try:
                value = float(config[key])

                if not min_val <= value <= max_val:
                    console.print(
                        f"[red]Invalid {key}:[/red] {value} not in range [{min_val}, {max_val}]"
                    )
                    raise typer.Exit(1)

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
    )
    for key in bool_keys:
        if key in file_config:
            bool_val = file_config[key]
            if not isinstance(bool_val, bool):
                console.print(f"[red]Invalid {key}:[/red] {bool_val!r} must be true or false")
                raise typer.Exit(1)

    # Validate enum keys against their VALID_* lists. A bad value in
    # either the YAML or CONFIG_DEFAULTS is rejected here so downstream
    # code can assume the value is one of the allowed tokens. ``theme`` is
    # GUI-only — the CLI never reads or applies it — so it's intentionally
    # excluded from the enum validation here (a bad theme in a YAML config
    # that the CLI loads shouldn't abort the run).
    enum_specs = [
        ("method", VALID_METHODS),
        ("encoder", VALID_ENCODERS),
        ("video_quality", VALID_QUALITIES),
        ("audio_quality", VALID_QUALITIES),
        ("download_quality", VALID_DOWNLOAD_QUALITIES),
        ("software_fallback", VALID_SOFTWARE_FALLBACKS),
        ("x264_preset", VALID_X264_PRESETS),
        ("output_fps", VALID_OUTPUT_FPS),
        ("output_format", VALID_OUTPUT_FORMATS),
    ]
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

    logger.debug(f"Final config: {config}")
    return config
