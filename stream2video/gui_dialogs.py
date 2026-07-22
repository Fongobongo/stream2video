"""DialogsMixin — file open / directory picker dialogs (Этап 10 mixin).

Extracted from ``Stream2VideoGUI``: the ``_browse_input`` /
``_browse_output`` handlers that pop a ``tkinter.filedialog`` chooser
and write the picked path back into the matching entry widget.
"""

from __future__ import annotations

from pathlib import Path
from tkinter import filedialog


class DialogsMixin:
    """File/directory picker dialogs.

    Thin wrapper around ``tkinter.filedialog`` — the chooser returns a
    path; the mixin writes it into the matching ``CTkEntry`` widget
    (built by :class:`MainWindowBuildMixin`). The input browse also
    kicks off the file-info probe so the Info panel reflects the new
    source immediately.
    """

    def _browse_input(self) -> None:
        path = filedialog.askopenfilename(
            title="Select Video File",
            filetypes=[
                ("Video files", "*.mp4 *.mkv *.avi *.mov *.webm *.ts"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self.entry_input.delete(0, "end")
            self.entry_input.insert(0, path)
            self._update_file_info(Path(path))

    def _browse_output(self) -> None:
        path = filedialog.askdirectory(title="Select Output Directory")
        if path:
            self.entry_output.delete(0, "end")
            self.entry_output.insert(0, path)
