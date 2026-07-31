"""Tk main-loop dispatcher + log queue plumbing — extracted from
``gui.py`` (Этап 10 incremental refactor).

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
import time
from collections.abc import Callable
from typing import Any, Protocol

from stream2video.gui_log_handler import QueueHandler

logger = logging.getLogger(__name__)


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
        except Exception as e:
            # ``TclError`` from root destroyed + any other exception from
            # a dead root → drop the queued update so the worker's finally
            # can still run cleanup, but log unexpected scheduler failures.
            if e.__class__.__name__ != "TclError":
                logger.debug("TkDispatcher.schedule dropped callback", exc_info=True)


class TkTextbox(Protocol):
    """Structural type for the GUI's textbox (``CTkTextbox``)."""

    def configure(self, **kwargs: Any) -> None: ...

    def insert(self, index: str, text: str) -> None: ...

    def see(self, index: str) -> None: ...


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

    def __init__(
        self,
        textbox: TkTextbox,
        dispatcher: TkDispatcher,
        log_queue: queue.Queue[str] | None = None,
    ):
        self._textbox = textbox
        self._dispatcher = dispatcher
        self._queue: queue.Queue[str] = log_queue or queue.Queue()
        # The logger handler set up by ``setup_logging`` is kept here so
        # the GUI's close path can remove it later (single-handler plan
        # — only one QueueHandler per GUI instance). Tests can also peek
        # to confirm wiring.
        self._handler: QueueHandler | None = None

    @property
    def queue(self) -> queue.Queue[str]:
        return self._queue

    def log(self, message: str) -> None:
        """Push a timestamped message onto the queue. Safe from any thread."""
        timestamp = time.strftime("%H:%M:%S")
        self._queue.put(f"[{timestamp}] {message}")

    def poll(self) -> None:
        """Drain the queue into the textbox; reschedule the next poll.

        Must be called on the Tk main loop. Stops the self-rescheduling
        chain if the textbox (or root) has been destroyed — otherwise
        the poller would re-raise ``TclError`` forever.
        """
        try:
            while True:
                try:
                    msg = self._queue.get_nowait()
                except queue.Empty:
                    break
                self._textbox.configure(state="normal")
                self._textbox.insert("end", msg + "\n")
                self._textbox.see("end")
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
