"""RecentProjectsMixin — Recent Projects sub-panel.

Extracted from ``Stream2VideoGUI``: render the recent-projects rows,
add a project to the list (called from the pipeline worker thread via
the adapter), delete a project (with confirmation), open a project in
the platform's file manager.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk

from stream2video.gui_platform import dir_size_mb, open_in_file_manager
from stream2video.gui_widgets import Tooltip as _Tooltip
from stream2video.paths import (
    RECENT_NAME_MAX,
    add_recent_project,
    is_marked_project_dir,
    prune_recent_projects,
    truncate_recent_name,
    validate_project_delete,
)

_logger = logging.getLogger("stream2video.gui")


class RecentProjectsMixin:
    """Build + mutate the Recent Projects list in the left panel."""

    def _render_recent_projects(self) -> None:
        """Rebuild the Recent Projects sub-section from self.settings.

        Prunes entries whose directory no longer exists. Rows have a
        label (project name + tooltip with full path) and a trailing
        button:

          * marked project dirs (per-video dirs created by this app):
            a trash "X" that confirms before recursively deleting the
            whole subdirectory;
          * anything else (flat-mode output dirs, foreign dirs): a
            remove-from-recents button only — audit #7: in flat mode the
            entry is the shared output dir, which this app never marks
            as a project dir, so a destructive delete is logically
            impossible and the UI must not promise it.
        """
        for child in self.recent_frame.winfo_children():
            # Each row owns a Tooltip with pending Tk ``after`` timers —
            # destroy it first so those timers don't fire against a
            # half-destroyed widget pane (TclError) or leak bindings.
            for widget in child.winfo_children():
                tooltip = getattr(widget, "_tooltip_ref", None)
                if tooltip is not None:
                    tooltip.destroy()
            child.destroy()
        pruned = prune_recent_projects(self.settings.get("recent_projects", []))
        if pruned != self.settings.get("recent_projects", []):
            self.settings["recent_projects"] = pruned
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
            # Keep a reachable reference so the row-teardown above can
            # cancel the tooltip's timers before the widget goes away.
            lbl._tooltip_ref = _Tooltip(lbl, path_str)
            try:
                deletable = is_marked_project_dir(Path(path_str))
            except OSError:
                deletable = False
            if deletable:
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
                del_btn._tooltip_ref = _Tooltip(del_btn, "Delete this project and all its files")
            else:
                # Flat mode (or a hand-edited entry): deletion of the
                # directory is not possible — only remove the list entry.
                rem_btn = ctk.CTkButton(
                    row,
                    text="-",
                    width=22,
                    height=22,
                    fg_color=("gray70", "gray30"),
                    hover_color=("#c0392b", "#922B21"),
                    text_color=("gray10", "gray90"),
                    command=lambda p=path_str: self._remove_recent_entry(p),
                )
                rem_btn.pack(side="right", padx=(0, 3))
                rem_btn._tooltip_ref = _Tooltip(
                    rem_btn, "Remove from Recent Projects (no files are deleted)"
                )

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

        def _apply_and_persist() -> None:
            # Runs on the Tk main loop: mutate config, re-render, and
            # persist from one place so a concurrent ``_save_settings``
            # can't serialize a half-updated list (the old code mutated
            # ``self.settings`` on the pipeline worker thread — a GIL-atomic
            # rebinding, but visible to a concurrent main-thread save as
            # a lost update on disk).
            self.settings["recent_projects"] = add_recent_project(
                self.settings.get("recent_projects", []),
                path_str,
            )
            self._render_recent_projects()
            try:
                self._save_settings()
            except Exception as e:
                _logger.warning("Failed to save settings after adding recent project: %s", e)

        self._tk_after(0, _apply_and_persist)

    def _remove_recent_entry(self, path_str: str) -> None:
        """Drop ``path_str`` from ``recent_projects`` and re-render the panel."""
        self.settings["recent_projects"] = [
            p for p in self.settings.get("recent_projects", []) if p != path_str
        ]
        self._render_recent_projects()
        # Persist immediately: every caller runs on the Tk main thread
        # (button handlers or ``after(0, ...)`` marshalling), so a
        # settings save here is safe — without it a removal was only
        # flushed by the NEXT unrelated settings save (or never, if the
        # app closed right after).
        try:
            self._save_settings()
        except Exception as e:
            _logger.warning("Failed to save settings after removing recent project: %s", e)

    def _delete_recent_project(self, path_str: str) -> None:
        """Confirm with the user, then recursively delete the project dir.

        The path comes from ``recent_projects`` in settings.json — plain,
        user-editable config data (a swapped settings file can put any
        path in it) — so it is validated via
        :func:`~stream2video.paths.validate_project_delete` BEFORE the
        confirmation dialog and before any rmtree. A foreign or sensitive
        path is never deleted: the entry is dropped from the list and the
        user gets a warning instead.
        """
        if self.running:
            self._log("Cannot delete a project while pipeline is running")
            return
        path = Path(path_str)
        if not path.is_dir():
            self._log(f"Project no longer exists, dropping from list: {path_str}")
            self._remove_recent_entry(path_str)
            return
        # Audit #7: in flat mode the recents entry is the shared output
        # directory, which this app never marks as a project dir — a
        # recursive delete of it is impossible by design. Drop the entry
        # silently instead of showing a warning for a state the UI no
        # longer exposes (the render hides the trash button for
        # unmarked entries; a hand-edited settings.json can still land
        # here).
        try:
            marked = is_marked_project_dir(path)
        except OSError:
            marked = False
        if not marked:
            self._log(f"Not an app-created project directory — removed from list: {path_str}")
            self._remove_recent_entry(path_str)
            return
        ok, reason = validate_project_delete(path)
        if not ok:
            self._log(f"[WARN] Refusing to delete {path}: {reason}")
            messagebox.showwarning(
                "Project not deleted",
                f"{path}\n\n"
                f"Was NOT deleted: {reason}.\n\n"
                f"Only directories created by this application as project "
                f"directories can be deleted from Recent Projects. "
                f"The entry has been removed from the list.",
                parent=self.winfo_toplevel(),
            )
            self._remove_recent_entry(path_str)
            return
        # dir_size_mb rglobs a potentially multi-GB project tree — on the
        # Tk thread that froze the window for seconds BEFORE the confirm
        # dialog even appeared (the rmtree itself already runs in its own
        # daemon thread). Compute the size off-thread and marshal the
        # result back; the dialog then opens from the main loop as usual.
        self._log("Calculating project size...")

        def _size_worker() -> None:
            try:
                size = dir_size_mb(path)
            except OSError:
                size = -1.0

            def _continue() -> None:
                try:
                    alive = self.winfo_exists()
                except Exception:
                    alive = False
                if alive:
                    self._confirm_project_delete(path, path_str, size)

            try:
                self._tk_after(0, _continue)
            except Exception:
                pass

        threading.Thread(target=_size_worker, daemon=True).start()

    def _confirm_project_delete(self, path: Path, path_str: str, size_mb: float) -> None:
        """Confirmation dialog + rmtree spawn for a validated project dir.

        Runs on the Tk main loop (marshalled here by the size worker);
        the deletion itself is spawned into a daemon thread at the end.
        """
        size_text = f"{size_mb:.1f} MB" if size_mb >= 0 else "unknown"
        msg = (
            f"Delete project '{path.name}' and ALL its contents?\n\n"
            f"Location: {path}\n"
            f"Approx size: {size_text}\n\n"
            f"This will permanently remove the source video (if downloaded), "
            f"the compressed output, the audio cache, the silence cache, "
            f"and the log file.\n\n"
            f"This cannot be undone."
        )
        # Parent the dialog so it's modal w.r.t. this window — an
        # unparented messagebox can be hidden behind the main window
        # while still blocking input, and lets the user get a second
        # click in on a "delete" that wasn't answered yet.
        ok = messagebox.askyesno(
            "Delete project?", msg, icon="warning", parent=self.winfo_toplevel()
        )
        if not ok:
            return
        # rmtree of a multi-GB project dir (source video + caches + log)
        # would freeze the UI for tens of seconds — long enough for
        # Windows to mark the window "Not responding". Run it in a
        # daemon thread; the list update + log marshalled back onto the
        # main loop when it's done. The dialog above captures `path` by
        # value, so a concurrent "delete again" can't mutate it mid-run.
        self._log(f"Deleting project: {path} ...")

        def _rmtree_worker() -> None:
            err: OSError | None = None
            # TOCTOU guard: ``path`` was validated (and confirmed by the
            # user) BEFORE the dialog, but the directory could have been
            # swapped or re-pointed in between (renamed + replaced by a
            # symlink, user moved a folder over it, an AV restore raced
            # us). Re-validate the RESOLVED path right before the
            # rmtree — the validation, not the stale pre-dialog check,
            # is the boundary for destructive actions. An invalid path
            # at this point is dropped from the list without deleting.
            try:
                resolved = path.resolve()
            except OSError:
                resolved = Path(os.path.abspath(str(path)))
            ok, reason = validate_project_delete(resolved)
            if not ok:
                self._log(f"[WARN] Refusing to delete {resolved} at delete time: {reason}")

                def _skip() -> None:
                    self._remove_recent_entry(path_str)

                try:
                    self.winfo_toplevel().after(0, _skip)
                except Exception:
                    pass
                return
            try:
                shutil.rmtree(resolved)
            except OSError as e:
                err = e

            def _finish() -> None:
                if err is not None:
                    self._log(f"[ERROR] Failed to delete {resolved}: {err}")
                    messagebox.showerror(
                        "Delete failed",
                        f"Could not delete {resolved}:\n{err}",
                        parent=self.winfo_toplevel(),
                    )
                    return
                self._log(f"Deleted project: {resolved}")
                self._remove_recent_entry(path_str)

            try:
                self.winfo_toplevel().after(0, _finish)
            except Exception:
                # Toplevel already destroyed mid-shutdown; nothing to
                # marshal back to. The delete already happened (or the
                # error is lost — acceptable during shutdown).
                pass

        threading.Thread(target=_rmtree_worker, daemon=True, name=f"rmtree:{path.name}").start()

    def _open_in_explorer(self, path_str: str) -> None:
        """Open the project directory in the platform's file manager."""
        path = Path(path_str)
        try:
            open_in_file_manager(path)
        except FileNotFoundError:
            messagebox.showwarning(
                "Folder not found",
                f"Directory no longer exists:\n{path_str}",
                # Same modal-parenting rule as every other dialog in this
                # mixin: an unparented warning can hide behind the main
                # window while still blocking input.
                parent=self.winfo_toplevel(),
            )
            self._remove_recent_entry(path_str)
        except OSError as e:
            self._log(f"[ERROR] Could not open {path}: {e}")
