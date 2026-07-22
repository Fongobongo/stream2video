"""Tests for stream2video.tk_dispatch (TkDispatcher + LogQueuePoller).

The dispatcher's job is to schedule work on the Tk main loop and swallow
the ``TclError`` that fires if the root was destroyed between the
schedule and the dispatch. The poller's job is to drive a textbox from
a thread-safe queue and stop itself when the textbox is gone.
"""

from __future__ import annotations

import logging
import queue
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

from stream2video.tk_dispatch import (
    LogQueuePoller,
    TkDispatcher,
)


class _FakeDeadRoot:
    """Pretends to be a dead Tk root: ``after`` raises an Exception."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, Callable[..., Any]]] = []
        self.dead = False

    def after(self, ms: int, func: Callable[..., Any]) -> str:
        self.calls.append((ms, func))
        if self.dead:
            raise RuntimeError("root has been destroyed")
        return "tok"


class _FakeTexbox:
    """Records every call sequence so the poller test can assert."""

    def __init__(self) -> None:
        self.buffer: list[str] = []
        self.state = "normal"
        self.crashed = False

    def configure(self, **kwargs: Any) -> None:
        if self.crashed:
            raise RuntimeError("textbox gone")
        if "state" in kwargs:
            self.state = kwargs["state"]

    def insert(self, index: str, text: str) -> None:
        if self.crashed:
            raise RuntimeError("textbox gone")
        self.buffer.append(text)

    def see(self, index: str) -> None:
        if self.crashed:
            raise RuntimeError("textbox gone")

    def crash(self) -> None:
        """Make every subsequent call raise — used to simulate a
        destroyed root or a tear-down race."""
        self.crashed = True


class TestTkDispatcher:
    def test_schedule_calls_root_after(self):
        root = _FakeDeadRoot()
        d = TkDispatcher(root)
        d.schedule(0, lambda: None)
        # One call recorded; the function was passed through as-is.
        assert len(root.calls) == 1
        assert root.calls[0][0] == 0

    def test_schedule_passes_args_via_lambda(self):
        root = _FakeDeadRoot()
        d = TkDispatcher(root)
        sentinel = []
        d.schedule(100, lambda: sentinel.append(1))
        # The dispatcher shouldn't run the func itself — that's Tk's job.
        assert sentinel == []
        # Drive the queued callback to confirm it would have run.
        root.calls[0][1]()
        assert sentinel == [1]

    def test_schedule_swallows_when_root_destroyed(self):
        root = _FakeDeadRoot()
        root.dead = True
        d = TkDispatcher(root)
        # Doesn't raise — the swallow keeps the worker thread alive.
        d.schedule(0, lambda: None)
        # The call was still received by the root before the exception.
        assert len(root.calls) == 1


class TestLogQueuePoller:
    def test_log_pushes_timestamped_message_to_queue(self):
        poller = LogQueuePoller(
            textbox=_FakeTexbox(),
            dispatcher=TkDispatcher(_FakeDeadRoot()),
            log_queue=queue.Queue(),
        )
        poller.log("hello")
        msg = poller._queue.get_nowait()
        assert msg.startswith("[")
        assert "hello" in msg
        assert msg.endswith("hello")
        # Format should be ``[HH:MM:SS] hello`` — at minimum the
        # timestamp prefix.
        assert msg.count("[") == 1
        assert msg.count("]") == 1

    def test_log_reuses_provided_queue(self):
        q: queue.Queue[str] = queue.Queue()
        poller = LogQueuePoller(
            textbox=_FakeTexbox(),
            dispatcher=TkDispatcher(_FakeDeadRoot()),
            log_queue=q,
        )
        assert poller.queue is q
        poller.log("a")
        assert q.qsize() == 1

    def test_poll_drains_queue_into_textbox(self):
        textbox = _FakeTexbox()
        # The dispatcher is a fake; we want poll to NOT reschedule itself
        # for this single-step test, so we use a dead-on-schedule root
        # and assert that the messages land in the textbox before the
        # schedule-on-the-end raise is swallowed. Actually, the poller
        # reschedules after the while-loop; with a healthy root the
        # rescheduling lands too — driven by the dispatcher's
        # ``after`` callback list. Use a real-ish fake so the
        # reschedule is recorded.
        dispatcher_calls: list[tuple[int, Callable[..., Any]]] = []

        class _RecordingDispatcher(TkDispatcher):
            def __init__(self) -> None:
                # We don't need a real root; schedule is overridden.
                super().__init__(_FakeDeadRoot())

            def schedule(self, ms: int, func: Callable[..., Any]) -> None:
                dispatcher_calls.append((ms, func))

        poller = LogQueuePoller(
            textbox=textbox,
            dispatcher=_RecordingDispatcher(),
            log_queue=queue.Queue(),
        )
        poller.log("one")
        poller.log("two")
        poller.poll()
        assert len(textbox.buffer) == 2
        assert textbox.buffer[0].startswith("[") and textbox.buffer[0].endswith(" one\n")
        assert textbox.buffer[1].endswith(" two\n")
        # Poller reschedules itself for the next drain.
        assert len(dispatcher_calls) == 1
        assert dispatcher_calls[0][0] == 100

    def test_poll_stops_when_textbox_destroyed(self):
        textbox = _FakeTexbox()

        class _RecordingDispatcher(TkDispatcher):
            def __init__(self) -> None:
                super().__init__(_FakeDeadRoot())

            def schedule(self, ms: int, func: Callable[..., Any]) -> None:
                # Should never be called — the textbox is dead so poll
                # returns early before rescheduling.
                raise AssertionError("poller shouldn't reschedule once textbox is dead")

        poller = LogQueuePoller(
            textbox=textbox,
            dispatcher=_RecordingDispatcher(),
            log_queue=queue.Queue(),
        )
        # Push a message so the poller reaches the textbox configure
        # call (empty queue would just break the loop and reschedule).
        poller.log("stuff")
        textbox.crash()  # subsequent textbox calls raise
        poller.poll()  # must swallow and return without rescheduling

    def test_setup_logging_attaches_queue_handler_once(self):
        # Wires a QueueHandler onto the ``stream2video`` logger — but
        # only once across multiple calls. Use a separate logger name
        # so the test doesn't pollute the global ``stream2video``
        # logger handler list permanently.
        poller = LogQueuePoller(
            textbox=_FakeTexbox(),
            dispatcher=TkDispatcher(_FakeDeadRoot()),
            log_queue=queue.Queue(),
        )

        class _FakeLogger:
            def __init__(self) -> None:
                self.handlers: list[logging.Handler] = []

            def addHandler(self, handler: logging.Handler) -> None:
                self.handlers.append(handler)

        fake_logger = _FakeLogger()
        # Patch ``logging.getLogger`` for the duration of the test so
        # our poller only sees the fake. This way the handler-check is
        # isolated from the rest of the test suite's logging setup.
        with (
            patch.object(logging, "getLogger", return_value=fake_logger),
        ):
            poller.setup_logging()
            poller.setup_logging()  # idempotent
        assert len(fake_logger.handlers) == 1
        assert isinstance(fake_logger.handlers[0], logging.Handler)

    def test_log_after_setup_winds_up_in_queue(self):
        # Integration-lite: setup_logging wires a handler that pushes
        # formatted records onto the same queue ``log`` uses — so a
        # background ``logger.info("foo")`` is enough to surface it in
        # the textbox (the GUI's poller drains the queue and spills the
        # formatted message into the widget).
        poller = LogQueuePoller(
            textbox=_FakeTexbox(),
            dispatcher=TkDispatcher(_FakeDeadRoot()),
            log_queue=queue.Queue(),
        )

        # Use a test-local logger so the handler doesn't leak to other
        # tests via the global ``stream2video`` namespace.
        local_logger = logging.getLogger("stream2video.tk_dispatch_test")
        local_logger.handlers.clear()
        local_logger.setLevel(logging.DEBUG)

        # The handler's queue is the poller's queue — patch the
        # ``logging.getLogger`` call inside ``setup_logging`` so the
        # wiring attaches our local logger. Then log via the standard
        # ``logger.info`` path.
        with patch.object(logging, "getLogger", return_value=local_logger):
            poller.setup_logging()
        local_logger.info("sent via handler")
        # The handler formats as ``%(message)s`` so the record reaches
        # the queue as exactly the message string.
        msg = poller._queue.get_nowait()
        assert msg == "sent via handler"
