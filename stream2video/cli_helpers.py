"""Shared low-level helpers for the CLI entry point.

These were extracted from ``cli.py`` (which had grown past 1000 lines):
signal wiring, the per-run file handler, the ffmpeg presence check, the
``ParameterSource`` import shim, the shared ``console`` / ``logger``
/ ``app`` singletons, and the per-run logging session context manager.
``cli.py`` re-exports every name from here so existing
``stream2video.cli.<name>`` attribute access (including the test suite's
``patch`` calls) keeps working unchanged.
"""

import contextlib
import logging
import shutil
import signal
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.logging import RichHandler

from stream2video.json_logging import install_json_handler

# Logging setup is deferred to ``main()`` so importing ``stream2video.cli``
# (e.g. from tests, or from a host application embedding the library)
# doesn't reconfigure the root logger. The historical ``basicConfig``
# at import time would override the host's own logging config, which is
# especially noisy for GUI embeds and pytest's caplog.
_console_handler = RichHandler(rich_tracebacks=True)
logger = logging.getLogger("stream2video")

console = Console()
app = typer.Typer(help="Compress stream recordings by removing silence")

# Module-level flag toggled by --log-format json lives in ``cli.py``
# (``_JSON_LOG_MODE``): it gates CLI-ONLY presentation (the banner, the
# Rich progress bars), so the logging layer never touches it directly —
# :func:`logging_session` reports mode changes through the caller's
# ``set_json_mode`` hook instead, and the flag's per-run setup/restore
# is owned by the session below.


class LoggingSessionState:
    """Mutable state carried by one :func:`logging_session` run.

    ``file_handler`` holds the per-run stream2video.log handler; the CLI
    creates it only after the output directory resolves (and swaps it when
    the project dir moves mid-run), so the session can't build it in
    ``__enter__``. Whatever object this points at when the session ends is
    detached from the logger and closed.
    """

    __slots__ = ("file_handler",)

    def __init__(self) -> None:
        self.file_handler: logging.FileHandler | None = None


@contextlib.contextmanager
def logging_session(
    log_format_lower: str,
    log_level: str,
    set_json_mode: Callable[[bool], None] | None = None,
) -> Iterator[LoggingSessionState]:
    """Configure CLI logging for one run and restore everything on exit.

    On enter: snapshot the root logger's handlers/level, the app logger's
    handlers + ``propagate``, ``console.stderr``, the console handler's
    level and the CLI's JSON-mode flag; then install either the JSON
    stdout handler (``--log-format json``) or the Rich console handler.
    On exit (EVERY exit — success, ``typer.Exit``, or exception): close
    the run's file handler (``state.file_handler`` if one was attached),
    remove any handler this run added, and restore the snapshot exactly.

    ``set_json_mode`` is the CLI's ``_JSON_LOG_MODE`` setter — invoked
    with ``True``/``False`` on enter and with ``False`` on exit, so the
    flag can't outlive the run even on the exception paths.

    This is the structural fix for the audit's P1 logging-leak findings:
    the old hand-written snapshot + ``try/finally`` inside ``main()``
    leaked state whenever the try boundary drifted past a mutating
    statement (missing ffmpeg, bad ``--log-level``). A context manager
    can't mis-place its own boundary — setup and restore live in one
    construct (audit round 13 follow-up).
    """
    state = LoggingSessionState()
    # The JSON-mode flag lives where the presentation code reads it
    # (cli.py gates the Rich banner / progress bars); a caller that
    # embeds the helpers without it simply doesn't need the flag.
    if set_json_mode is None:

        def set_json_mode(_value: bool) -> None:
            return None

    _root_logger = logging.getLogger()
    # Snapshot every piece of logging state this invocation rewrites so
    # the ``finally`` below can restore it: a host calling main() twice
    # in one process (embeds, tests with CliRunner) used to leak the
    # first run's JSON mode into the second — ``console.stderr`` stayed
    # True, the JSON handler stayed attached, ``_JSON_LOG_MODE`` stayed
    # True and the "rich" run printed to stderr through a JSON handler.
    _logging_snapshot = (
        list(_root_logger.handlers),
        _root_logger.level,
        logger.propagate,
        list(logger.handlers),
        console.stderr,
        # The console handler's level is rewritten by ``--log-level``;
        # a run that ends early (or a second in-process run) would
        # otherwise inherit the previous run's console level (audit P1:
        # the level was never restored, not even on the happy path).
        _console_handler.level,
    )
    if log_format_lower == "json":
        # JSON structured logging — replace the Rich console handler
        # with a JSON formatter writing to stdout so the caller can
        # pipe the log stream into a renderer (``| jq .``, ELK,
        # Splunk). The file handler still writes human-readable text
        # to stream2video.log; only stdout switches format. The
        # human-readable banner and progress bars are suppressed in
        # JSON mode (they'd pollute the JSON stream).
        _json_handler = install_json_handler(logger, level=log_level.upper())
        # install_json_handler attached the handler to the app ``logger``.
        # basicConfig below re-roots the same handler for the root logger.
        # ``logger.propagate`` defaults to True, so without the line below
        # every app record fires the handler TWICE (once directly, once
        # via propagation to the same handler at the root) and stdout
        # JSON is duplicated line-by-line — breaking ``| jq .`` pipes.
        logger.propagate = False
        logging.basicConfig(
            level=logging.DEBUG,
            handlers=[_json_handler],
            force=True,  # replace any handler the caller attached
        )
        # Keep the stdout stream line-per-JSON-record: point the shared
        # Rich console at stderr and disable the live progress bars.
        # Progress updates are still emitted as JSON log records by the
        # callbacks, so no information is lost — but no Rich frames,
        # banners, or summaries may leak into stdout, or a downstream
        # ``| jq .`` breaks on the first non-JSON line.
        console.stderr = True
        set_json_mode(True)
    else:
        set_json_mode(False)
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(message)s",
            handlers=[_console_handler],
        )
    try:
        yield state
    finally:
        if state.file_handler is not None:
            logger.removeHandler(state.file_handler)
            state.file_handler.close()
        (
            _root_handlers,
            _root_level,
            _propagate,
            _logger_handlers,
            _stderr,
            _console_level,
        ) = _logging_snapshot
        _console_handler.setLevel(_console_level)
        for _h in list(logger.handlers):
            logger.removeHandler(_h)
            if _h not in _logger_handlers:
                _h.close()
        for _h in _logger_handlers:
            logger.addHandler(_h)
        logger.propagate = _propagate
        _root_logger.handlers[:] = _root_handlers
        _root_logger.setLevel(_root_level)
        console.stderr = _stderr
        set_json_mode(False)


# Tracks the SIGINT handler THIS module installed so cli.py can detect
# a double-main() run by identity rather than by a fragile name/module
# heuristic (a refactor that renames the closure breaks
# the name check silently).
_installed_sigint_handler: Callable[..., None] | None = None

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

    global _installed_sigint_handler
    _installed_sigint_handler = _handler

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
