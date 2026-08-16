"""``load_config`` — load and validate the optional YAML config file.

Extracted from ``cli.py`` to keep that module under the 1000-line
ceiling. The function takes the Rich ``console`` as an explicit
parameter (instead of importing it) so ``patch("stream2video.cli.console")``
works the same as before.
"""

import difflib
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
from stream2video.param_specs import PARAM_SPECS

logger = logging.getLogger("stream2video")

# Keys that exist ONLY on the CLI as flags, not in CONFIG_DEFAULTS —
# they can end up in a YAML file (the pre-audit README documented
# ``log_format`` as a YAML setting) and get rejected as unknown. The
# nearest-match suggestion is misleading for them: ``log_format`` is a
# Levenshtein-neighbour of ``output_format``, which tunes the container
# (mp4/mp3/...), not logging. Each entry maps the bogus YAML key to the
# flag the user should migrate to instead.
_CLI_ONLY_FLAGS: dict[str, str] = {
    "log_format": "--log-format",
}


class _LoadedConfig(dict):
    """A plain ``dict`` that ALSO records which keys were EXPLICITLY
    written in the YAML file.

    ``apply_preset`` reads ``explicit_keys`` to honour the contract
    "an explicit YAML key wins per-key over the preset" (audit round 13
    P1). It's a ``dict`` subclass so every existing consumer (resolver,
    tests that ``==``-compare it, ``dict(config)`` copies) keeps working
    unchanged.
    """

    explicit_keys: frozenset[str] = frozenset()


# Keys whose PARAM_SPECS type is an integer (plain int or the
# auto-or-int special form). YAML may hand these a float; the numeric
# validator rejects any non-integral float so ``PipelineConfig`` never
# receives ``10.9`` on an int slot (audit round 13 P3 — previously the
# resolver's unconditional ``int(value)`` silently truncated).
_INT_SPEC_KEYS = frozenset(
    name for name, spec in PARAM_SPECS.items() if spec["kind"] in ("int", "auto_or_int")
)


def load_config(config_file: Path | None, console: Any) -> dict:
    """Load and validate configuration file.

    Validates BOTH numeric ranges (``CONFIG_RANGES``) AND enum keys
    (``method``, ``encoder``, ``video_quality``, ``download_quality``,
    ``theme``) against their ``VALID_*`` lists. This is the single
    chokepoint for config-file validation — the CLI flag-path goes
    through its own ``_resolved_*`` check downstream, so an invalid
    YAML value is rejected here regardless of whether the matching
    CLI flag was passed.

    The returned dict carries ``explicit_keys`` (via the
    :class:`_LoadedConfig` subclass): the frozenset of keys actually
    written in the YAML file. ``apply_preset`` consults it so an
    explicit YAML entry wins per-key over the resource preset (audit
    round 13 P1) while unset managed keys pick up the preset's value.
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

    # Reject unknown keys (audit round 12). Previously the whole YAML was
    # merged via ``config.update(file_config)`` and only KNOWN keys were
    # validated, so a typo (``threshhold: -25``) or a key that doesn't
    # exist as a tunable (``log_format: json`` — log format is CLI-flag
    # only) loaded silently and never had any effect: the user kept
    # tuning a knob that wasn't connected. Each bad key is reported with
    # its nearest valid names so the typo is one edit away.
    #
    # Keys that exist ONLY as CLI flags get a dedicated message instead
    # of the Levenshtein suggestion: ``log_format`` is close enough to
    # ``output_format`` for difflib to offer it as a correction, which
    # points users at an entirely different knob (container/codec vs
    # log format). The right guidance is "remove the key, use the flag"
    # (audit round 13 feedback on the breaking-change messaging).
    unknown = sorted(set(file_config) - set(CONFIG_DEFAULTS))
    if unknown:
        for key in unknown:
            flag = _CLI_ONLY_FLAGS.get(key)
            if flag:
                console.print(
                    f"[red]Unknown config key:[/red] {key!r} is a CLI flag "
                    f"({flag}), not a config key — remove it from the YAML "
                    f"file and pass {flag} on the command line"
                )
                continue
            near = difflib.get_close_matches(key, list(CONFIG_DEFAULTS), n=3)
            hint = f" (did you mean {' or '.join(repr(k) for k in near)}?)" if near else ""
            console.print(f"[red]Unknown config key:[/red] {key!r}{hint}")
        raise typer.Exit(1)

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

                # An int-typed key with a FRACTIONAL float silently
                # truncated downstream: YAML parses ``download_timeout:
                # 10.9`` as a float, passes float(); the resolver's int
                # branch did ``int(value)`` → 10, and a GUI Advanced field
                # rejected the same value. All three surfaces now agree
                # on one rule: an int-typed slot takes a whole number or
                # nothing (audit round 13 P1). Only EXPLICIT file keys are
                # checked — untouched keys keep their (already-int)
                # defaults, never a float.
                if not value.is_integer() and key in _INT_SPEC_KEYS and key in file_config:
                    console.print(f"[red]Invalid {key}:[/red] {original!r} is not an integer")
                    raise typer.Exit(1)

                # Only rewrite when the type changed meaningfully —
                # YAML ``40`` parses as int and should stay int, and a
                # YAML ``40.0`` on an INT-typed key (timeouts, sizes)
                # must not leak a float into the int-typed PipelineConfig
                # slots downstream. Float-typed keys (threshold, margin)
                # keep their float even when integral (``-30.0``).
                # The auto_or_int keys (encoder_threads / memory_limit_mb)
                # have a str default ("auto"), so the CONFIG_DEFAULTS-typed
                # branch below can't catch them — yet a QUOTED number
                # (``encoder_threads: "8"``) parses to a str, passes
                # float(), and would leak 8.0 into the config, which the
                # resolver's auto_or_int path then rejects as "not an
                # integer" — same class of bug the "auto" case fix closed.
                if value.is_integer():
                    if isinstance(original, int):
                        config[key] = original
                    elif key in AUTO_OR_INT_KEYS or isinstance(CONFIG_DEFAULTS.get(key), int):
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
    #
    # The key set is derived from ``PARAM_SPECS`` (kind == "bool") — the
    # same table the resolver uses, so adding a new bool tunable there
    # picks it up here automatically (audit round 12: this was a second
    # hand-maintained list that could drift).
    bool_keys = tuple(name for name, spec in PARAM_SPECS.items() if spec["kind"] == "bool")
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
    result = _LoadedConfig(config)
    result.explicit_keys = frozenset(file_config.keys())
    return result
