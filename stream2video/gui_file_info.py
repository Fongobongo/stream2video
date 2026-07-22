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
        if not path.exists():
            return
        self.lbl_file.configure(text=f"File: {path.name}")
        size = path.stat().st_size
        self.lbl_size.configure(text=f"Size: {fmt_size(size)}")
        self.lbl_duration.configure(text="Duration: ...")

        def _get_dur() -> None:
            dur = get_video_duration(path)
            if dur:
                self.after(
                    0,
                    lambda d=dur: self.lbl_duration.configure(text=f"Duration: {fmt_time(d)}"),
                )

        threading.Thread(target=_get_dur, daemon=True).start()
