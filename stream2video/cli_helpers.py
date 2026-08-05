"""Shared low-level helpers for the CLI entry point.

These were extracted from ``cli.py`` (which had grown past 1000 lines):
signal wiring, the per-run file handler, the ffmpeg presence check, the
``ParameterSource`` import shim, and the shared ``console`` / ``logger``
/ ``app`` singletons. ``cli.py`` re-exports every name from here so
existing ``stream2video.cli.<name>`` attribute access (including the
test suite's ``patch`` calls) keeps working unchanged.
"""

import logging
import shutil
import signal
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.logging import RichHandler

# Logging setup is deferred to ``main()`` so importing ``stream2video.cli``
# (e.g. from tests, or from a host application embedding the library)
# doesn't reconfigure the root logger. The historical ``basicConfig``
# at import time would override the host's own logging config, which is
# especially noisy for GUI embeds and pytest's caplog. See P2.9 in the
# fix plan.
_console_handler = RichHandler(rich_tracebacks=True)
logger = logging.getLogger("stream2video")

console = Console()
app = typer.Typer(help="Compress stream recordings by removing silence")

# ``ParameterSource`` tells us whether a CLI flag came from the command
# line or a default. Its import path has moved across typer/click
# releases. Use a defensive try/except chain so the module keeps
# importing on all supported versions.
ParameterSource: Any = None
try:
    from click.core import ParameterSource as _PS  # click >= 8.0

    ParameterSource = _PS
except ImportError:  # pragma: no cover - legacy fallback
    try:
        from typer._click.core import ParameterSource as _PS2

        ParameterSource = _PS2
    except ImportError:  # pragma: no cover - very old typer
        pass


def _make_sigint_cancel() -> tuple[threading.Event, Callable[[], bool]]:
    """Wire SIGINT to a cancel event so Ctrl+C aborts running ffmpeg/yt-dlp.

    Returns (event, callback). The callback returns True once SIGINT has been
    received. The event is set by the signal handler in the main thread, but
    signal handlers in Python can only safely set an event/flag, not raise.

    When called from a non-main thread (host application embedding the CLI
    in a worker thread) ``signal.signal`` raises ``ValueError`` — Python only
    allows signal handling from the interpreter's main thread. In that case
    the function logs a warning and returns an event that never fires: the
    embedding host owns Ctrl+C dispatch (e.g. via its own signal handler that
    sets the returned event), and the CLI run proceeds without a SIGINT hook.
    The pipeline's ``cancel_callback`` is still polled, so a host that flips
    the event manually still cancels cleanly.
    """
    event = threading.Event()

    def _handler(signum: Any, frame: Any) -> None:
        event.set()

    try:
        signal.signal(signal.SIGINT, _handler)
    except (ValueError, OSError) as e:
        # ``ValueError`` is raised when called from a non-main thread;
        # ``OSError`` on some platforms for the same reason. Log and
        # continue — the embedding host is responsible for wiring Ctrl+C
        # to the returned event (or to its own cancel mechanism).
        logger.warning(f"Could not install SIGINT handler (non-main thread?): {e}")

    def _cb() -> bool:
        return event.is_set()

    return event, _cb


def _make_file_handler(path: Path) -> logging.FileHandler:
    """Create the CLI's per-run file handler with the canonical format.

    DEBUG-level so the file always gets the full trace; the user-facing
    console level is controlled separately by ``_console_handler.setLevel``.
    Format: ``%(asctime)s - %(name)s - %(levelname)s - %(message)s`` —
    matches what stream2video.log has always written so existing log-
    parsing scripts keep working across upgrades.
    """
    # Use UTF-8 explicitly so the log file is consistent across platforms
    # (Windows OEM codepages are often not UTF-8 and would raise
    # UnicodeEncodeError on non-ASCII paths/labels mid-run, swallowed
    # by logging.handleError and lost). Matches the cache writers in
    # silence.py / config.py.
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    return fh


def _check_ffmpeg() -> None:
    """Warn if ffmpeg or ffprobe is missing."""
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            console.print(f"[red]Error:[/red] {tool} not found in PATH")
            console.print("  Install: [cyan]winget install Gyan.FFmpeg[/cyan]")
            console.print("  Or run:  [cyan]setup.ps1[/cyan] (Windows)")
            raise typer.Exit(1)
