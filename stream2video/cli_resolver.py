"""Generic CLI-flag vs YAML-config resolver.

Extracted from ``cli.py`` to reduce the per-parameter ``_resolved_*``
boilerplate. Each parameter declares its shape once (enum, bool, int,
float, auto-or-int, etc.) and the resolver handles:

  1. CLI flag wins when the user passed it explicitly
     (``ParameterSource.COMMANDLINE``);
  2. Otherwise the YAML config value (already type-checked + range-validated
     by ``cli_config.load_config``);
  3. Otherwise the ``CONFIG_DEFAULTS`` fallback.

The declarative spec table lives in ``param_specs`` — the single source
of truth for "which parameters exist, what's their type, and what
values are allowed", shared with ``gui_helpers.build_cli_command`` so
the CLI flags and the GUI's copied commands can't drift apart. Adding a
new tunable is a one-line change there (plus the matching
``@app.command()`` argument in ``cli.py``).
"""

from __future__ import annotations

import math
from typing import Any

import typer

from stream2video.cli_helpers import ParameterSource
from stream2video.config import CONFIG_DEFAULTS
from stream2video.param_specs import PARAM_SPECS, ParamKind

__all__ = ["is_from_cli", "make_resolver"]


def is_from_cli(ctx: typer.Context | None, name: str) -> bool:
    """True when the user explicitly passed ``name`` on the command line.

    Public so hosts (cli.py, tests, copied-command builders) can ask the
    same question the resolver answers internally — e.g. whether a bool
    flag was explicitly flipped so a generated command can mirror it.
    """
    if ParameterSource is None or ctx is None:
        return False
    try:
        return ctx.get_parameter_source(name) == ParameterSource.COMMANDLINE
    except (AttributeError, ValueError):
        # Typer < 0.9 lacks get_parameter_source; treat as "not from CLI"
        return False


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
        # Explicit --proxy-active/--no-proxy-active pin (audit P1). None =
        # "no explicit choice" — the proxy kind then falls back to the
        # config's proxy_active gate. Set via :meth:`pin_proxy_active`
        # BEFORE resolving "proxy".
        self._proxy_active_pin: bool | None = None

    def pin_proxy_active(self, active: bool) -> None:
        """Pin the proxy gate state from an explicit CLI flag.

        Called before ``resolve("proxy", ...)`` when the user passed
        ``--proxy-active`` / ``--no-proxy-active``. The pin overrides
        the config's ``proxy_active`` key in either direction — the
        copied-command case: the GUI's proxy checkbox OFF pastes as
        ``--no-proxy-active`` so a ``proxy_active: true`` stored in
        user_defaults.json cannot re-enable the stored address.
        """
        self._proxy_active_pin = bool(active)

    def _source_is_commandline(self, name: str) -> bool:
        """True when the user explicitly passed the flag on the command line."""
        return is_from_cli(self._ctx, name)

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
            # When neither surface supplied a value, fall back to the
            # CONFIG_DEFAULTS entry — NOT a hard-coded False: bool flags
            # whose default is True (gapless_concat / per_video_dir /
            # completion_sound) used to silently flip off whenever a host
            # or test fed the resolver a dict missing that key.
            if value is None:
                default = CONFIG_DEFAULTS.get(name, False)
                return bool(default)
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
            # A YAML / config-path float on an int slot must be a WHOLE
            # number: ``int(10.9)`` would silently truncate to 10 (the
            # audit's P1). ``load_config`` rejects a fractional value on
            # an explicit YAML key, but hosts and tests can feed raw dicts
            # (and a quoted ``"10.9"`` parses to a float via ``float``) —
            # so the resolver is the second chokepoint that must agree.
            if isinstance(value, float) and not value.is_integer():
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

        if kind == "float":
            # Float-typed keys (threshold / min_silence / margin).
            # Config-side values are already range-checked by
            # load_config; CLI values arrive as float (Typer converts
            # the string) and are range-checked here against the SAME
            # CONFIG_RANGES bounds the YAML path enforces.
            if value is None or isinstance(value, bool):
                self._fail(name, value, "(must be a number)")
            try:
                fv = float(value)  # type: ignore[arg-type]
            except (TypeError, ValueError) as e:
                self._fail(name, value, "(must be a number)")
                raise AssertionError("unreachable") from e
            # NaN / ±Infinity: ``fv < min`` and ``fv > max`` are both
            # False for nan, so the range checks below would silently
            # pass it into PipelineConfig (audit round 15 P1 — the CLI
            # flag path accepted ``--threshold nan`` exactly like the
            # YAML path did).
            if not math.isfinite(fv):
                self._fail(name, value, "(must be a finite number)")
            min_val = spec.get("min")
            if min_val is not None and fv < min_val:
                self._fail(name, fv, f"(must be >= {min_val})")
            max_val = spec.get("max")
            if max_val is not None and fv > max_val:
                self._fail(name, fv, f"(must be <= {max_val})")
            return fv

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
                # CLI --proxy URL explicitly enables the proxy... unless
                # an explicit --no-proxy-active pin (audit P1) flips it
                # off (``--proxy http://p --no-proxy-active`` pins the
                # address but keeps direct connections — rare, but the
                # pin is the later, more explicit choice and wins).
                if self._proxy_active_pin is False:
                    return ""
                return str(value) if value else ""
            if self._proxy_active_pin is not None:
                # Explicit gate flag without an explicit URL: True means
                # use the stored address, False disables the proxy even
                # when the config keeps proxy_active: true.
                if not self._proxy_active_pin:
                    return ""
                value = self._config.get("proxy", "")
                return str(value) if value else ""
            # YAML: proxy_active is the gate. Without it the address
            # stays inert — the user can toggle it on in the GUI later.
            # Strict bool check (NOT plain truthiness): a quoted
            # ``proxy_active: "false"`` parses to the truthy string
            # ``"false"`` and would otherwise enable the proxy against
            # the user's intent. ``load_config`` rejects such values for
            # YAML, but hosts/tests may feed raw dicts to the resolver.
            if isinstance(self._config.get("proxy_active"), bool) and self._config["proxy_active"]:
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
