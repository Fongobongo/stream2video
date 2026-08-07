"""Pure snapshot + I/O helpers for the GUI's settings / defaults
persistence — extracted from ``gui.py`` (Этап 10 incremental refactor).

Three categories of pure logic live here:

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
  3. ``write_cli_config_yaml`` — writes the tiny ``threshold /
     min_silence / margin`` YAML so a "Copy CLI command" paste can be
     run with ``-c stream2video_cli_config.yaml`` and match the GUI's
     slider values. Pure file I/O — taking ``out_dir`` + the three
     values means the GUI side just resolves the path and forwards.

Everything that touches the clipboard, the messagebox, or a Tk widget
stays in ``gui.py`` — only pure transformations and file writes live
here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# Canonical key order for ``settings.json`` — session-only state
# (``window_geometry``, ``recent_projects``) is included; pure tunables
# (threshold / min_silence / margin) come from ``self.config`` (slider
# floats) not from individual widget reads.
SAVE_SETTINGS_KEYS: tuple[str, ...] = (
    "input_path",
    "output_dir",
    "method",
    "encoder",
    "video_quality",
    "audio_quality",
    "download_quality",
    "output_format",
    "force",
    "delete_after",
    "per_video_dir",
    "completion_sound",
    "x264_low_memory",
    "use_crf",
    "gapless_concat",
    "low_process_priority",
    "preset",
    "theme",
    "proxy",
    "window_geometry",
)

# Canonical key order for ``user_defaults.json``. The user defaults file
# stores only the tunables a user might want as their personal factory
# defaults; session-only state (input_path, output_dir, recent_projects,
# window_geometry) is intentionally excluded so a fresh GUI start on
# another machine doesn't inherit a previous session's paths.
USER_DEFAULTS_KEYS: tuple[str, ...] = (
    "threshold",
    "min_silence",
    "margin",
    "method",
    "encoder",
    "video_quality",
    "audio_quality",
    "download_quality",
    "output_format",
    "force",
    "delete_after",
    "per_video_dir",
    "completion_sound",
    "x264_low_memory",
    "use_crf",
    "gapless_concat",
    "low_process_priority",
    "preset",
    "theme",
    "proxy",
)


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
    """
    snapshot: dict[str, Any] = {}
    for key in SAVE_SETTINGS_KEYS:
        snapshot[key] = widgets[key]
    return snapshot


def build_user_defaults_snapshot(widgets: dict[str, Any]) -> dict[str, Any]:
    """Construct the dict passed to ``config.save_user_defaults``.

    ``widgets`` has the same shape as for
    :func:`build_save_settings_snapshot` (the threshold / min_silence /
    margin floats come from the slider-synced ``self.config`` in the
    GUI, not from individual widget reads — the GUI's existing
    ``_sync_slider_entries`` call already normalises them). Returns a
    new dict containing exactly the keys in ``USER_DEFAULTS_KEYS``.
    Pure: no widget reads, no I/O.
    """
    snapshot: dict[str, Any] = {}
    for key in USER_DEFAULTS_KEYS:
        snapshot[key] = widgets[key]
    return snapshot


def write_cli_config_yaml(
    out_dir: Path,
    threshold: float,
    min_silence: float,
    margin: float,
    *,
    filename: str = "stream2video_cli_config.yaml",
) -> Path | None:
    """Write a tiny YAML config holding the three slider values so a
    "Copy CLI command" paste picks them up via ``-c``.

    The CLI's ``-c`` / ``--config`` flag accepts a YAML file whose
    keys mirror the GUI's sliders; without it, the copied command's
    ``threshold`` / ``min_silence`` / ``margin`` would silently fall
    back to the CLI defaults (see ``CONFIG_DEFAULTS``) and the user's
    slider values would be lost. This function writes *just* those
    three keys — adding more would clutter the (visible) command line
    and isn't necessary (the rest is already passed as explicit flags).

    Pure: returns the path written or ``None`` on any filesystem error
    (the GUI logs the warning; the caller decides whether to keep
    ``--config`` in the command). Atomicity: not strictly required for
    a tiny CLI config — a partial write is acceptable because the
    caller tolerates ``None`` (it just omits the flag).
    """
    config_path = (out_dir / filename).resolve()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        config_yaml = f"threshold: {threshold}\nmin_silence: {min_silence}\nmargin: {margin}\n"
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(config_yaml)
        return config_path
    except OSError:
        # The caller logs and continues without the ``--config`` flag
        # (the command still runs, just with CLI defaults for the
        # slider-only values).
        return None
