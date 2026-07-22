"""Tests for stream2video.encoder_test (extracted from gui.py — Этап 10).

Covers:
  * ``ENCODER_DESCRIPTIONS`` / ``get_encoder_description`` — registry
    + lookup, including the unknown-encoder fallback.
  * ``EncoderTester`` — single-flight semantics, log line shape for
    success / not found / exception, button state transitions through
    the callbacks Protocol.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

from stream2video.encoder_test import (
    ENCODER_DESCRIPTIONS,
    EncoderTester,
    get_encoder_description,
)


class _FakeCallbacks:
    """Minimal implementation of ``EncoderTestCallbacks`` for tests.

    Records every call so the test can assert ordering and content
    without driving the Tk main loop or spawning real ffmpeg.
    """

    def __init__(self) -> None:
        self.logs: list[str] = []
        self.scheduled_on_main: list[tuple[int, Callable[..., Any]]] = []
        self.scheduled_after: list[tuple[int, Callable[..., Any]]] = []
        self.button_states: list[bool] = []  # True = running, False = idle
        self._lock = threading.Lock()

    def log(self, message: str) -> None:
        self.logs.append(message)

    def schedule_on_main(self, ms: int, func: Callable[..., Any]) -> None:
        self.scheduled_on_main.append((ms, func))
        # Drive the queued callback immediately so the test sees the
        # resulting log line without needing a Tk main loop.
        func()

    def schedule_after(self, ms: int, func: Callable[..., Any]) -> None:
        self.scheduled_after.append((ms, func))
        func()

    def set_test_button_state(self, *, running: bool) -> None:
        with self._lock:
            self.button_states.append(running)


class TestEncoderDescriptions:
    def test_known_encoders_present(self):
        # The four encoders the GUI's combobox enumerates must be in
        # the dict — otherwise the user gets an empty description and
        # no clue which encoder they're picking.
        assert set(ENCODER_DESCRIPTIONS.keys()) == {
            "h264_nvenc",
            "h264_amf",
            "h264_mf",
            "libx264",
        }

    def test_descriptions_are_human_readable(self):
        # Every description should be a non-empty string. A blank
        # description would render as "" in the GUI's lbl_encoder_desc
        # which the user would read as "no encoder picked."
        for key, text in ENCODER_DESCRIPTIONS.items():
            assert text, f"description for {key!r} must be non-empty"

    def test_get_encoder_description_returns_value(self):
        assert get_encoder_description("libx264") == ENCODER_DESCRIPTIONS["libx264"]

    def test_get_encoder_description_unknown_returns_empty(self):
        # Unknown encoder (rare: a user-edited config.json) → empty
        # string, NOT an exception. GUI keeps its current label.
        assert get_encoder_description("nonexistent_encoder") == ""


class TestEncoderTester:
    def test_test_marks_running_and_restores_on_success(self):
        cb = _FakeCallbacks()
        tester = EncoderTester(cb)

        with patch("stream2video.concat.check_encoder", return_value=True) as mock:
            tester.test("libx264")
            # Background thread starts; wait long enough for it to run.
            # Using a tiny wait + retry so on slow CI we still catch up
            # instead of a fixed 3s sleep that's both flaky and slow.
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if not tester.running and cb.button_states[-1] is False:
                    break
                time.sleep(0.01)

            mock.assert_called_once_with("libx264")
            assert "Testing encoder: libx264 ..." in cb.logs
            assert any("[OK]" in line for line in cb.logs)
            # Button flips twice: True when test starts, False on
            # finally restoration.
            assert cb.button_states[0] is True
            assert cb.button_states[-1] is False

    def test_test_logs_no_when_check_returns_false(self):
        cb = _FakeCallbacks()
        tester = EncoderTester(cb)

        with patch("stream2video.concat.check_encoder", return_value=False):
            tester.test("h264_amf")
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if not tester.running and cb.button_states[-1] is False:
                    break
                time.sleep(0.01)
            assert any("NO" in line for line in cb.logs)

    def test_test_logs_ffmpeg_not_found(self):
        cb = _FakeCallbacks()
        tester = EncoderTester(cb)

        with patch("stream2video.concat.check_encoder", side_effect=FileNotFoundError):
            tester.test("libx264")
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if not tester.running and cb.button_states[-1] is False:
                    break
                time.sleep(0.01)
            assert any("ffmpeg not found in PATH" in line for line in cb.logs)

    def test_test_logs_error_on_unexpected_exception(self):
        cb = _FakeCallbacks()
        tester = EncoderTester(cb)

        with patch("stream2video.concat.check_encoder", side_effect=RuntimeError("boom")):
            tester.test("h264_nvenc")
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if not tester.running and cb.button_states[-1] is False:
                    break
                time.sleep(0.01)
            assert any("ERROR (boom)" in line for line in cb.logs)
            # Button state still restored even on unexpected error.
            assert cb.button_states[-1] is False

    def test_single_flight_logs_warning_for_second_request(self):
        cb = _FakeCallbacks()
        tester = EncoderTester(cb)

        # Use an Event to keep the first test thread blocked so the
        # second ``test()`` call arrives while the first is still
        # ``running``.
        unblock = threading.Event()

        def _hold(*_args: object, **_kwargs: object) -> bool:
            unblock.wait(timeout=2.0)
            return True

        with patch("stream2video.concat.check_encoder", side_effect=_hold):
            tester.test("libx264")
            # Tiny wait for the worker to enter ``_running=True``.
            time.sleep(0.05)
            assert tester.running
            tester.test("libx264")
            assert "Test already running" in cb.logs
            unblock.set()
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if not tester.running and cb.button_states[-1] is False:
                    break
                time.sleep(0.01)
        assert cb.button_states[-1] is False
