"""RecentProjectsMixin — Recent Projects sub-panel (Этап 10 mixin).

Extracted from ``Stream2VideoGUI``: render the recent-projects rows,
add a project to the list (called from the pipeline worker thread via
the adapter), delete a project (with confirmation), open a project in
the platform's file manager.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk

from stream2video.gui_platform import dir_size_mb, open_in_file_manager
from stream2video.gui_widgets import Tooltip as _Tooltip
from stream2video.paths import (
    RECENT_NAME_MAX,
    add_recent_project,
    prune_recent_projects,
    truncate_recent_name,
)

_logger = logging.getLogger("stream2video.gui")


class RecentProjectsMixin:
    """Build + mutate the Recent Projects list in the left panel."""

    def _render_recent_projects(self) -> None:
        """Rebuild the Recent Projects sub-section from self.config.

        Prunes entries whose directory no longer exists. Rows have a
        label (project name + tooltip with full path) and a trash button
        that asks for confirmation before deleting the whole subdirectory.
        """
        for child in self.recent_frame.winfo_children():
            child.destroy()
        pruned = prune_recent_projects(self.config.get("recent_projects", []))
        if pruned != self.config.get("recent_projects", []):
            self.config["recent_projects"] = pruned
        if not pruned:
            ctk.CTkLabel(
                self.recent_frame,
                text="(no recent projects)",
                text_color=("gray50", "gray60"),
                anchor="w",
            ).pack(fill="x", padx=5, pady=2)
            return
        for path_str in pruned:
            row = ctk.CTkFrame(self.recent_frame, fg_color="transparent")
            row.pack(fill="x", padx=2, pady=1)
            display = Path(path_str).name or path_str
            lbl = ctk.CTkLabel(
                row,
                text=truncate_recent_name(display, RECENT_NAME_MAX),
                anchor="w",
                cursor="hand2",
            )
            lbl.pack(side="left", fill="x", expand=True, padx=(3, 2))
            lbl.bind(
                "<Button-1>",
                lambda e, p=path_str: self._open_in_explorer(p),
            )
            _Tooltip(lbl, path_str)
            del_btn = ctk.CTkButton(
                row,
                text="X",
                width=22,
                height=22,
                fg_color=("gray70", "gray30"),
                hover_color=("#c0392b", "#922B21"),
                text_color=("gray10", "gray90"),
                command=lambda p=path_str: self._delete_recent_project(p),
            )
            del_btn.pack(side="right", padx=(0, 3))

    def _add_to_recent_projects(self, project_path: Path | str) -> None:
        """Add or move ``project_path`` to the top of the recent list (max 5).

        Persists to ``settings.json`` eagerly so a GUI crash doesn't
        lose the list. Called from the pipeline worker thread; the
        settings write + the recent-projects panel re-render are
        scheduled on the Tk main loop via ``_tk_after`` so we never touch
        Tk widgets or do file I/O cross-thread.
        """
        if not project_path:
            return
        path_str = str(project_path)
        self.config["recent_projects"] = add_recent_project(
            self.config.get("recent_projects", []),
            path_str,
        )
        self._tk_after(0, self._render_recent_projects)

        def _persist():
            try:
                self._save_settings()
            except Exception as e:
                _logger.warning("Failed to save settings after adding recent project: %s", e)

        self._tk_after(0, _persist)

    def _delete_recent_project(self, path_str: str) -> None:
        """Confirm with the user, then recursively delete the project dir."""
        if self.running:
            self._log("Cannot delete a project while pipeline is running")
            return
        path = Path(path_str)
        if not path.is_dir():
            self._log(f"Project no longer exists, dropping from list: {path_str}")
            self.config["recent_projects"] = [
                p for p in self.config.get("recent_projects", []) if p != path_str
            ]
            self._render_recent_projects()
            return
        size_mb = dir_size_mb(path)
        msg = (
            f"Delete project '{path.name}' and ALL its contents?\n\n"
            f"Location: {path}\n"
            f"Approx size: {size_mb:.1f} MB\n\n"
            f"This will permanently remove the source video (if downloaded), "
            f"the compressed output, the audio cache, the silence cache, "
            f"and the log file.\n\n"
            f"This cannot be undone."
        )
        ok = messagebox.askyesno("Delete project?", msg, icon="warning")
        if not ok:
            return
        try:
            shutil.rmtree(path)
            self._log(f"Deleted project: {path}")
        except OSError as e:
            self._log(f"[ERROR] Failed to delete {path}: {e}")
            messagebox.showerror("Delete failed", f"Could not delete {path}:\n{e}")
            return
        self.config["recent_projects"] = [
            p for p in self.config.get("recent_projects", []) if p != path_str
        ]
        self._render_recent_projects()

    def _dir_size_mb(self, path: Path) -> float:
        """Delegates to gui_platform.dir_size_mb (pure, testable)."""
        return dir_size_mb(path)

    def _open_in_explorer(self, path_str: str) -> None:
        """Open the project directory in the platform's file manager."""
        path = Path(path_str)
        try:
            open_in_file_manager(path)
        except FileNotFoundError:
            messagebox.showwarning(
                "Folder not found",
                f"Directory no longer exists:\n{path_str}",
            )
            self.config["recent_projects"] = [
                p for p in self.config.get("recent_projects", []) if p != path_str
            ]
            self._render_recent_projects()
        except OSError as e:
            self._log(f"[ERROR] Could not open {path}: {e}")
