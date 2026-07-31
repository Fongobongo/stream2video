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
