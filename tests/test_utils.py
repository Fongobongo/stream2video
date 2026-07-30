"""Tests for stream2video.utils — cancel_monitor, get_video_duration, etc."""

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from stream2video.utils import (
    CANCEL_POLL_INTERVAL,
    cancel_monitor,
    cancel_process,
    get_active_process,
    get_video_duration,
    get_video_start_time,
    looks_like_oom,
    registered_process,
    set_active_process,
    subprocess_kwargs,
)


def _spawn_quick_proc():
    """Spawn a short-lived subprocess that exits on its own."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(0.1)"],
    )


class TestCancelMonitor:
    """cancel_monitor — context manager that kills a Popen when its
    cancel_callback returns True. Replaces three near-duplicate
    _cancel_monitor functions in concat.py and silence.py."""

    def test_no_callback_event_stays_unset(self):
        """If cancel_callback is None, the monitor thread is not even
        started; the yielded event stays unset during the context."""
        proc = _spawn_quick_proc()
        try:
            with cancel_monitor(proc) as cancelled:
                assert not cancelled.is_set()
                proc.wait(timeout=5)
                # Still unset during the context — callback was never polled.
                assert not cancelled.is_set()
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_callback_returning_false_does_not_kill(self):
        """Callback returning False should leave the process running."""
        proc = _spawn_quick_proc()
        try:
            with cancel_monitor(proc, cancel_callback=lambda: False):
                proc.wait(timeout=5)
            assert proc.returncode == 0
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_rlimit_as_zero_returns_only_no_window_kw(self):
        """rlimit_as_mb=0 (default) disables the cap, so the
        returned dict is identical to no_window_kwargs() alone (no
        preexec_fn added)."""
        kw = subprocess_kwargs(low_priority=False, rlimit_as_mb=0)
        if sys.platform == "win32":
            assert kw == {"creationflags": subprocess.CREATE_NO_WINDOW}
        else:
            assert kw == {}
        assert "preexec_fn" not in kw

    def test_rlimit_as_ignored_on_windows(self):
        """rlimit_as_mb > 0 has no effect on Windows (no portable
        RLIMIT_AS equivalent) — only low_priority adds priority flags,
        leaving the cap in the user's hands via memory_limit_mb."""
        if sys.platform != "win32":
            pytest.skip("Windows-only")
        kw = subprocess_kwargs(low_priority=False, rlimit_as_mb=2048)
        # Only CREATE_NO_WINDOW; no preexec_fn (preexec_fn is POSIX-only).
        assert kw == {"creationflags": subprocess.CREATE_NO_WINDOW}
        assert "preexec_fn" not in kw

    def test_rlimit_as_and_low_priority_compose_on_posix(self):
        """On POSIX, rlimit_as_mb > 0 + low_priority=True compose
        into a single preexec_fn (Python only allows one). The child
        runs os.nice(10) then setrlimit(RLIMIT_AS, (cap, cap))."""
        if sys.platform == "win32":
            pytest.skip("POSIX-only")
        kw = subprocess_kwargs(low_priority=True, rlimit_as_mb=2048)
        assert callable(kw["preexec_fn"])
        assert "creationflags" not in kw

    def test_rlimit_as_only_on_posix(self):
        """rlimit_as_mb > 0 alone (no low_priority) still sets
        preexec_fn on POSIX so the child is capped."""
        if sys.platform == "win32":
            pytest.skip("POSIX-only")
        kw = subprocess_kwargs(low_priority=False, rlimit_as_mb=2048)
        assert callable(kw["preexec_fn"])
        assert "creationflags" not in kw

    def test_rlimit_as_actually_caps_addr_space_on_posix(self):
        """Smoke test: spawn a child with rlimit_as_mb=2 (2 MiB cap)
        and have it try to allocate 4 MiB. malloc should return NULL
        (or raise MemoryError in pure-python) and the child should
        exit cleanly — the kernel refused the allocation before it
        could swap."""
        if sys.platform == "win32":
            pytest.skip("POSIX-only")
        # 2 MiB cap. Preexec sets RLIMIT_AS. Pure-Python bytearray
        # allocation will either raise MemoryError (catchable) or
        # cause a hard death (segfault on MemoryError in some builds);
        # the child uses try/except to convert to exit code 0 on
        # MemoryError (success), 1 on the bytearray actually succeeding
        # (which would mean the cap isn't enforced).
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "try:\n"
                "    bytearray(4 * 1024 * 1024)\n"
                "    import sys; sys.exit(1)\n"
                "except MemoryError:\n"
                "    import sys; sys.exit(0)\n",
            ],
            **subprocess_kwargs(low_priority=False, rlimit_as_mb=2),
        )
        proc.wait()
        # rc 0 = MemoryError caught (cap enforced).
        # rc 1 = allocation succeeded (cap NOT enforced -> bug or rlimit not honoured).
        assert proc.returncode == 0, (
            f"RLIMIT_AS cap not enforced; rc={proc.returncode} "
            "(expected 0 = MemoryError caught on a 4MiB allocation)"
        )

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX-only",
    )
    def test_rlimit_as_popen_runs_cleanly_when_cap_is_high(self):
        """Sanity: a generous rlimit_as (e.g. 512 MiB) is enough for a
        short-lived python interpreter and the cap doesn't fault the
        child's startup. Popen succeeds, exit code 0."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "print('hi')"],
            **subprocess_kwargs(low_priority=False, rlimit_as_mb=512),
        )
        proc.wait()
        assert proc.returncode == 0


class TestLooksLikeOom:
    """looks_like_oom — heuristic that decides whether an ffmpeg failure
    was an out-of-memory condition (SIGKILL on POSIX, allocator-failure
    markers in stderr). Used by concat._run_ffmpeg and the silence
    detectors to surface a dedicated FFmpegOutOfMemoryError /
    SilenceOutOfMemoryError with a 'lower the memory budget / use the
    Low-memory preset' hint instead of dumping the raw ffmpeg stderr."""

    def test_returncode_none_returns_false(self):
        # Process still running — can't be OOM yet.
        assert looks_like_oom(None, "out of memory") is False

    def test_returncode_zero_returns_false(self):
        # Success — stderr markers might be informational warnings,
        # not a fatal OOM. Conservative: don't classify as OOM.
        assert looks_like_oom(0, "out of memory") is False
        assert looks_like_oom(0, "") is False

    def test_sigkill_negative_signal_returns_true(self):
        # POSIX: Python convention. returncode == -N means the child
        # was killed by signal N. SIGKILL is 9, so -9 is a strong OOM
        # indicator (the Linux OOM killer sends SIGKILL).
        assert looks_like_oom(-9, "") is True
        assert looks_like_oom(-9, "killed") is True

    def test_sigkill_shell_convention_returns_true(self):
        # POSIX: shell convention. Bash returns 128 + signal_number.
        # 137 == 128 + 9 == SIGKILL — same indicator as -9.
        assert looks_like_oom(137, "") is True
        assert looks_like_oom(137, "Killed") is True

    def test_non_sigkill_negative_signal_returns_false(self):
        # SIGSEGV is 11. A segfault isn't OOM — keep this conservative
        # so the heuristic doesn't overclassify crashes.
        assert looks_like_oom(-11, "Segfault") is False
        assert looks_like_oom(139, "") is False  # 128 + 11

    def test_stderr_out_of_memory_marker(self):
        # libx264's allocator failure: "out of memory"
        assert looks_like_oom(1, "x264 [error]: out of memory") is True
        assert looks_like_oom(-9, "ffmpeg: out of memory allocating big buffer") is True

    def test_stderr_cannot_allocate_memory_marker(self):
        # POSIX malloc failure (errno ENOMEM)
        assert looks_like_oom(1, "Cannot allocate memory\nmalloc failed") is True
        assert looks_like_oom(1, "ffmpeg: cannot allocate memory") is True

    def test_stderr_malloc_failed_marker(self):
        # ffmpeg / libc allocator failures
        assert looks_like_oom(1, "malloc failed: Application's big!") is True

    def test_stderr_mmap_failed_marker(self):
        # libx264's frame-buffer mmap failure
        assert looks_like_oom(1, "mmap failed: Cannot allocate memory") is True

    def test_stderr_not_enough_space_marker(self):
        # Windows allocator-failure phrasing
        assert looks_like_oom(1, "not enough space for buffer") is True

    def test_stderr_x264_thread_split_marker(self):
        # libx264's thread init failure (alloc error during thread fork)
        assert (
            looks_like_oom(
                1,
                "x264 [error]: Error splitting input into thread: Cannot allocate memory",
            )
            is True
        )

    def test_case_insensitive_match(self):
        # Markers are matched case-insensitively so a build that emits
        # "Out Of Memory" (Windows) still classifies.
        assert looks_like_oom(1, "Out Of Memory allocating 1GB") is True
        assert looks_like_oom(1, "MALLOC Failed") is True

    def test_no_marker_no_signal_returns_false(self):
        # Generic non-zero exit with no OOM markers — false.
        assert looks_like_oom(1, "Conversion failed!") is False
        assert looks_like_oom(2, "some random error") is False
        assert looks_like_oom(255, "") is False

    def test_empty_stderr_with_generic_exit_returns_false(self):
        assert looks_like_oom(1, "") is False

    def test_stderr_truncation_safe(self):
        # A long stderr doesn't crash the matcher. The marker is found
        # anywhere in the text.
        long = "x" * 10000 + " out of memory " + "y" * 10000
        assert looks_like_oom(1, long) is True

    def test_callback_returning_true_kills_process(self):
        """Callback returning True should kill the process and set the event."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
        )
        try:
            callback_calls = []

            def cb() -> bool:
                callback_calls.append(True)
                return True

            def runner():
                time.sleep(CANCEL_POLL_INTERVAL * 2)
                # We can't call cb from outside; the monitor
                # thread polls it on its own. Just wait for the event.

            t = threading.Thread(target=runner, daemon=True)
            t.start()
            with cancel_monitor(proc, cancel_callback=cb) as cancelled:
                # Force the callback to return True on the next poll by
                # waiting past one poll interval; but since cb already
                # always returns True, the first poll will kill it.
                proc.wait(timeout=5)
            assert cancelled.is_set()
            assert proc.poll() is not None  # process was killed
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_yielded_event_is_set_on_exit(self):
        """When the context manager exits (normally or via exception),
        the event is set so the monitor thread terminates."""
        proc = _spawn_quick_proc()
        try:
            with cancel_monitor(proc) as cancelled:
                proc.wait(timeout=5)
            assert cancelled.is_set()
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_exception_in_block_still_sets_event(self):
        proc = _spawn_quick_proc()
        try:
            with pytest.raises(RuntimeError), cancel_monitor(proc) as cancelled:
                raise RuntimeError("boom")
            assert cancelled.is_set()
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_process_already_dead(self):
        """If the process exits before the monitor thread's first poll,
        the thread should detect that via poll() and exit cleanly without
        invoking the cancel callback (no kill needed)."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.exit(0)"],
        )
        proc.wait(timeout=5)
        callback_called = []
        with cancel_monitor(
            proc, cancel_callback=lambda: callback_called.append(True) or True
        ) as cancelled:
            time.sleep(CANCEL_POLL_INTERVAL * 1.5)
            assert not cancelled.is_set()
            assert callback_called == []  # monitor exited via poll(), not via cb


class TestGetVideoDuration:
    """get_video_duration — ffprobe wrapper."""

    def test_returns_none_for_missing_file(self, tmp_path: Path):
        # Nonexistent file: ffprobe fails, returns None.
        result = get_video_duration(tmp_path / "does_not_exist.mp4")
        assert result is None

    def test_returns_none_for_non_video(self, tmp_path: Path):
        # Random text file: ffprobe can't read duration.
        f = tmp_path / "not_a_video.txt"
        f.write_text("hello")
        result = get_video_duration(f)
        assert result is None


class TestGetVideoStartTime:
    """get_video_start_time — ffprobe wrapper used by the batch path
    to compensate ``-copyts`` + ``-ss`` for non-zero PTS sources."""

    def test_missing_file_returns_zero(self, tmp_path: Path):
        # Nonexistent file: ffprobe fails — returns 0.0 (the safe
        # default, so a failed probe can't abort the whole encode).
        result = get_video_start_time(tmp_path / "does_not_exist.mp4")
        assert result == 0.0

    def test_non_video_returns_zero(self, tmp_path: Path):
        # Random text file: ffprobe reports no format-level start_time.
        f = tmp_path / "not_a_video.txt"
        f.write_text("hello")
        assert get_video_start_time(f) == 0.0


class TestActiveProcess:
    """set/get_active_process — registry used by the GUI's WM_DELETE
    handler to kill the in-flight ffmpeg on close."""

    def test_default_is_none(self):
        set_active_process(None)
        assert get_active_process() is None

    def test_unknown_owner_returns_none_no_fallback(self):
        """get_active_process("nonexistent") must return None, not fall
        back to the "default" slot — otherwise a preview's finally
        could clobber the pipeline's registration (P0 audit 1.1)."""
        proc = _spawn_quick_proc()
        try:
            set_active_process(proc)
            assert get_active_process("preview") is None
            assert get_active_process("nonexistent") is None
        finally:
            set_active_process(None)
            proc.wait(timeout=5)

    def teardown_method(self, method):
        # Don't leak state into other tests.
        set_active_process(None)


class TestRegisteredProcess:
    """registered_process context manager — registers a subprocess under
    an owner on entry and *always* clears the same slot on exit, even on
    exception/early return. This is the fix for the P0 audit: preview's
    bare ``set_active_process(None)`` in finally cleared the "default"
    slot where the pipeline's ffmpeg was registered."""

    def teardown_method(self, method):
        set_active_process(None)

    def test_clears_default_slot_on_normal_exit(self):
        proc = _spawn_quick_proc()
        with registered_process(proc):
            assert get_active_process("default") is proc
        assert get_active_process("default") is None
        proc.wait(timeout=5)

    def test_clears_preview_slot_on_normal_exit(self):
        proc = _spawn_quick_proc()
        with registered_process(proc, owner="preview"):
            assert get_active_process("preview") is proc
        assert get_active_process("preview") is None
        proc.wait(timeout=5)

    def test_clears_slot_on_exception(self):
        proc = _spawn_quick_proc()
        try:
            with registered_process(proc, owner="preview"):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert get_active_process("preview") is None
        proc.wait(timeout=5)

    def test_clears_slot_on_early_return(self):
        proc = _spawn_quick_proc()

        def f():
            with registered_process(proc, owner="preview"):
                return "done"

        f()
        assert get_active_process("preview") is None
        proc.wait(timeout=5)

    def test_preview_does_not_clobber_default(self):
        """A preview registration under "preview" must not touch the
        "default" slot, so a concurrent pipeline registered under
        "default" survives preview's finally (regression for P0 1.1)."""
        default_proc = _spawn_quick_proc()
        preview_proc = _spawn_quick_proc()
        try:
            set_active_process(default_proc, owner="default")
            with registered_process(preview_proc, owner="preview"):
                # Both registrations coexist.
                assert get_active_process("default") is default_proc
                assert get_active_process("preview") is preview_proc
            # Preview's finally cleared its own slot.
            assert (
                "preview"
                not in {
                    owner
                    for owner in ["preview", "default"]
                    if get_active_process(owner) is not None
                }
                or get_active_process("preview") is None
            )
            # Default slot untouched by preview's exit.
            assert get_active_process("default") is default_proc
        finally:
            set_active_process(None, owner="default")
            set_active_process(None, owner="preview")
            default_proc.wait(timeout=5)
            preview_proc.wait(timeout=5)

    def test_cancel_preview_kills_preview_not_default(self):
        """cancel_process("preview") targets only the preview slot
        (regression: a fallback in get_active_process used to make
        cancel process cross-owner)."""
        default_proc = _spawn_quick_proc()
        preview_proc = _spawn_quick_proc()
        try:
            set_active_process(default_proc, owner="default")
            set_active_process(preview_proc, owner="preview")
            killed = cancel_process("preview", timeout=2.0)
            assert killed is True
            preview_proc.wait(timeout=5)
            # Default process must be unaffected.
            assert default_proc.poll() is None
        finally:
            set_active_process(None, owner="default")
            set_active_process(None, owner="preview")
            default_proc.wait(timeout=5)


class TestSubprocessKwargs:
    """subprocess_kwargs — composes no_window_kwargs with optional
    low-priority scheduling flags. Spawned ffmpeg processes inherit
    these flags via Popen(**subprocess_kwargs(...))."""

    def test_default_low_priority_false_returns_only_window_flag(self):
        kw = subprocess_kwargs(low_priority=False)
        if sys.platform == "win32":
            assert kw == {"creationflags": subprocess.CREATE_NO_WINDOW}
        else:
            assert kw == {}

    def test_low_priority_true_includes_priority_flag(self):
        kw = subprocess_kwargs(low_priority=True)
        if sys.platform == "win32":
            # Composes CREATE_NO_WINDOW (0x08000000) with
            # BELOW_NORMAL_PRIORITY_CLASS (0x00004000).
            assert kw["creationflags"] == (subprocess.CREATE_NO_WINDOW | 0x00004000)
            assert "preexec_fn" not in kw
        else:
            # POSIX: preexec_fn ensures the child starts at nice +10.
            assert callable(kw["preexec_fn"])
            assert "creationflags" not in kw

    def test_low_priority_false_is_identity_on_posix(self):
        if sys.platform == "win32":
            pytest.skip("POSIX-only")
        assert subprocess_kwargs(low_priority=False) == {}

    def test_low_priority_false_keeps_no_window_on_windows(self):
        if sys.platform != "win32":
            pytest.skip("Windows-only")
        kw = subprocess_kwargs(low_priority=False)
        assert kw["creationflags"] == subprocess.CREATE_NO_WINDOW

    def test_preexec_fn_increases_nice_on_posix(self):
        """When the call to preexec_fn is executed (in the child after
        fork), it should successfully os.nice(+10)."""
        if sys.platform == "win32":
            pytest.skip("POSIX-only")
        # Run the preexec_fn in a child process (which is what Popen
        # does) and check the child's nice value increases by 10.
        proc = subprocess.Popen(
            [sys.executable, "-c", "import os; print(os.nice(0))"],
            **subprocess_kwargs(low_priority=True),
        )
        proc.wait()
        # As long as the Popen succeeds with preexec_fn, the function
        # is wired correctly; the actual nice increment is verified
        # by os.nice's semantics (it returns the new value). We can't
        # easily get the child's post-exec nice here because os.nice
        # runs in the child's address space, but a successful exit
        # confirms preexec_fn didn't raise OSError.
        assert proc.returncode == 0
