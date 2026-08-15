"""SlidersMixin — threshold / min_silence / margin slider rows.

Extracted from ``Stream2VideoGUI``: ``_add_slider`` (builds a labelled
slider row with editable value field + Default button), ``_reset_default``
(the Default button click), and ``_sync_slider_entries`` (read entries →
config dict before a pipeline run / save).
"""

from __future__ import annotations

from typing import Any

import customtkinter as ctk

from stream2video.config import CONFIG_DEFAULTS
from stream2video.gui_widgets import Tooltip as _Tooltip
from stream2video.slider_widgets import (
    format_slider_entry_value,
    parse_slider_entry_value,
    sync_slider_entries,
)


class SlidersMixin:
    """Builds and syncs the silence-detection slider rows."""

    def _add_slider(
        self,
        parent: Any,
        label: str,
        key: str,
        min_v: float,
        max_v: float,
        current: float,
        tooltip: str = "",
    ) -> None:
        """Add a labelled slider row with editable value field and default button."""
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=6, pady=(2, 0))

        lbl = ctk.CTkLabel(row, text=label, width=95, anchor="w")
        lbl.pack(side="left")
        if tooltip:
            _Tooltip(lbl, tooltip)

        slider = ctk.CTkSlider(
            row, from_=min_v, to=max_v, number_of_steps=round((max_v - min_v) * 10), width=110
        )
        slider.set(current)
        slider.pack(side="left", padx=(0, 4))

        entry_val = ctk.CTkEntry(row, width=52, justify="right")
        entry_val.insert(0, format_slider_entry_value(current))
        entry_val.pack(side="left")

        btn_default = ctk.CTkButton(
            row,
            text="D",
            width=20,
            height=20,
            font=("", 9, "bold"),
            command=lambda k=key, d=CONFIG_DEFAULTS.get(key, current), sv=slider, ev=entry_val: (
                self._reset_default(d, sv, ev, k)
            ),
        )
        btn_default.pack(side="left", padx=(2, 0))

        slider._entry_val = entry_val
        setattr(self, f"_slider_{key}", slider)

        def on_change(v: Any, k: str = key, ev: Any = entry_val) -> None:
            ev.delete(0, "end")
            ev.insert(0, format_slider_entry_value(float(v)))
            self.settings[k] = round(float(v), 1)
            # The threshold drives a line in the waveform preview; re-render
            # via a debounced timer so a slider drag does not pile up
            # render calls. Other sliders (min_silence, margin) only
            # affect future pipeline runs, so they don't trigger a
            # re-render here.
            if k == "threshold":
                self._schedule_waveform_threshold_re_render()

        def on_entry_confirm(
            event: Any = None, sv: Any = slider, mn: float = min_v, mx: float = max_v, k: str = key
        ) -> None:
            # Pure parse handled in slider_widgets; the closure just
            # applies the result to the slider + entry + config and
            # re-renders the waveform when threshold changes.
            raw = entry_val.get()
            val = parse_slider_entry_value(raw, mn, mx)
            if val is None:
                # Revert entry to the slider's current value (the
                # legacy behavior on parse failure).
                entry_val.delete(0, "end")
                entry_val.insert(0, format_slider_entry_value(float(sv.get())))
                return
            sv.set(val)
            self.settings[k] = val
            entry_val.delete(0, "end")
            entry_val.insert(0, format_slider_entry_value(val))
            if k == "threshold":
                self._schedule_waveform_threshold_re_render()

        entry_val.bind("<Return>", on_entry_confirm)
        entry_val.bind("<FocusOut>", on_entry_confirm)
        slider.configure(command=on_change)
        # Remember the real min/max so ``_sync_slider_entries`` can clamp
        # a typed-but-never-confirmed value (e.g. the user hits "Start"
        # before FocusOut fires) instead of leaking an out-of-range raw
        # value into the pipeline config. ``setdefault`` keeps this robust
        # when the mixin is composed into a test double without __init__.
        bounds_map: dict[str, tuple[float, float]] | None = getattr(self, "_slider_bounds", None)
        if bounds_map is None:
            bounds_map = {}
            self._slider_bounds: dict[str, tuple[float, float]] = bounds_map
        bounds_map[key] = (min_v, max_v)

    def _reset_default(self, default: float, slider: Any, entry: Any, key: str) -> None:
        slider.set(default)
        entry.delete(0, "end")
        entry.insert(0, format_slider_entry_value(default))
        self.settings[key] = default

    def _sync_slider_entries(self) -> None:
        # Build the entries dict the pure
        # :func:`stream2video.slider_widgets.sync_slider_entries` expects,
        # keyed by the slider panel's three keys. The GUI owns the
        # widget reads; the helper owns the parse + clamp + round. The
        # clamp matters when the user typed an out-of-range value and
        # clicked Start before the entry's FocusOut handler could
        # confirm/revert it — without it the pipeline would run with a
        # config that doesn't match what the sliders show.
        bounds: dict[str, tuple[float, float]] = getattr(self, "_slider_bounds", {})
        entries: dict[str, str] = {}
        for key in ("threshold", "min_silence", "margin"):
            slider = getattr(self, f"_slider_{key}", None)
            if slider and hasattr(slider, "_entry_val"):
                entries[key] = slider._entry_val.get()
                if key not in bounds:
                    # Fallback for tests that build sliders without
                    # calling ``_add_slider``: use the widget range.
                    bounds[key] = (float(slider.cget("from_")), float(slider.cget("to")))
        updates = sync_slider_entries(entries, bounds)
        for key, val in updates.items():
            self.settings[key] = val
