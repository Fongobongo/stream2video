"""Channel picker dialog (GUI): a checkbox table over a resolved listing.

When the GUI's input is a listing URL (Twitch channel, YouTube channel or
playlist), the ordinary single-video flow cannot run — the pipeline
downloads ONE video. This dialog is the GUI's answer to the CLI's
``--channel-pick`` prompt:

* the listing is resolved on a background thread (the same
  ``resolve_channel_vods`` flat-playlist pass the CLI uses — metadata
  only, seconds) and the entries fill a scrollable table;
* every row carries a checkbox (the original "check the videos you
  want" ask), plus the same columns the CLI table shows — duration,
  views, upload date, title;
* column headers sort the table (click again to reverse);
* Start hands the CHECKED entries' URLs back to the GUI, which runs
  them as a batch through the same worker loop the CLI uses (per-entry
  error isolation, batch progress).

The dialog is modal relative to the picker action: it does not block the
main window's event loop (Tk ``grab`` only), and cancel simply closes it.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from typing import Any

import customtkinter as ctk

from stream2video.channel import (
    ChannelImportError,
    ChannelVod,
    sort_channel_vods,
)

logger = logging.getLogger(__name__)

# Column layout: (key, header label, width chars). ``key`` indexes the
# row dicts; "checked" is the checkbox state, handled separately.
_COLUMNS: list[tuple[str, str, int]] = [
    ("duration", "Duration", 9),
    ("views", "Views", 11),
    ("date", "Date", 11),
    ("title", "Title", 60),
]


class ChannelPickerDialog(ctk.CTkToplevel):
    """A modal checkbox table over a channel/playlist listing.

    The caller passes a ``listing_factory``: a zero-arg callable that
    resolves the listing (already bound with limit/type/filter/proxy) —
    typically ``functools.partial(resolve_channel_vods, ...)``. It runs
    on a background thread; the dialog shows a busy state until the
    entries arrive, an error message if the listing fails, and the
    table + Start/Cancel buttons on success.
    """

    def __init__(
        self,
        parent: Any,
        listing_factory: Callable[[], list[ChannelVod]],
        on_start: Callable[[list[ChannelVod]], None],
        initial_sort: str = "date",
    ) -> None:
        super().__init__(parent)
        self.title("Pick videos to compress")
        self.geometry("980x560")
        self.minsize(720, 420)
        self.transient(parent)
        self.grab_set()

        self._listing_factory = listing_factory
        self._on_start = on_start
        self._sort_key = initial_sort if initial_sort in {"date", "duration", "views"} else "date"
        self._sort_desc = True
        self._rows: list[dict[str, Any]] = []  # {vod, checked(BooleanVar), widgets}
        self._started = False
        # Resolution hand-off: the background thread NEVER touches a
        # widget (Tk is not thread-safe, and ``after`` called FROM a
        # thread is unreliable on some platforms) — it only puts its
        # result on this queue; ``_poll_listing`` (scheduled from the
        # main loop) drains it. Same pattern as the GUI's LogQueuePoller.
        self._listing_q: queue.Queue = queue.Queue()
        self._poll_handle: str | None = None

        # ---- header: status + sort control -----------------------------
        self._header = ctk.CTkFrame(self, fg_color="transparent")
        self._header.pack(fill="x", padx=12, pady=(12, 0))
        self._lbl_status = ctk.CTkLabel(
            self._header, text="Resolving listing…", font=ctk.CTkFont(size=13)
        )
        self._lbl_status.pack(side="left")

        # ---- column headers (click to sort) ----------------------------
        self._col_header = ctk.CTkFrame(self, fg_color="transparent")
        self._col_header.pack(fill="x", padx=12, pady=(8, 0))

        # checkbox column spacer keeps the grid aligned with rows
        ctk.CTkLabel(self._col_header, text="", width=28).grid(row=0, column=0, padx=(0, 4))
        for col_i, (key, label, width) in enumerate(_COLUMNS, start=1):
            btn = ctk.CTkButton(
                self._col_header,
                text=label,
                width=width * 10,
                anchor="w",
                fg_color="transparent",
                text_color=("gray10", "gray90"),
                hover_color=("gray80", "gray25"),
                command=lambda k=key: self._sort_by(k),
            )
            btn.grid(row=0, column=col_i, padx=4, sticky="ew")
            self._col_header.columnconfigure(col_i, weight=1 if key == "title" else 0)

        # ---- the table (scrollable frame of rows) ----------------------
        self._table = ctk.CTkScrollableFrame(self)
        self._table.pack(fill="both", expand=True, padx=12, pady=(8, 8))

        # ---- footer: select-all + start/cancel --------------------------
        self._footer = ctk.CTkFrame(self, fg_color="transparent")
        self._footer.pack(fill="x", padx=12, pady=(0, 12))

        self._var_all = ctk.BooleanVar(value=False)
        self._chk_all = ctk.CTkCheckBox(
            self._footer,
            text="Select all",
            variable=self._var_all,
            command=self._toggle_all,
            width=110,
        )
        self._chk_all.pack(side="left")

        self._lbl_count = ctk.CTkLabel(self._footer, text="")
        self._lbl_count.pack(side="left", padx=(16, 0))

        self._btn_cancel = ctk.CTkButton(
            self._footer, text="Cancel", width=90, command=self.destroy
        )
        self._btn_cancel.pack(side="right")

        self._btn_start = ctk.CTkButton(
            self._footer,
            text="Start",
            width=110,
            state="disabled",
            command=self._start,
        )
        self._btn_start.pack(side="right", padx=(0, 8))

        # Resolve on a background thread; the result crosses back via
        # ``_listing_q`` and lands on the main loop through
        # ``_poll_listing`` (see the queue's docstring above).
        threading.Thread(target=self._resolve, daemon=True).start()
        self._poll_handle = self.after(60, self._poll_listing)
        # Bring ourselves to front (Toplevel can start behind on some WMs).
        self.after(50, lambda: self.focus_force())

    # ------------------------------------------------------------------
    # Listing resolution
    # ------------------------------------------------------------------

    def _resolve(self) -> None:
        """Worker-thread half: resolve the listing, queue the result.

        Widget calls are strictly main-loop-only (see ``_listing_q``);
        this half only runs yt-dlp and puts ``(vods | error)`` on the
        queue.
        """
        try:
            vods = self._listing_factory()
        except ChannelImportError as e:
            self._listing_q.put(("error", str(e)))
            return
        except Exception as e:  # pragma: no cover - defensive
            logger.exception("Channel listing failed in the picker")
            self._listing_q.put(("error", str(e)))
            return
        self._listing_q.put(("ok", vods))

    def _poll_listing(self) -> None:
        """Main-loop half: drain the resolution queue (rescheduled every
        60 ms until the result lands; cancelled by destroy)."""
        try:
            kind, payload = self._listing_q.get_nowait()
        except queue.Empty:
            self._poll_handle = self.after(60, self._poll_listing)
            return
        if kind == "error":
            self._show_error(payload)
            return
        self._fill(payload)

    def _show_error(self, message: str) -> None:
        self._lbl_status.configure(text="Listing failed", text_color="#d9534f")
        self._lbl_count.configure(text=message[:220])
        self._btn_cancel.configure(text="Close")

    # ------------------------------------------------------------------
    # Table
    # ------------------------------------------------------------------

    def _fill(self, vods: list[ChannelVod]) -> None:
        vods = sort_channel_vods(vods, self._sort_key)
        if not self._sort_desc:
            vods = list(reversed(vods))
        self._lbl_status.configure(text=f"{len(vods)} entries — check the ones to compress")
        for vod in vods:
            self._add_row(vod)
        # Start is enabled by _update_count ONLY when something is
        # checked (an enabled-but-empty click would be a no-op guard —
        # better to reflect the truth in the button state).
        self._update_count()
        if not vods:
            self._lbl_count.configure(text="The listing is empty.")

    def _add_row(self, vod: ChannelVod) -> None:
        row = ctk.CTkFrame(self._table, fg_color="transparent")
        row.pack(fill="x", pady=1)

        checked = ctk.BooleanVar(value=False, master=row)
        chk = ctk.CTkCheckBox(row, text="", variable=checked, width=28, command=self._update_count)
        chk.grid(row=0, column=0, padx=(0, 4))

        views = f"{vod.view_count:,}" if vod.view_count is not None else "?"
        cells = [
            vod.duration_hm(),
            views,
            vod.date_label(),
            vod.title or vod.url,
        ]
        widgets = [chk]
        for col_i, text in enumerate(cells, start=1):
            lbl = ctk.CTkLabel(
                row,
                text=text,
                anchor="w",
                justify="left",
            )
            lbl.grid(row=0, column=col_i, padx=4, sticky="ew")
            # Title column stretches; the fixed-width columns don't.
            row.columnconfigure(col_i, weight=1 if col_i == len(cells) else 0)
            widgets.append(lbl)

        self._rows.append({"vod": vod, "checked": checked, "frame": row, "widgets": widgets})

    # ------------------------------------------------------------------
    # Sorting
    # ------------------------------------------------------------------

    def _sort_by(self, key: str) -> None:
        if key == self._sort_key:
            # Same column: flip direction.
            self._sort_desc = not self._sort_desc
        else:
            self._sort_key = key
            self._sort_desc = True
        self._rebuild()

    def _rebuild(self) -> None:
        """Re-fill the table in the new sort order (simplest correct
        approach: destroy and re-add — listings are tens of rows, not
        thousands)."""
        vods = [r["vod"] for r in self._rows]
        for r in self._rows:
            r["checked"].set(False)
        for w in self._table.winfo_children():
            w.destroy()
        self._rows.clear()
        if vods:
            self._fill(vods)
            self._update_count()

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def _toggle_all(self) -> None:
        want = self._var_all.get()
        for r in self._rows:
            r["checked"].set(want)
        self._update_count()

    def _update_count(self) -> None:
        n = sum(1 for r in self._rows if r["checked"].get())
        self._lbl_count.configure(text=f"{n} selected")
        self._btn_start.configure(state="normal" if n else "disabled")

    # ------------------------------------------------------------------
    # Start
    # ------------------------------------------------------------------

    def _start(self) -> None:
        picked = [r["vod"] for r in self._rows if r["checked"].get()]
        if not picked:
            return
        self._started = True
        self.destroy()
        self._on_start(picked)


def show_channel_picker(
    parent: Any,
    listing_factory: Callable[[], list[ChannelVod]],
    on_start: Callable[[list[ChannelVod]], None],
    initial_sort: str = "date",
) -> ChannelPickerDialog:
    """Open the picker dialog (see ``ChannelPickerDialog``).

    ``listing_factory`` resolves the listing on a background thread;
    ``on_start`` receives the CHECKED ``ChannelVod`` entries when the
    user presses Start (not called on cancel/close).
    """
    return ChannelPickerDialog(parent, listing_factory, on_start, initial_sort)
