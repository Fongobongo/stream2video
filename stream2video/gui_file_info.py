"""FileInfoMixin — Info panel population (Этап 10 mixin).

Extracted from ``Stream2VideoGUI``: the ``_update_file_info`` method
that updates ``File:`` / ``Size:`` / ``Duration:`` labels in the left
panel. Duration is read on a background thread because ``ffprobe`` can
take a second on large files; the label is set to ``...`` first and the
result is marshaled back onto the Tk main loop.
"""

from __future__ import annotations

import threading
from pathlib import Path

from stream2video.formatters import fmt_size, fmt_time
from stream2video.utils import get_video_duration


class FileInfoMixin:
    """Populates the left-panel Info labels from a source path."""

    def _update_file_info(self, path: Path) -> None:
        # Generation token (C18 audit): a slow ffprobe from an OLD file
        # must not overwrite the labels after the user picked a NEW one.
        # Each call bumps the token; the async callback only writes when
        # its token is still current.
        token = getattr(self, "_file_info_token", 0) + 1
        self._file_info_token = token
        # TOCTOU-tolerant: drop the exists() gate and stat directly. A file
        # deleted (or a network share dropped) between exists() and stat()
        # would otherwise raise FileNotFoundError on the GUI main thread
        # via ``_browse_from``'s chain. A slow network path also belongs
        # off the main loop — keep the existing thread below.
        try:
            size = path.stat().st_size
        except OSError:
            return
        self.lbl_file.configure(text=f"File: {path.name}")
        self.lbl_size.configure(text=f"Size: {fmt_size(size)}")
        self.lbl_duration.configure(text="Duration: ...")

        def _get_dur() -> None:
            dur = get_video_duration(path)
            if not dur:
                return
            # P1.10: tkinter is not thread-safe. ``self.after`` from a
            # worker thread can race with the main loop's widget
            # teardown; ``_tk_after`` is the project's only safe entry
            # point for cross-thread widget writes.
            self._tk_after(
                0,
                lambda d=dur, tok=token: (
                    self.lbl_duration.configure(text=f"Duration: {fmt_time(d)}")
                    if getattr(self, "_file_info_token", -1) == tok
                    else None
                ),
            )

        threading.Thread(target=_get_dur, daemon=True).start()
