"""Tests for stream2video.utils.SubprocessRunner (P2.4).

SubprocessRunner is a context manager that encapsulates the Popen +
stderr drain + cancel/cleanup pattern that was duplicated across
concat/silence/download/waveform. These tests cover the basic contract:
spawning, stderr collection, on_line callback, pipe cleanup, and the
owner-scoped process registration.
"""

from __future__ import annotations

import sys

import pytest

from stream2video.utils import (
    SubprocessRunner,
    get_active_process,
    list_active_owners,
)


def _python(args: list[str]) -> list[str]:
    return [sys.executable, "-c", *args]


class TestSubprocessRunner:
    def test_basic_spawn_and_collect_stderr(self):
        cmd = _python(
            [
                "import sys; sys.stderr.write('hello stderr\\n'); sys.stderr.flush(); "
                "sys.stdout.write('hello stdout\\n'); sys.exit(0)"
            ]
        )
        with SubprocessRunner(cmd, owner="test_basic") as runner:
            assert runner.process is not None
            runner.process.wait(timeout=5)
            runner.drain_stderr()
        assert "hello stderr" in "".join(runner.stderr_lines)

    def test_exit_closes_pipes(self):
        cmd = _python(["import sys; sys.exit(0)"])
        with SubprocessRunner(cmd, owner="test_pipes") as runner:
            proc = runner.process
            assert proc is not None
            proc.wait(timeout=5)
        assert proc.stdout is not None  # attribute still there
        # After exit, the pipes are closed; further reads should raise
        # or return empty.
        assert list_active_owners() == []

    def test_active_process_registered_during_context(self):
        cmd = _python(["import time; time.sleep(0.5)"])
        with SubprocessRunner(cmd, owner="test_register") as runner:
            assert runner.process is not None
            # During the context, the process is registered under owner.
            assert "test_register" in list_active_owners()
            assert get_active_process("test_register") is runner.process
            runner.process.wait(timeout=5)
        # After exit, the registration is cleared.
        assert "test_register" not in list_active_owners()

    def test_on_line_callback_receives_lines(self):
        received: list[str] = []
        cmd = _python(
            [
                "import sys, time; sys.stderr.write('line 1\\n'); sys.stderr.flush(); "
                "sys.stderr.write('line 2\\n'); sys.stderr.flush(); sys.exit(0)"
            ]
        )
        with SubprocessRunner(cmd, owner="test_cb", on_line=received.append) as runner:
            assert runner.process is not None
            runner.process.wait(timeout=5)
            runner.drain_stderr()
        # The drain thread may have appended either full lines (incl. \n)
        # or split them; check both shapes.
        joined = "".join(received)
        assert "line 1" in joined
        assert "line 2" in joined

    def test_on_line_callback_crash_doesnt_break_drain(self):
        def crashy(line: str) -> None:
            raise RuntimeError("intentional crash")

        cmd = _python(
            [
                "import sys; sys.stderr.write('before\\n'); sys.stderr.flush(); "
                "sys.stderr.write('after\\n'); sys.stderr.flush(); sys.exit(0)"
            ]
        )
        with SubprocessRunner(cmd, owner="test_crash", on_line=crashy) as runner:
            assert runner.process is not None
            runner.process.wait(timeout=5)
            runner.drain_stderr()
        # The callback crashed on the first line; the drain thread logs
        # and continues, so 'after' is still in stderr_lines.
        joined = "".join(runner.stderr_lines)
        assert "after" in joined

    def test_file_not_found_raises_with_clear_message(self):
        with (
            pytest.raises(FileNotFoundError, match="not found in PATH"),
            SubprocessRunner(["this_executable_does_not_exist_xyz"]),
        ):
            pass

    def test_non_zero_exit_doesnt_break_cleanup(self):
        cmd = _python(["import sys; sys.stderr.write('fail\\n'); sys.exit(1)"])
        with SubprocessRunner(cmd, owner="test_exit1") as runner:
            assert runner.process is not None
            rc = runner.process.wait(timeout=5)
            runner.drain_stderr()
        assert rc == 1
        assert "fail" in "".join(runner.stderr_lines)

    def test_text_mode(self):
        # In text mode, stdout/stderr are str, not bytes. Useful for
        # yt-dlp where we want to read lines as text directly.
        cmd = _python(["import sys; sys.stderr.write('text mode\\n'); sys.exit(0)"])
        with SubprocessRunner(cmd, owner="test_text", text=True) as runner:
            assert runner.process is not None
            runner.process.wait(timeout=5)
            runner.drain_stderr()
        assert any("text mode" in line for line in runner.stderr_lines)
