"""Tests for memory monitor fallback behaviour."""

from __future__ import annotations

import logging

from stream2video import memory


def test_missing_psutil_warning_is_emitted_once(monkeypatch, caplog):
    monkeypatch.setattr(memory, "_HAS_PSUTIL", False)
    monkeypatch.setattr(memory, "_missing_psutil_warned", False)

    with caplog.at_level(logging.WARNING, logger="stream2video.memory"):
        memory.MemoryMonitor(pid=1, memory_limit_mb=None)
        memory.MemoryMonitor(pid=2, memory_limit_mb=None)

    warnings = [r for r in caplog.records if "psutil not installed" in r.message]
    assert len(warnings) == 1


def _pump_monitor(monkeypatch, monitor, *, rss_values, avail_values):
    """Run ``MemoryMonitor._run`` loop iterations synchronously.

    Replaces the poll primitives with scripted values and makes the
    stop event return False ``len(avail_values)`` times then True, so a
    single ``_run()`` call executes a deterministic number of loop
    bodies without real threads/sleeps.
    """
    rss_iter = iter(rss_values)
    avail_iter = iter(avail_values)
    monkeypatch.setattr(memory, "_process_rss_mb", lambda pid: next(rss_iter, None))
    monkeypatch.setattr(memory, "_available_ram_mb", lambda: next(avail_iter, None))

    waits = {"n": 0}

    class _FakeEvent:
        def wait(self, timeout):
            waits["n"] += 1
            return waits["n"] > len(avail_values)

        def set(self):
            pass

        def clear(self):
            pass

    monitor._stop_event = _FakeEvent()
    monitor._run()


def test_os_reserve_breach_warns_but_never_cancels(monkeypatch, caplog):
    """Available RAM below the reserve must NOT cancel the encode.

    Regression test: previously a single transient dip (browser GC, AV
    scan) killed a multi-minute encode. The reserve is now a warning
    floor — ``cancel_callback`` must never fire for it, regardless of
    how long the breach lasts.
    """
    cancelled = []
    warnings = []
    monitor = memory.MemoryMonitor(
        pid=1234,
        memory_limit_mb=None,  # no RSS budget: reserve is the only door
        memory_reserve_mb=2048.0,
        cancel_callback=lambda: cancelled.append(True) or True,
        on_warning=warnings.append,
        label="seg1 encode",
    )
    with caplog.at_level(logging.WARNING, logger="stream2video.memory"):
        _pump_monitor(
            monkeypatch,
            monitor,
            rss_values=[100.0] * 5,
            avail_values=[1900.0, 1500.0, 500.0, 1900.0, 3000.0],
        )

    assert cancelled == [], "reserve breach must not cancel the encode"
    assert monitor.hard_exceeded is False
    assert monitor.os_reserve_breached is True
    # Warning emitted only once despite the breach spanning 3 polls.
    reserve_warnings = [r for r in caplog.records if "< reserve" in r.getMessage()]
    assert len(reserve_warnings) == 1
    assert reserve_warnings[0].levelno == logging.WARNING
    # on_warning hook fires once too (GUI status line).
    assert len(warnings) == 1


def test_rss_hard_limit_still_cancels(monkeypatch):
    """The per-process RSS budget remains a hard cancel door."""
    cancelled = []
    monitor = memory.MemoryMonitor(
        pid=1234,
        memory_limit_mb=1000.0,
        memory_reserve_mb=100.0,
        cancel_callback=lambda: cancelled.append(True) or True,
    )
    _pump_monitor(
        monkeypatch,
        monitor,
        rss_values=[500.0, 960.0],  # 2nd poll exceeds 95% of 1000MB
        avail_values=[5000.0, 5000.0],
    )

    assert cancelled == [True]
    assert monitor.hard_exceeded is True
    assert monitor.os_reserve_breached is False


def test_rss_soft_limit_warns_without_cancelling(monkeypatch):
    cancelled = []
    monitor = memory.MemoryMonitor(
        pid=1234,
        memory_limit_mb=1000.0,
        memory_reserve_mb=100.0,
        cancel_callback=lambda: cancelled.append(True) or True,
    )
    _pump_monitor(
        monkeypatch,
        monitor,
        rss_values=[500.0, 850.0],  # above 80% soft, below 95% hard
        avail_values=[5000.0, 5000.0],
    )

    assert cancelled == []
    assert monitor.hard_exceeded is False
