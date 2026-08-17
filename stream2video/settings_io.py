"""Pure snapshot + I/O helpers for the GUI's settings / defaults
persistence — extracted from ``gui.py`` (incremental refactor).

Two categories of pure logic live here:

  1. ``build_save_settings_snapshot`` — turns a widget-value dict into
     the dict written to ``settings.json`` on close / save. The GUI
     previously read each combobox / entry inline; extracting the
     shape here lets the test suite verify the field list and the bool
     casts without driving the Tk main loop. The GUI keeps only the
     reads (``combo_method.get()`` etc.) and forwards them as a tiny
     dict.
  2. ``build_user_defaults_snapshot`` — analogue for
     ``user_defaults.json`` (the per-user "factory defaults" file that
     overrides ``CONFIG_DEFAULTS`` on next startup). Same shape, one
     fewer field (``window_geometry`` is session-only and not saved).

Everything that touches the clipboard, the messagebox, or a Tk widget
stays in ``gui.py`` — only pure transformations live here.
"""

from __future__ import annotations

from typing import Any

from stream2video.config import CONFIG_DEFAULTS as _CONFIG_DEFAULTS
from stream2video.config import USER_DEFAULT_KEYS
from stream2video.param_specs import PARAM_SPECS

# Canonical key order for ``settings.json``. Session-only state
# (``input_path``, ``output_dir``, ``theme``, ``window_geometry``) is
# explicit; every TUNable key is derived from the single tunable table
# (``PARAM_SPECS``), so adding a tunable there automatically persists
# it everywhere — a hand-maintained twin list here is what drifted
# before (audit round 23 P9). ``recent_projects`` is NOT included: it
# lives on the GUI's ``self.settings`` and is written alongside via
# the whole-dict save path. The three slider floats (threshold /
# min_silence / margin) are also carried by ``self.settings`` (slider
# floats) rather than by individual widget reads, so they stay out of
# this widget-driven save.
_SLIDER_FLOAT_KEYS = ("threshold", "min_silence", "margin")
SAVE_SETTINGS_KEYS: tuple[str, ...] = (
    "input_path",
    "output_dir",
    *tuple(k for k in PARAM_SPECS if k not in _SLIDER_FLOAT_KEYS),
    "theme",
    "window_geometry",
)

# Canonical key order for ``user_defaults.json``. The user defaults file
# stores only the tunables a user might want as their personal factory
# defaults; session-only state (input_path, output_dir, recent_projects,
# window_geometry) is intentionally excluded so a fresh GUI start on
# another machine doesn't inherit a previous session's paths.
#
# Single source of truth is ``config.USER_DEFAULT_KEYS`` — a second
# hand-maintained list here previously drifted and silently dropped the
# 18 tunables (encoder_threads, output_fps, memory_*, all the pipeline
# timeouts, batch_chunk_size, min_part_bytes…) from "Save current as
# defaults".
USER_DEFAULTS_KEYS: tuple[str, ...] = tuple(USER_DEFAULT_KEYS)


# Declarative table for the 18 "Advanced" GUI widgets (the CLI-only
# tunables the audit found without a GUI surface). Single source of
# truth for how each key is rendered and parsed:
#
#   * ``kind``    — ``"combo"`` (CTkComboBox) or ``"entry"``
#                   (CTkEntry, parsed via ``parse_advanced_widgets``) —
#                   pure layout metadata;
#   * ``label``   — row label in the Advanced section;
#   * ``tooltip`` — hover help.
#
# The VALUES a combo offers and the type each entry parses to are NOT
# restated here: they come from ``PARAM_SPECS`` (the shared pipeline-
# parameter table the CLI resolver and the GUI's copied-command
# builder use), so a tunable's allowed choices / type live in exactly
# one place (audit round 24 P11 — this table used to duplicate
# ``valid`` and ``value_type`` and could drift from the real spec).
ADVANCED_WIDGET_SPECS: dict[str, dict[str, Any]] = {
    "software_fallback": {
        "kind": "combo",
        "label": "SW fallback:",
        "tooltip": (
            "Encoder fallback policy when the selected HW encoder is "
            "unavailable or fails mid-run.\nask — confirm before falling "
            "back to libx264 (default)\ndisabled — fail instead of "
            "falling back\nenabled — silently fall back to libx264"
        ),
    },
    "x264_preset": {
        "kind": "combo",
        "label": "x264 preset:",
        "tooltip": "CPU preset for libx264 encodes (ultrafast = fastest, "
        "largest files; slow = smallest files, slower encode).",
    },
    "output_fps": {
        "kind": "combo",
        "label": "Output FPS:",
        "tooltip": "Output frame rate.\nsource — keep the input's cadence (default)\n24/25/30/50/60 — force a constant frame rate (duplicates frames).",
    },
    "encoder_threads": {
        "kind": "entry",
        "label": "Threads:",
        "tooltip": "Encoder thread budget.\nauto — let ffmpeg decide (default)\n1-1024 — cap the thread count.",
    },
    "memory_limit_mb": {
        "kind": "entry",
        "label": "RAM limit (MB):",
        "tooltip": "RAM budget for the encode.\nauto — 60% of total RAM (default)\n0 — disable the in-process budget check.",
    },
    "memory_reserve_mb": {
        "kind": "entry",
        "label": "RAM reserve (MB):",
        "tooltip": "Available-RAM floor; below it new heavy phases refuse to start.",
    },
    "rlimit_as_mb": {
        "kind": "entry",
        "label": "RLIMIT_AS (MB):",
        "tooltip": "POSIX-only hard cap on each ffmpeg subprocess's virtual "
        "address space. 0 disables (default). Ignored on Windows.",
    },
    "batch_chunk_size": {
        "kind": "entry",
        "label": "Batch chunk:",
        "tooltip": "Keep-segments per batch filter invocation (batch method).",
    },
    "min_part_bytes": {
        "kind": "entry",
        "label": "Min part (B):",
        "tooltip": "Minimum bytes for a resumed part file to be treated as "
        "valid; smaller files are re-encoded.",
    },
    "download_timeout": {
        "kind": "entry",
        "label": "DL timeout (s):",
        "tooltip": "Absolute ceiling for the whole download phase.",
    },
    "connect_timeout": {
        "kind": "entry",
        "label": "Connect timeout:",
        "tooltip": "Seconds to wait for the first download byte.",
    },
    "no_progress_timeout": {
        "kind": "entry",
        "label": "No-progress timeout:",
        "tooltip": "Mid-download stall watchdog (no progress for this long aborts).",
    },
    "segment_encode_timeout": {
        "kind": "entry",
        "label": "Segment timeout:",
        "tooltip": "Per-segment encode watchdog (segment method).",
    },
    "final_concat_timeout": {
        "kind": "entry",
        "label": "Concat timeout:",
        "tooltip": "Absolute ceiling on the final concat pass.",
    },
    "silence_timeout": {
        "kind": "entry",
        "label": "Silence timeout:",
        "tooltip": "Ceiling on silence detection.",
    },
    "stall_kill_timeout": {
        "kind": "entry",
        "label": "Stall kill (s):",
        "tooltip": "No-progress for this long during an ffmpeg phase kills it.",
    },
    "stall_warning_timeout": {
        "kind": "entry",
        "label": "Stall warn (s):",
        "tooltip": "No-progress for this long during an ffmpeg phase logs a warning.",
    },
    "waveform_timeout": {
        "kind": "entry",
        "label": "Waveform timeout:",
        "tooltip": "Ceiling on the waveform-preview ffmpeg decode.",
    },
}

# Widget attribute name for a key: ``combo_<key>`` / ``entry_<key>``.
ADVANCED_WIDGET_NAMES: dict[str, str] = {
    key: f"{spec['kind']}_{key}" for key, spec in ADVANCED_WIDGET_SPECS.items()
}


def _advanced_spec_valid(key: str) -> tuple[str, ...]:
    """Allowed choices for an Advanced combo — from ``PARAM_SPECS``,
    the single source of truth (audit round 24 P11: the widget table
    used to duplicate the list and could drift from the CLI resolver).
    """
    return tuple(PARAM_SPECS[key]["valid"])


def _advanced_spec_is_auto_or_int(key: str) -> bool:
    """Whether an Advanced entry key accepts ``"auto"`` or an int —
    the key's PARAM_SPECS kind, not a restated widget-table field."""
    return PARAM_SPECS[key]["kind"] == "auto_or_int"


def parse_advanced_widgets(
    raw: dict[str, str],
    current: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Parse the Advanced-section widget strings into typed config values.

    ``raw`` maps a config key to the widget's *string* value
    (``CTkEntry.get()`` / ``CTkComboBox.get()``). Combo values pass
    through unchanged; entry values are parsed per the key's
    ``PARAM_SPECS`` kind:

      * ``auto_or_int`` — the literal ``"auto"`` (case-insensitive) or
        an int; anything else falls back to ``current[key]`` (the GUI's
        live settings, which are already typed);
      * anything else — an int, else the same fallback.

    The fallback keeps a half-typed field from crashing the run with a
    ``TypeError``/``ValueError`` — the widget shows the bad text, the
    run uses the last known-good value. Callers that must REJECT invalid
    text instead of silently substituting (Start / Copy CLI command)
    check :func:`validate_advanced_widgets` FIRST — the two functions
    share the same parsing rules so they can never disagree about what
    "invalid" means. Pure: no widget reads, no I/O.
    """
    current = current or {}
    out: dict[str, Any] = {}
    for key, spec in ADVANCED_WIDGET_SPECS.items():
        value = raw.get(key)
        if value is None:
            continue
        if spec["kind"] == "combo":
            out[key] = value
            continue
        text = str(value).strip()
        if _advanced_spec_is_auto_or_int(key) and text.lower() == "auto":
            out[key] = "auto"
            continue
        try:
            out[key] = int(text)
        except ValueError:
            out[key] = current.get(key, _CONFIG_DEFAULTS.get(key))
    return out


def validate_advanced_widgets(raw: dict[str, str]) -> dict[str, str]:
    """Validate the Advanced-section widget text; return per-key errors.

    Empty dict = every widget parses. A non-empty result maps each bad
    key to a human-readable error string, mirroring the CLI resolver's
    rejection messages (enum keys quote the valid choices; int keys
    quote the allowed range). The GUI's Start / Copy gates block on
    these errors so the run can never silently use a value different
    from what the entry shows (audit P2: the CLI rejects ``abc`` with an
    explicit error; the GUI used to run with the previous value while
    still showing the typed garbage — a false "applied" impression).

    Pure: no widget reads, no I/O; exactly the parse rules of
    :func:`parse_advanced_widgets` plus range checks against
    ``CONFIG_RANGES`` — the same bounds the CLI resolver and
    ``validate_pipeline_config`` enforce.
    """
    from stream2video.config import CONFIG_RANGES

    errors: dict[str, str] = {}
    for key, spec in ADVANCED_WIDGET_SPECS.items():
        value = raw.get(key)
        if value is None:
            continue
        if spec["kind"] == "combo":
            valid = _advanced_spec_valid(key)
            if value not in valid:
                errors[key] = f"{key} {value!r} (use {' or '.join(repr(v) for v in valid)})"
            continue
        text = str(value).strip()
        if _advanced_spec_is_auto_or_int(key) and text.lower() == "auto":
            continue
        try:
            parsed = int(text)
        except ValueError:
            what = "'auto' or an integer" if _advanced_spec_is_auto_or_int(key) else "an integer"
            errors[key] = f"{key} {value!r} (must be {what})"
            continue
        lo, hi = CONFIG_RANGES[key]
        if not lo <= parsed <= hi:
            allowed = (
                f"'auto' or {lo}..{hi}" if _advanced_spec_is_auto_or_int(key) else f"{lo}..{hi}"
            )
            errors[key] = f"{key} {parsed} out of range [{allowed}]"
    return errors


def build_settings_payload(
    snapshot: dict[str, Any],
    baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the subset of ``snapshot`` that must actually be written
    to ``settings.json``.

    A key is written when its value differs from ``baseline`` (default:
    ``effective_defaults()``) OR when it is one of the session keys
    (``input_path`` / ``output_dir`` / ``window_geometry``), which are
    per-machine state and always persist.

    The delta rule is what stops settings.json from permanently
    shadowing user_defaults.json: an untouched tunable keeps its
    user_defaults.json value even after the GUI rewrites settings.json
    on close, so "Save current as defaults" changes actually take
    effect for keys the user never tweaked in the GUI. Overwriting the
    file with the payload also drops stale keys from older versions.
    """
    from stream2video.config import effective_defaults as _effective_defaults

    baseline = baseline if baseline is not None else _effective_defaults()
    payload: dict[str, Any] = {}
    for key, value in snapshot.items():
        if key in _SESSION_SAVE_KEYS or baseline.get(key) != value:
            payload[key] = value
    return payload


# Keys always persisted to settings.json regardless of the delta rule
# (per-machine session state, not tunables).
_SESSION_SAVE_KEYS: frozenset[str] = frozenset({"input_path", "output_dir", "window_geometry"})


def build_save_settings_snapshot(widgets: dict[str, Any]) -> dict[str, Any]:
    """Construct the dict passed to ``gui_settings.save_settings``.

    ``widgets`` is a tiny dict the GUI builds from its widget reads::

        {
            "input_path":        str,   # entry_input.get().strip()
            "output_dir":        str,   # entry_output.get().strip()
            "method":            str,   # combo_method.get()
            "encoder":           str,   # combo_encoder.get()
            "video_quality":     str,   # combo_video_quality.get()
            "audio_quality":     str,   # combo_audio_quality.get()
            "download_quality":  str,   # combo_download_quality.get()
            "output_format":     str,   # combo_output_format.get()
            "force":             bool,  # bool(chk_force.get())
            "delete_after":      bool,  # bool(chk_delete.get())
            "per_video_dir":     bool,  # bool(chk_per_video_dir.get())
            "completion_sound":  bool,  # bool(chk_completion_sound.get())
            "x264_low_memory":   bool,  # bool(chk_x264_low_memory.get())
            "use_crf":           bool,  # bool(chk_use_crf.get())
            "gapless_concat":    bool,  # bool(chk_gapless_concat.get())
            "low_process_priority": bool,  # bool(chk_low_process_priority.get())
            "preset":            str,   # combo_preset.get()
            "theme":             str,   # combo_theme.get()
            "window_geometry":   str,   # self.geometry()
        }

    Returns a new dict containing exactly the keys in
    ``SAVE_SETTINGS_KEYS`` (in that order) so the on-disk JSON stays
    stable across runs. Pure: no widget reads, no I/O.

    A missing widget key used to raise ``KeyError`` here and — because
    the caller wrapped the whole save in one ``except`` — silently
    discard the *entire* settings snapshot on behalf of a single
    absent field. Use ``widgets.get(key)`` instead: the missing key
    is serialized as ``null`` and everything else still persists.
    """
    snapshot: dict[str, Any] = {}
    missing: list[str] = []
    for key in SAVE_SETTINGS_KEYS:
        if key in widgets:
            snapshot[key] = widgets[key]
        else:
            snapshot[key] = None
            missing.append(key)
    if missing:
        import logging

        logging.getLogger(__name__).warning(
            "build_save_settings_snapshot: widgets missing keys %s — serializing as null",
            missing,
        )
    return snapshot


def build_user_defaults_snapshot(
    widgets: dict[str, Any],
    current: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Construct the dict passed to ``config.save_user_defaults``.

    ``widgets`` has the same shape as for
    :func:`build_save_settings_snapshot` (the threshold / min_silence /
    margin floats come from the slider-synced ``self.settings`` in the
    GUI, not from individual widget reads — the GUI's existing
    ``_sync_slider_entries`` call already normalises them). Returns a
    new dict containing exactly the keys in ``USER_DEFAULTS_KEYS``.

    Keys absent from ``widgets`` are taken from ``current`` (the GUI's
    live ``self.settings``) when supplied — that dict holds the *current
    effective* values, including ones the user set via an earlier
    ``user_defaults.json`` / preset but that have no dedicated widget
    (``encoder_threads``, ``output_fps``, ``memory_*``, timeouts, …).
    When ``current`` is omitted or a key is absent from both, the
    fallback is ``config.CONFIG_DEFAULTS`` — the previous behaviour
    silently reset those 18 tunables to factory on every "Save current
    as defaults".
    Pure: no widget reads, no I/O.
    """
    snapshot: dict[str, Any] = {}
    for key in USER_DEFAULTS_KEYS:
        if key in widgets:
            snapshot[key] = widgets[key]
        elif current is not None and key in current:
            snapshot[key] = current[key]
        else:
            snapshot[key] = _CONFIG_DEFAULTS.get(key)
    return snapshot
