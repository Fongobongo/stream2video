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


class _FakeMemInfo:
    def __init__(self, rss: int):
        self.rss = rss


class _FakeProc:
    def __init__(self, pid: int, rss: int, children: list[_FakeProc] | None = None):
        self.pid = pid
        self._rss = rss
        self._children = children or []

    def memory_info(self) -> _FakeMemInfo:
        return _FakeMemInfo(self._rss)

    def children(self, recursive: bool = False) -> list[_FakeProc]:
        if not recursive:
            return list(self._children)
        out: list[_FakeProc] = []
        for child in self._children:
            out.append(child)
            out.extend(child.children(recursive=True))
        return out


class _FakePsutil:
    NoSuchProcess = type("NoSuchProcess", (Exception,), {})
    AccessDenied = type("AccessDenied", (Exception,), {})

    def __init__(self, root: _FakeProc):
        self._root = root

    def Process(self, pid: int) -> _FakeProc:
        if pid != self._root.pid:
            raise self.NoSuchProcess(pid)
        return self._root


def test_process_rss_sums_whole_tree(monkeypatch):
    """Benchmark 2026-08 P2: the watched pid is often a shim/launcher
    (~13 MB) whose CHILD is the real ffmpeg (~1.3 GB). Only summing the
    whole tree measures the true footprint."""
    MB = 1024 * 1024
    grandchild = _FakeProc(pid=30, rss=50 * MB)
    child = _FakeProc(pid=20, rss=1200 * MB, children=[grandchild])
    root = _FakeProc(pid=10, rss=13 * MB, children=[child])
    monkeypatch.setattr(memory, "_HAS_PSUTIL", True)
    monkeypatch.setattr(memory, "psutil", _FakePsutil(root))

    rss = memory._process_rss_mb(10)
    assert rss is not None
    assert abs(rss - (13 + 1200 + 50)) < 0.001


def test_process_rss_skips_gone_child(monkeypatch):
    """A child that exits mid-walk must not abort the reading."""

    class _GoneChild(_FakeProc):
        def memory_info(self) -> _FakeMemInfo:
            raise memory.psutil.NoSuchProcess(self.pid)

    MB = 1024 * 1024
    root = _FakeProc(pid=10, rss=13 * MB, children=[_GoneChild(pid=20, rss=0)])
    monkeypatch.setattr(memory, "_HAS_PSUTIL", True)
    monkeypatch.setattr(memory, "psutil", _FakePsutil(root))

    rss = memory._process_rss_mb(10)
    assert rss is not None and abs(rss - 13) < 0.001


def test_process_rss_none_for_gone_root(monkeypatch):
    monkeypatch.setattr(memory, "_HAS_PSUTIL", True)
    monkeypatch.setattr(memory, "psutil", _FakePsutil(_FakeProc(pid=10, rss=0)))

    assert memory._process_rss_mb(999) is None
