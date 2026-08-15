"""Platform helpers extracted from ``gui.py``.

Pure / OS-level operations that don't depend on Tk state: directory
size probing and the cross-platform "open in file manager" call.
Kept in their own module so they can be unit-tested without driving
the Tk main loop, and so the GUI class is one step smaller.

  * ``dir_size_mb(path)`` — sums ``st_size`` of every file under
    ``path`` via ``rglob``. Fast but approximate (doesn't follow
    symlinks, doesn't account for sparse files). Used by the recent-
    projects panel to show "~12 MB" next to each entry.
  * ``open_in_file_manager(path)`` — Windows ``os.startfile``,
    macOS ``open``, Linux ``xdg-open``. Raises ``FileNotFoundError``
    when the dir is gone so the GUI can prune the entry from its
    recent-projects list.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def dir_size_mb(path: Path) -> float:
    """Approximate directory size in MB. Fast — sums ``stat().st_size``.

    Walks the tree once with ``rglob("*")``; doesn't follow symlinks
    (matches ``Path.is_file()`` semantics). Errors on individual files
    (permission denied, vanished mid-walk) are swallowed so the result
    is always a number — a partial tree still produces a useful
    approximation for the recent-projects panel's "~12 MB" hint.

    Returns 0.0 for a missing directory; the GUI treats 0.0 as
    "empty or unreadable" and shows "—" in the panel.
    """
    total = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total / 1024 / 1024


def open_in_file_manager(path: Path) -> None:
    """Open ``path`` in the platform's file manager.

    Raises ``FileNotFoundError`` when the directory doesn't exist so
    the caller can prune it from its recent-projects list — the GUI
    used to inline this check (with a messagebox) before calling the
    OS command; the raise lets the caller decide the UX.

    Raises ``OSError`` for any other failure (e.g. ``xdg-open`` not
    installed on a minimal Linux install) so the caller can show a
    "couldn't open" message instead of silently swallowing it.
    """
    if not path.is_dir():
        raise FileNotFoundError(f"Directory no longer exists: {path}")
    if os.name == "nt":
        # ``os.startfile`` is Windows-only; mypy complains on other
        # platforms because the attribute doesn't exist in typeshed's
        # POSIX stubs. Use getattr so the runtime call works on Windows
        # (where the attr exists) and the type checker doesn't see a
        # missing-attribute error on Linux/macOS CI runs.
        startfile = getattr(os, "startfile", None)
        if startfile is not None:
            startfile(str(path))
        else:
            # Fallback that shouldn't realistically trigger on real
            # Windows, but keeps the function total for type checkers.
            raise OSError("os.startfile unavailable on this platform")
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


# Target window size for the GUI on a typical desktop. Exported so
# tests can pin the constants and the GUI can import them without
# duplicating the values.
_DEFAULT_WINDOW_W = 1280
_DEFAULT_WINDOW_H = 720
# Margins reserved around the window so it doesn't butt up against
# the screen edges or the taskbar / Dock.
_SCREEN_MARGIN_W = 40
_SCREEN_MARGIN_H = 60


def fit_to_screen(sw: int, sh: int) -> tuple[int, int]:
    """Return the default window size (w, h) clamped to the screen.

    Targets 1121x643 on a typical desktop; shrinks to (sw-40) x (sh-60)
    on smaller displays. The ``max(1, ...)`` floor guards against
    negative/zero values from absurdly small screens (e.g., a remote
    session at 200x150) where ``sw - 40`` could otherwise go negative.
    """
    return (
        max(1, min(_DEFAULT_WINDOW_W, sw - _SCREEN_MARGIN_W)),
        max(1, min(_DEFAULT_WINDOW_H, sh - _SCREEN_MARGIN_H)),
    )


def is_previewable_input(raw: str) -> bool:
    """True iff ``raw`` points at a readable local file (not a URL).

    Used by the GUI's "Waveform" button enable/disable logic. Mirrors
    the guards in ``_render_waveform_preview`` so the button reflects
    the actual preconditions (no file, URL, or non-existent path = no
    preview). Pure: no Tk, no side effects — takes the raw input
    string, returns True/False.

    Uses the same strict ``^https?://`` check as
    ``download._validate_url`` so a local filename containing ``://``
    (rare but legal on some filesystems) isn't misclassified as a URL
    and silently disabled.
    """
    if not raw:
        return False
    # URLs aren't previewable (they need a download first).
    if re.match(r"^https?://", raw, re.IGNORECASE):
        return False
    try:
        return Path(raw).expanduser().is_file()
    except (OSError, ValueError):
        return False
