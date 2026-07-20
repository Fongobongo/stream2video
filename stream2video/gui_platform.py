"""Platform helpers extracted from ``gui.py`` (Этап 10 incremental).

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
