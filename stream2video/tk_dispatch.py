"""Tk main-loop dispatcher + log queue plumbing — extracted from
``gui.py`` (incremental refactor).

The GUI's worker threads must talk to the Tk main loop instead of
touching widgets directly (Tk widgets are not thread-safe). Two pieces
of glue make this safe:

  * ``TkDispatcher`` — a tiny ``self.after`` wrapper that swallows
    ``TclError`` if the root window is destroyed mid-pipeline (a
    close-during-run race) so queued callbacks don't crash the worker
    thread with an unhandled exception. The class holds no state other
    than the root reference; tests can drive it via a fake.
  * ``LogQueuePoller`` — owns a ``queue.Queue`` + a textbox reference.
    The ``log`` method pushes timestamped strings onto the queue from
    any thread; ``poll`` (called on the Tk main loop via ``self.after``
    every 100 ms) drains the queue and inserts each message into the
    textbox. The queue decouples producers (worker threads / the logging
    ``QueueHandler``) from the consumer (the Tk main loop) so widgets
    are only ever touched on the main thread. ``setup_logging`` wires
    a ``QueueHandler`` onto the ``stream2video`` logger — extracted here
    so a test can verify the handler shape without instantiating the
    GUI.

These pieces move out together because they share the queue and form
the contract every other worker↔widget interaction in the GUI relies on
(see the ``_ui_*`` family and the pipeline worker's callbacks).
"""

from __future__ import annotations

import logging
import queue
import re
import time
import tkinter
from collections.abc import Callable
from typing import Any, Protocol

from stream2video.gui_log_handler import QueueHandler

logger = logging.getLogger(__name__)

# Severity markers the pipeline prefixes to log lines (``[WARN]``,
# ``[ERROR]``). The poller re-reads them so it can color the whole
# matching line in the textbox.
_SEVERITY_RE = re.compile(r"\[(ERROR|WARN|INFO|DEBUG)\]")

# Theme-aware colours for warn/error log lines. ``see()`` keeps the
# log pinned to the newest line; these colours are chosen to stay
# readable on both the dark and light CTk themes.
_TAG_FG = {
    "error": {"dark": "#ff6b6b", "light": "#c62828"},
    "warn": {"dark": "#ffb300", "light": "#b26a00"},
}


class TkRoot(Protocol):
    """Structural type for anything that quacks like a Tk root (has
    ``after`` and raises ``TclError`` if destroyed). The GUI's
    ``ctk.CTk`` subclass implements this directly; tests pass a fake
    with controllable ``after`` behaviour.
    """

    def after(self, ms: int, func: Callable[..., Any]) -> str: ...


class TkDispatcher:
    """Schedules callables on the Tk main loop, swallowing ``TclError``
    if the root was destroyed between the schedule and the dispatch.

    The GUI's worker threads call this for every UI update (progress
    bar, status label, log line, …). Without the swallow a window
    closed mid-pipeline would surface an uncaught ``TclError`` from the
    worker thread's queued callbacks and leave a confusing logger
    traceback — the swallow turns the race into a quiet no-op so the
    cancel / cleanup path can finish cleanly.
    """

    def __init__(self, root: TkRoot):
        self._root = root

    def schedule(self, ms: int, func: Callable[..., Any]) -> None:
        try:
            self._root.after(ms, func)
        except tkinter.TclError:
            # Root destroyed mid-shutdown — the expected case. Drop the
            # queued update so the worker's finally can still run cleanup.
            pass
        except Exception:
            # Any OTHER exception from a dead/dying root is unexpected —
            # log it instead of silently swallowing (the old name-string
            # check ``e.__class__.__name__ != "TclError"`` also matched
            # subclasses and custom Tk wrappers only by accident).
            logger.debug("TkDispatcher.schedule dropped callback", exc_info=True)


class TkTextbox(Protocol):
    """Structural type for the GUI's textbox (``CTkTextbox``)."""

    def configure(self, **kwargs: Any) -> None: ...

    def insert(self, index: str, text: str) -> None: ...

    def delete(self, index1: str, index2: str | None = None) -> None: ...

    def see(self, index: str) -> None: ...

    def index(self, index: str) -> str: ...

    def tag_config(self, tag: str, **kwargs: Any) -> None: ...

    def tag_add(self, tag: str, index1: str, index2: str) -> None: ...


class LogQueuePoller:
    """Owns the GUI's log queue: workers push via ``log`` and the Tk
    main loop polls via ``poll``.

    ``log`` is safe to call from any thread (a ``queue.Queue`` is the
    lock). ``poll`` must run on the Tk main loop because it touches the
    textbox; the GUI reschedules itself every 100 ms via
    ``dispatcher.schedule(...)``.

    The poller stops itself (doesn't reschedule) when the textbox raises
    ``TclError`` (root destroyed → rescheduling would re-raise forever)
    or when an unexpected exception happens in the drain loop (so a
    single bad message doesn't spin the poller at 100 % CPU forever).
    """

    _POLL_INTERVAL_MS = 100
    _MAX_LOG_LINES = 10_000  # Unbounded growth = GUI memory leak

    def __init__(
        self,
        textbox: TkTextbox,
        dispatcher: TkDispatcher,
        log_queue: queue.Queue[str] | None = None,
        theme: str = "dark",
    ):
        self._textbox = textbox
        self._dispatcher = dispatcher
        self._queue: queue.Queue[str] = log_queue or queue.Queue()
        # The logger handler set up by ``setup_logging`` is kept here so
        # the GUI's close path can remove it later (single-handler plan
        # — only one QueueHandler per GUI instance). Tests can also peek
        # to confirm wiring.
        self._handler: QueueHandler | None = None
        # Theme name ("dark"/"light") selects the warn/error tag colours.
        self._theme = theme
        self._tags_configured = False
        # Running count of log lines already written into the textbox.
        # Tk index arithmetic with CTkTextbox is fragile (a phantom
        # trailing newline shifts ``index("end")``), so we track line
        # numbers ourselves — the log is append-only and every message
        # is one line, which makes this exact.
        self._line_count = 0

    @property
    def queue(self) -> queue.Queue[str]:
        return self._queue

    def log(self, message: str) -> None:
        """Push a timestamped message onto the queue. Safe from any thread."""
        timestamp = time.strftime("%H:%M:%S")
        self._queue.put(f"[{timestamp}] {message}")

    @staticmethod
    def _severity_tag(message: str) -> str | None:
        """Map a log line's ``[WARN]``/``[ERROR]`` marker to a tag name.

        Returns ``"error"`` for ``[ERROR]``, ``"warn"`` for ``[WARN]``,
        and ``None`` for anything else (including ``[INFO]``/``[DEBUG]``,
        which keep the default text colour). Mirrors the GUI's manual
        ``[WARN]``/``[ERROR]`` prefixes in ``gui_lifecycle``,
        ``pipeline_controller`` etc., plus the logging-handler path.
        """
        m = _SEVERITY_RE.search(message)
        if m is None:
            return None
        level = m.group(1)
        if level == "ERROR":
            return "error"
        if level == "WARN":
            return "warn"
        return None

    def _ensure_tags(self) -> None:
        """Configure the warn/error text tags once (theme-aware)."""
        if self._tags_configured:
            return
        for tag, colours in _TAG_FG.items():
            self._textbox.tag_config(tag, foreground=colours.get(self._theme, colours["dark"]))
        self._tags_configured = True

    def _apply_tag(self, tag: str, start: str, end: str) -> None:
        """Colour the line ``start..end`` with ``tag``; set up the tag on first use."""
        self._ensure_tags()
        self._textbox.tag_add(tag, start, end)

    def set_theme(self, theme: str) -> None:
        """Switch the warn/error tag colours to a new CTk theme.

        ``tags`` are configured lazily on first use, so a theme change
        can't re-skin already-configured tags — force a re-setup (old
        lines keep their colour, new lines use the new theme's palette).
        """
        self._theme = theme
        self._tags_configured = False

    def poll(self) -> None:
        """Drain the queue into the textbox; reschedule the next poll.

        Must be called on the Tk main loop. Stops the self-rescheduling
        chain if the textbox (or root) has been destroyed — otherwise
        the poller would re-raise ``TclError`` forever.
        """
        try:
            inserted = False
            # Flip to ``normal`` once for the whole batch and back to
            # ``disabled`` after — avoids the per-message state toggle
            # flicker problem when dozens of lines arrive in one poll
            # (Tk re-lays out the widget on each ``insert``, and toggling
            # state around every line makes the auto-scroll fight the
            # pending redraw).
            self._textbox.configure(state="normal")
            try:
                while True:
                    try:
                        msg = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    self._textbox.insert("end", msg + "\n")
                    self._line_count += 1
                    tag = self._severity_tag(msg)
                    if tag is not None:
                        # The log is append-only and every message is exactly
                        # one line, so line ``_line_count`` is the line just
                        # written. Tag the whole line (excluding the trailing
                        # newline, which Tk owns).
                        self._apply_tag(
                            tag, f"{self._line_count}.0", f"{self._line_count}.0 lineend"
                        )
                    # Trim the oldest lines once the log outgrows the
                    # budget — otherwise a multi-hour run's log widget
                    # accumulates tens of thousands of lines and the GUI's
                    # memory footprint grows unboundedly.
                    # Tk line indices are 1-based; deleting 1..extra removes
                    # exactly the oldest ``extra`` lines and shifts every
                    # subsequent line down — our own ``_line_count`` tracks
                    # the new total.
                    if self._line_count > self._MAX_LOG_LINES:
                        extra = self._line_count - self._MAX_LOG_LINES
                        try:
                            self._textbox.delete("1.0", f"{extra}.0 lineend + 1 char")
                        except Exception:
                            logger.debug("log trim failed", exc_info=True)
                        self._line_count -= extra
                    inserted = True
                if inserted:
                    # Auto-scroll to the bottom once the whole batch is in.
                    # ``see("end")`` scrolls to the last line; calling it
                    # after all inserts (instead of after each one) keeps the
                    # view pinned to the tail while the log grows.
                    self._textbox.see("end")
            finally:
                # An exception mid-batch (TclError from a widget race, a
                # ValueError from a tag index overflow) must not leave the
                # textbox permanently user-editable — restore the read-only
                # state even when the drain fails.
                self._textbox.configure(state="disabled")
        except Exception:
            # Root destroyed (TclError) or textbox gone — stop the poller
            # quietly. Logging the exception would loop forever through
            # this same handler.
            return
        self._dispatcher.schedule(self._POLL_INTERVAL_MS, self.poll)

    def setup_logging(self) -> None:
        """Wire a ``QueueHandler`` onto the ``stream2video`` logger.

        Idempotent across the GUI's lifecycle (the GUI builds one poller
        in ``__init__`` and never again), but defensive against double
        setup so a test that re-instantiates the poller doesn't double-
        log every record.
        """
        if self._handler is not None:
            return
        handler = QueueHandler(self._queue)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logging.getLogger("stream2video").addHandler(handler)
        self._handler = handler

    def teardown_logging(self) -> None:
        """Detach the ``QueueHandler`` the poller attached in ``setup_logging``.

        Without this, a reused process (test suite, or a future in-process
        GUI restart) would accumulate defunct handlers on the ``stream2video``
        logger — each pointing at a queue whose poller no longer exists,
        which both leaks memory and means later log records are silently
        dropped into an un-drain queue.
        """
        if self._handler is None:
            return
        try:
            logging.getLogger("stream2video").removeHandler(self._handler)
        except Exception:
            # Handler was already detached (e.g. logging module teardown) —
            # nothing to do. Never raise from a close path.
            pass
        finally:
            self._handler = None
