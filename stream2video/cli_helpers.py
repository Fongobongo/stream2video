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
import os
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


# Global serialization for logging_session: logging state is process-
# global, so two OVERLAPPING sessions in different threads (a host
# embedding the CLI in a worker thread while another thread also runs
# one) would each snapshot the other's already-mutated state and
# restore in the wrong order — one session could close the other's
# handler or leave JSON mode installed after a rich run (audit round 16
# P2). The lock is acquired NON-blocking (audit round 19 P2): the CLI
# holds its logging session for the WHOLE run — download/detect/encode
# can take hours — so a blocking lock would hang a second embedded CLI
# call for hours with no feedback. A second concurrent session is
# rejected up-front with an explicit error instead.
#
# The lock is a plain ``threading.Lock``, NOT an ``RLock`` (audit round
# 20 P1): reentrant acquisition would let the SAME thread nest a second
# session inside a live one — the nested enter would snapshot the
# outer's already-mutated state and could flip ``_JSON_LOG_MODE`` back
# to False while the outer session is still in JSON, letting Rich
# banner/progress leak into JSON stdout. A plain Lock rejects even the
# owner on the second acquisition.
_LOGGING_SESSION_LOCK = threading.Lock()


class LoggingSessionBusyError(RuntimeError):
    """Raised when :func:`logging_session` is entered while another
    session is already live (in this thread or another).

    Separate from internal ``RuntimeError``s so the CLI can turn an
    EXPECTED concurrency rejection into a short user-facing message
    with exit code 1 instead of an unhandled traceback (audit round 20
    P2).
    """


@contextlib.contextmanager
def logging_session(
    log_format_lower: str,
    log_level: str,
    set_json_mode: Callable[[bool], None] | None = None,
) -> Iterator[LoggingSessionState]:
    """Configure CLI logging for one run and restore everything on exit.

    Only ONE logging session may be live at a time — logging state is
    process-global, so overlapping sessions from different threads
    cannot be made safe (the snapshot/restore of the second session
    would see the first one's half-installed state), and reentrant
    nesting in the SAME thread is just as unsafe (the nested snapshot
    would capture the outer's half-installed state; audit round 20 P1).
    A second concurrent session is REJECTED with
    :class:`LoggingSessionBusyError` instead of blocking: the CLI holds
    its session for the whole run, which can last hours (audit round 19
    P2 — the previous blocking lock could silently hang an embedded
    worker pool for the duration of an unrelated encode).

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
    if not _LOGGING_SESSION_LOCK.acquire(timeout=0):
        raise LoggingSessionBusyError(
            "another embedded CLI session is active; logging sessions cannot overlap"
        )
    try:
        yield from _logging_session_unlocked(log_format_lower, log_level, set_json_mode)
    finally:
        _LOGGING_SESSION_LOCK.release()


def _logging_session_unlocked(
    log_format_lower: str,
    log_level: str,
    set_json_mode: Callable[[bool], None] | None = None,
) -> Iterator[LoggingSessionState]:
    """Unserialized body of :func:`logging_session`.

    Only ever runs while the caller holds ``_LOGGING_SESSION_LOCK``, so
    its snapshot/restore can't race another session's mutations.
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
    # The ``try`` starts IMMEDIATELY after the snapshot: every mutating
    # statement in the two install branches below must be covered by the
    # ``finally``'s restore. A failure mid-install (a handler constructor
    # raising after it has already flipped ``propagate`` / attached
    # handlers) must not leak a half-applied logging state. Nothing above
    # the snapshot mutates state, so this boundary is the only correct
    # one — the audit's experiment proved the inverse: an exception
    # inside the (then-untouched) install section left the partial state
    # unrestored (audit round 13 feedback; the hand-written version had
    # the same hole until it was moved, this one must not regress).
    try:
        if log_format_lower == "json":
            # JSON structured logging — replace the Rich console handler
            # with a JSON formatter writing to stdout so the caller can
            # pipe the log stream into a renderer (``| jq .``, ELK,
            # Splunk). The file handler still writes human-readable text
            # to stream2video.log; only stdout switches format. The
            # human-readable banner and progress bars are suppressed in
            # JSON mode (they'd pollute the JSON stream).
            _json_handler = install_json_handler(logger, level=log_level.upper())
            # install_json_handler attached the handler to the app ``logger``;
            # it is ALSO rooted on the root logger below so records from
            # every logger (not just "stream2video") come out as JSON.
            # ``logger.propagate`` defaults to True, so without the line below
            # every app record fires the handler TWICE (once directly, once
            # via propagation to the same handler at the root) and stdout
            # JSON is duplicated line-by-line — breaking ``| jq .`` pipes.
            logger.propagate = False
            # The app logger must carry ONLY the JSON handler for the run's
            # duration: a host that pre-attached its OWN handler to
            # ``stream2video`` (embedded host, GUI embeds, test capture)
            # would otherwise keep firing it for every CLI record — and, via
            # propagation, leak the JSON lines into the host's log stream
            # (audit round 14 P2: the session isolated root handlers but not
            # app-level ones; ``--log-level`` and the JSON-only contract
            # silently didn't apply to the host handler). Assignment doesn't
            # close() the dropped handlers — the ``finally`` below restores
            # the snapshot list verbatim.
            logger.handlers = [_json_handler]
            # Replace the root handler list directly (NOT
            # ``basicConfig(force=True)``): the rich branch does the same,
            # and ``force=True`` would close() any handler the host
            # attached — but this session is contractually obligated to
            # return the snapshot intact on exit (one CLI run inside an
            # embedded host must not break the host's logging). The
            # session guarantees exactly one handler on the root for the
            # run's duration and restores the original list on exit.
            _root_logger.setLevel(logging.DEBUG)
            _root_logger.handlers = [_json_handler]
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
            # Install the CLI's Rich console handler as the root handler for
            # THIS run — the session's stated contract ("a handler is
            # installed for the run's duration") and the only way
            # ``--log-level`` can act on a handler that is actually wired up.
            #
            # Previously this ran ``logging.basicConfig(handlers=[...])``
            # WITHOUT ``force``: when a host had already configured the root
            # logger (embedded process, test capture), basicConfig was a
            # no-op, the Rich handler was never attached, CLI records leaked
            # to the host's root handlers, and ``--log-level`` adjusted a
            # dead handler — one flag, three behaviours, contradicted by the
            # docstring (audit round 13 P3).
            #
            # We REPLACE the root handler list directly rather than calling
            # ``basicConfig(force=True)``: ``force=True`` closes() the
            # pre-existing handlers, but this session's snapshot/restore is
            # contractually obligated to return them *intact* on exit — the
            # host's own logging must not be broken by running one CLI
            # command inside it (audit round 13 "не ломать host").
            _root_logger.setLevel(logging.DEBUG)
            _root_logger.handlers = [_console_handler]
            # Detach any handlers a host pre-attached to the app ``logger``
            # for the run's duration: with them in place every CLI record
            # would fire the host handler AND the root Rich handler via
            # propagation — duplicated lines, and ``--log-level`` couldn't
            # gate the host handler (audit round 14 P2, same class as the
            # JSON branch above). Assignment doesn't close() them; the
            # snapshot's ``logger.handlers`` list is restored verbatim on
            # exit. The CLI's own per-run file handler is attached to
            # ``logger`` AFTER this point (once the output dir resolves),
            # so this clearing never touches it.
            logger.handlers = []
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
    from stream2video.tools import ffmpeg_install_hint

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            console.print(f"[red]Error:[/red] {tool} not found in PATH")
            # Same per-OS hint --doctor prints; the old text hardcoded the
            # Windows winget command and fed it to macOS/Linux users too.
            console.print(f"  Install: [cyan]{ffmpeg_install_hint()}[/cyan]")
            if os.name == "nt":
                console.print("  Or run:  [cyan]setup.ps1[/cyan] (Windows)")
            raise typer.Exit(1)
