"""AdvancedSettingsMixin — the "Advanced" tunables section.

The 18 CLI-only options (software_fallback, x264_preset,
encoder_threads, output_fps, memory_*, timeouts, batch_chunk_size,
min_part_bytes) previously had no GUI surface — the audit found the
GUI silently ran with config values the user couldn't see or change
(and a copied CLI command that disagreed with the run). This mixin
builds one compact section in the Controls column, reads the widgets
back through the pure ``settings_io.parse_advanced_widgets`` helper
(so parsing is unit-tested without Tk), and re-applies a config dict
to the widgets for "Restore defaults".

The widget layout is fully declarative: ``ADVANCED_WIDGET_SPECS``
dictates the label, the combo choices vs entry parsing, and the
tooltip, so a future tunable is a one-line table addition.
"""

from __future__ import annotations

from typing import Any

import customtkinter as ctk

from stream2video.config import PRESETS
from stream2video.gui_widgets import Tooltip as _Tooltip
from stream2video.settings_io import (
    ADVANCED_WIDGET_NAMES,
    ADVANCED_WIDGET_SPECS,
    parse_advanced_widgets,
    validate_advanced_widgets,
)


class AdvancedSettingsMixin:
    """Builds / reads / restores the Advanced tunables section."""

    def _build_advanced_section(self, parent: Any) -> None:
        """Build the Advanced section inside the Controls scroll frame."""
        ctk.CTkFrame(parent, height=2, fg_color=("gray70", "gray30")).pack(fill="x", padx=5, pady=4)
        ctk.CTkLabel(parent, text="Advanced", anchor="w", font=("", 13, "bold")).pack(
            fill="x", padx=5, pady=(3, 1)
        )
        grid = ctk.CTkFrame(parent, fg_color="transparent")
        grid.pack(fill="x", padx=5, pady=1)
        grid.grid_columnconfigure(1, weight=1)
        grid.grid_columnconfigure(3, weight=1)

        for index, (key, spec) in enumerate(ADVANCED_WIDGET_SPECS.items()):
            row, col = divmod(index, 2)
            col *= 2
            ctk.CTkLabel(grid, text=spec["label"], width=105, anchor="w").grid(
                row=row, column=col, sticky="w", padx=(0, 4), pady=(0, 2)
            )
            name = ADVANCED_WIDGET_NAMES[key]
            if spec["kind"] == "combo":
                widget = ctk.CTkComboBox(
                    grid, values=list(spec["valid"]), state="readonly", width=120
                )
            else:
                widget = ctk.CTkEntry(grid, width=120)
            setattr(self, name, widget)
            widget.grid(row=row, column=col + 1, sticky="w", padx=(0, 8), pady=(0, 2))
            tooltip = spec.get("tooltip")
            if tooltip:
                _Tooltip(widget, tooltip)

        self._set_advanced_widget_values(self.settings)

    def _read_advanced_widget_values(self) -> dict[str, Any]:
        """Read the Advanced widgets (Tk reads are main-thread-only).

        Returns typed values: combos pass through, entries are parsed
        via the pure ``parse_advanced_widgets`` helper (unparseable
        text falls back to the current settings value).
        """
        return parse_advanced_widgets(self._raw_advanced_widget_values(), current=self.settings)

    def _raw_advanced_widget_values(self) -> dict[str, str]:
        """Read every Advanced widget's raw string (Tk main thread only).

        Shared by the typed parse (:meth:`_read_advanced_widget_values`)
        and the validation gate (:meth:`_advanced_widget_errors`) so both
        read the exact same strings the user sees — the gate must not
        disagree with the parse about what "invalid" means.
        """
        return {
            key: getattr(self, ADVANCED_WIDGET_NAMES[key]).get() for key in ADVANCED_WIDGET_SPECS
        }

    def _advanced_widget_errors(self) -> dict[str, str]:
        """Per-key error strings for invalid Advanced widget content.

        Empty dict = all widgets parse and are in range. Mirrors the CLI
        resolver's validation (audit P2): the same ``abc`` entry text that
        the CLI rejects with "Invalid X (use ...)" also blocks Start /
        Copy CLI command here, so the GUI can no longer run with a value
        different from what its own field shows.
        """
        return validate_advanced_widgets(self._raw_advanced_widget_values())

    def _set_advanced_widget_values(self, values: dict[str, Any]) -> None:
        """Push a config dict (e.g. restored defaults) into the widgets."""
        for key, spec in ADVANCED_WIDGET_SPECS.items():
            widget = getattr(self, ADVANCED_WIDGET_NAMES[key])
            value = values.get(key)
            if value is None:
                continue
            if spec["kind"] == "combo":
                widget.set(str(value))
            else:
                widget.delete(0, "end")
                widget.insert(0, str(value))

    def _on_preset_change(self, preset: str) -> None:
        """Sync the preset-managed widgets when the resource preset changes.

        Previously the preset was applied to ``run_config`` at Start and
        then immediately overwritten by the widget snapshot — the preset
        was a no-op (audit P1). Now the preset's tunables are pushed into
        the widgets that manage them (and into ``self.settings``) at
        selection time, so the widgets SHOW what the run will use and the
        widget snapshot is exactly what runs. ``balanced`` changes nothing
        (identity preset), so a user's explicit choices survive a
        round-trip through 'balanced'.

        The preset is applied EXACTLY once, at selection: any later hand
        tweak to a managed widget counts as the user's explicit override
        and persists (``_save_settings`` writes the widget values). It is
        deliberately NOT re-applied at startup — a previous startup sync
        destroyed such saved overrides on restart (audit round 13 P1).
        """
        if preset not in PRESETS:
            return
        overrides = PRESETS[preset]
        self.settings.update(overrides)
        self.settings["preset"] = preset
        # Programmatic callers (``_restore_defaults``) land here without
        # firing the combobox command, so keep the combo in sync too.
        # ``.set()`` on a readonly combo doesn't invoke ``command`` — no
        # recursion.
        if self.combo_preset.get() != preset:
            self.combo_preset.set(preset)
        if not overrides:
            return
        for key, value in overrides.items():
            spec = ADVANCED_WIDGET_SPECS.get(key)
            if spec is not None:
                widget = getattr(self, ADVANCED_WIDGET_NAMES[key])
                if spec["kind"] == "combo":
                    widget.set(str(value))
                else:
                    widget.delete(0, "end")
                    widget.insert(0, str(value))
            elif key == "x264_low_memory":
                self._set_checkbox(self.chk_x264_low_memory, bool(value))
            elif key == "low_process_priority":
                self._set_checkbox(self.chk_low_process_priority, bool(value))
        # Persist right away (parity with _on_proxy_toggle): the synced
        # widget values must survive a restart even when the user never
        # clicks another save point.
        self._save_settings()
