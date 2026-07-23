"""Logging handler extracted from ``gui.py`` (Этап 10 incremental).

``QueueHandler`` is a tiny ``logging.Handler`` that pushes formatted
records onto a ``queue.Queue``. The GUI's log textbox polls the queue
every 100ms (via ``self.after``) so log records produced on worker
threads surface on the Tk main loop without blocking it.

Extracted to its own module so it can be reused by other Tk front-ends
(a future CLI-to-GUI embed, for instance) and so the GUI class doesn't
have to be imported just to instantiate a log handler in tests.
"""

from __future__ import annotations

import logging
import queue


class QueueHandler(logging.Handler):
    """Send log records to a queue for GUI display.

    The handler is intentionally minimal: it formats the record (using
    whatever ``Formatter`` the caller set on the handler, or the
    default) and pushes the formatted string onto the queue. The
    consumer (GUI textbox poller) is responsible for displaying it.

    Why a queue instead of a direct widget write: logging.Handler.emit
    can be called from ANY thread (worker threads, drain threads, the
    signal handler's thread). Tk widgets are not thread-safe — calling
    ``widget.insert`` from a non-main thread can crash the interpreter
    on some platforms. The queue decouples the producer (the logger)
    from the consumer (the Tk main loop), so the only thread touching
    the widget is the main one.
    """

    def __init__(self, log_queue: queue.Queue):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record: logging.LogRecord) -> None:
        # ``logging.Handler`` defines ``emit`` as the override point.
        # ``handle_error`` is the parent's safety net for format errors
        # — we don't override it, so a formatter that raises (e.g. a
        # bad %-format string in a custom Formatter) logs to stderr
        # instead of crashing the worker thread that emitted the record.
        self.log_queue.put(self.format(record))
