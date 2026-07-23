"""Standalone widget helpers extracted from ``gui.py`` (Этап 10 incremental).

The GUI class in ``gui.py`` is a 2900-line monolith. Reducing it in one
big-bang refactor would be high-risk without comprehensive GUI tests, so
we extract self-contained widget helpers here one at a time, keeping
``gui.py`` import-compatible throughout.

Currently exported:
  * ``Tooltip`` — the hover-tooltip class previously known as ``_Tooltip``.
    Renamed to drop the leading underscore because it's now a public
    helper in its own module.

Future candidates (kept in ``gui.py`` until they get a clean extraction):
  * ``QueueHandler`` (logging.Handler → log queue for the GUI textbox).
  * Recent-projects rendering (reads/writes recent_projects list, builds
    CTkFrame rows — depends on paths.py + GUI state, slightly trickier).
  * Settings I/O (currently inline in ``_save_settings`` / ``_load_settings``).
"""

from __future__ import annotations

from typing import Any

import customtkinter as ctk


class Tooltip:
    """A hover tooltip for any tkinter/ctk widget.

    Shows the ``text`` after a 400ms hover; hides 200ms after the cursor
    leaves the widget or the tooltip itself. The hide is delayed so a
    user flicking the cursor onto the tooltip (to copy a value, for
    instance) doesn't make it disappear mid-action.
    """

    _SHOW_DELAY_MS = 400
    _HIDE_DELAY_MS = 200

    def __init__(self, widget: Any, text: str) -> None:
        self.widget = widget
        self.text = text
        self._tip: ctk.CTkToplevel | None = None
        self._after_id: str | None = None
        widget.bind("<Enter>", self._schedule_show, add="+")
        widget.bind("<Leave>", self._schedule_hide, add="+")

    def _schedule_show(self, event: Any = None) -> None:
        self._cancel_scheduled()
        self._after_id = self.widget.after(self._SHOW_DELAY_MS, self._show)

    def _schedule_hide(self, event: Any = None) -> None:
        self._cancel_scheduled()
        if self._tip:
            self.widget.after(self._HIDE_DELAY_MS, self._hide)

    def _cancel_scheduled(self) -> None:
        if self._after_id:
            self.widget.after_cancel(self._after_id)
            self._after_id = None

    def _show(self, event: Any = None) -> None:
        self._after_id = None
        if self._tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self._tip = tw = ctk.CTkToplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tw.attributes("-topmost", True)
        ctk.CTkLabel(
            tw,
            text=self.text,
            wraplength=320,
            fg_color=("gray85", "gray15"),
            text_color=("gray10", "gray90"),
            corner_radius=4,
            padx=8,
            pady=4,
        ).pack()
        tw.bind("<Enter>", self._cancel_scheduled, add="+")
        tw.bind("<Leave>", self._schedule_hide, add="+")

    def _hide(self, event: Any = None) -> None:
        tw = self._tip
        self._tip = None
        if tw:
            tw.destroy()


# Back-compat alias so existing imports of ``_Tooltip`` (inside gui.py
# and any downstream code) keep working during the incremental refactor.
_Tooltip = Tooltip
