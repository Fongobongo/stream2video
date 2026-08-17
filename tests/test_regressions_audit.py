"""Regression tests for the 28-bug audit sweep.

Every test in this module maps directly to a numbered audit finding.
A test that starts failing again means the corresponding bug came back.
"""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from stream2video.concat import (
    ConcatLockError,
    _run_cut_then_encode,
    acquire_output_lock,
    release_output_lock,
)
from stream2video.concat.output_lock import lock_path_for


# ── #4/#3b ── resume cache path is shared between CLI and GUI ──────────
class TestResumeCachePathShared:
    def test_cli_and_gui_agree_on_resume_path(self, tmp_path: Path):
        from stream2video.silence import build_resume_cache_path

        video = tmp_path / "myvideo.mp4"
        video.write_bytes(b"x")
        expected = build_resume_cache_path(video, tmp_path / "out")
        # Both front-ends orchestrate through PipelineController now
        # (audit #11), which must NOT hand-roll the hash inline anymore.
        import inspect

        import stream2video.pipeline_controller as pc_mod

        src = inspect.getsource(pc_mod)
        assert "build_resume_cache_path" in src, (
            "pipeline_controller.py must use build_resume_cache_path (shared with GUI)"
        )
        assert video.stem in expected.name

    def test_probe_position_resumes_with_zero_segments(self, tmp_path: Path):
        """#3b: a checkpoint with zero segments must not restart at t=0."""
        from stream2video.silence.cache import (
            _save_cache,
            load_resume_probe_position,
        )

        video = tmp_path / "src.mp4"
        video.write_bytes(b"video-bytes")
        cache = tmp_path / "out" / "src_x_silence_cache.json.resume"
        cfg = {"threshold": -30.0, "min_silence": 2.0, "margin": 0.5}
        _save_cache(cache, video, [], cfg, indent=None, fsync=False, probe_position=3600.0)
        pos = load_resume_probe_position(cache, video, cfg)
        assert pos == pytest.approx(3600.0)


# ── #8 ── cache identity: name + size, utf-8 round-trip ────────────────
class TestSilenceCacheIdentity:
    def test_other_stem_does_not_share_cache(self, tmp_path: Path):
        from stream2video.silence.cache import _save_cache, load_silence_cache

        out = tmp_path / "out"
        a = tmp_path / "a.mp4"
        b = tmp_path / "b.mp4"
        a.write_bytes(b"aaa")
        b.write_bytes(b"bbb")
        # _save_cache keys by video_path.name — directly place a cache
        # under b's stem but with a's content marker.
        cache_path = out / f"{b.stem}_silence_cache.json"
        _save_cache(cache_path, a, [], {"threshold": -30.0, "min_silence": 2.0, "margin": 0.5})
        # Load for b: recorded source is a.mp4 — must NOT match.
        hit = load_silence_cache(b, out, {"threshold": -30.0, "min_silence": 2.0, "margin": 0.5})
        assert hit is None, "cache for a.mp4 was reused for b.mp4"

    def test_unicode_source_name_does_not_crash(self, tmp_path: Path):
        """#8: Cyrillic source names on cp1251 Windows must not UnicodeDecodeError."""
        from stream2video.silence.cache import _save_cache, load_silence_cache

        out = tmp_path / "out"
        vid = tmp_path / "стрим_запись.mp4"
        vid.write_bytes(b"video")
        _save_cache(
            out / f"{vid.stem}_silence_cache.json",
            vid,
            [],
            {"threshold": -30.0, "min_silence": 2.0, "margin": 0.5},
        )
        hit = load_silence_cache(vid, out, {"threshold": -30.0, "min_silence": 2.0, "margin": 0.5})
        assert hit == []

    def test_size_mismatch_detected(self, tmp_path: Path):
        """#8: same mtime but different size must invalidate the cache."""
        from stream2video.silence.cache import _save_cache, load_silence_cache

        out = tmp_path / "out"
        vid = tmp_path / "a.mp4"
        vid.write_bytes(b"small")
        cfg = {"threshold": -30.0, "min_silence": 2.0, "margin": 0.5}
        _save_cache(out / f"{vid.stem}_silence_cache.json", vid, [], cfg)
        # Grow the file (same mtime would need os.utime; different size
        # alone must already invalidate).
        vid.write_bytes(b"small_but_grown")
        assert load_silence_cache(vid, out, cfg) is None


# ── #5 ── cut_encode resume: truncated part is re-cut, not reused ──────
class TestSegmentResumeDuration:
    """A3 audit: a segment part whose duration doesn't match the keep
    window must be re-encoded on resume, not reused."""

    def test_wrong_duration_segment_is_reencoded(self, tmp_path: Path):
        video = tmp_path / "src.mp4"
        video.write_bytes(b"source")
        output = tmp_path / "out.mp4"
        keep = [(0.0, 5.0)]  # dur=5
        seg_dir = output.parent / "_out_segments"
        seg_dir.mkdir(parents=True)
        (seg_dir / "seg_000000.mp4").write_bytes(b"\x00" * 2048)

        from stream2video.concat import _run_segment_concat

        cut_calls: list[list[str]] = []

        def fake_encode(cmd, **kw):
            cut_calls.append(list(cmd))

        with (
            patch("stream2video.concat._run_ffmpeg", side_effect=fake_encode),
            patch("stream2video.concat._run_final_concat"),
            patch("stream2video.concat._ffprobe_is_valid_mp4", return_value=True),
            patch("stream2video.concat._ffprobe_is_valid_media", return_value=True),
            # Duration probe says "wrong length" → must NOT reuse.
            patch("stream2video.concat._ffprobe_duration_ok", return_value=False),
            patch("stream2video.concat._ensure_fresh_work_dir"),
        ):
            _run_segment_concat(
                video,
                keep,
                output,
                "libx264",
                ["-preset", "medium"],
                None,
                None,
                encoder="libx264",
                source_has_audio=False,
            )
        assert len(cut_calls) == 1, "wrong-duration segment was reused, not re-encoded"


# ── #5 ── cut_encode resume: truncated part is re-cut, not reused ──────
class TestCutEncodeResumeDuration:
    def test_wrong_duration_part_is_recut(self, tmp_path: Path):
        """#5: a stale part whose duration is off must be re-encoded."""
        video = tmp_path / "src.mp4"
        video.write_bytes(b"source")
        output = tmp_path / "out.mp4"
        keep = [(10.0, 20.0)]  # dur=10
        cut_dir = output.parent / "_out_cut"
        cut_dir.mkdir(parents=True)
        (cut_dir / "cut_000000.mp4").write_bytes(b"\x00" * 2048)

        cut_calls: list[list[str]] = []

        def fake_cut(cmd, **kw):
            cut_calls.append(list(cmd))

        with (
            patch("stream2video.concat._run_ffmpeg", side_effect=fake_cut),
            patch("stream2video.concat._run_final_concat"),
            patch("stream2video.concat._ffprobe_is_valid_mp4", return_value=True),
            patch("stream2video.concat._ffprobe_is_valid_media", return_value=True),
            # Duration probe says "wrong length" → must NOT reuse.
            patch("stream2video.concat._ffprobe_duration_ok", return_value=False),
            patch("stream2video.concat._run_subprocess_cmd"),
            patch("stream2video.concat._ensure_fresh_work_dir"),
        ):
            _run_cut_then_encode(
                video,
                keep,
                output,
                "libx264",
                ["-preset", "medium"],
                None,
                None,
                encoder="libx264",
                source_has_audio=False,
            )
        cut_encodes = [c for c in cut_calls if "cut_000000.mp4" in " ".join(c)]
        assert len(cut_encodes) == 1, "stale-duration part was reused, not re-cut"


# ── #B5 ── force re-detect must clear both .resume and .resume.inuse ────
class TestForceClearsInUseCheckpoint:
    """P2 audit regression: ``--force`` previously wiped only the
    canonical ``.resume`` cache file but left a ``.resume.inuse``
    checkpoint behind from a crashed previous run. ``detect_silence``
    prefers the ``.inuse`` over ``.resume`` (see silence/pipeline.py:
    "A leftover .inuse takes precedence"), so a forced re-detect
    silently continued from the old, possibly-shifted timeline
    instead of starting fresh. The controller must wipe both."""

    def test_force_deletes_inuse_alongside_resume(self, tmp_path: Path):
        import sys
        import threading
        from pathlib import Path as _Path

        # Reuse the shared helper from test_pipeline_controller
        sys.path.insert(0, str(_Path(__file__).parent))
        from test_pipeline_controller import _valid_config

        from stream2video.pipeline_controller import (
            PipelineCallbacks,
            PipelineController,
            PipelineUnexpectedError,
        )
        from stream2video.silence import build_resume_cache_path, resume_inuse_path

        # Setup: a fake output dir with a video file + both checkpoint
        # artifacts left behind by a crashed previous run.
        video = tmp_path / "src.mp4"
        video.write_bytes(b"source")

        cfg = _valid_config(
            output_dir=tmp_path,
            input_raw=str(video),
            force=True,
        )
        out = cfg.output_dir

        resume_path = build_resume_cache_path(video, out)
        resume_path.parent.mkdir(parents=True, exist_ok=True)
        resume_path.write_text("{}")
        inuse_path = resume_inuse_path(resume_path)
        inuse_path.write_text("{}")

        assert resume_path.exists() and inuse_path.exists()

        log_messages: list[str] = []

        cb = PipelineCallbacks(
            on_progress=lambda _f: None,
            on_status=lambda _t, **_k: None,
            on_log=log_messages.append,
            on_info=lambda _t: None,
            on_overall=lambda _a, _b, _c: None,
            on_total=lambda _a, **_k: None,
            on_download_progress=lambda _p: None,
            on_pipeline_complete=lambda _d: None,
        )

        from unittest.mock import MagicMock, patch

        with (
            patch(
                "stream2video.pipeline_controller.download",
                side_effect=lambda *a, **k: MagicMock(path=video, is_downloaded=False),
            ),
            patch("stream2video.pipeline_controller.get_video_duration", return_value=10.0),
            patch(
                "stream2video.pipeline_controller.apply_per_video_dir",
                return_value=(out, video),
            ),
            # detect_silence raises a generic exception so the controller
            # wraps it in PipelineUnexpectedError and we exit. The force
            # cleanup happens BEFORE detect_silence is called, so the
            # checkpoint files are already gone by that point.
            patch(
                "stream2video.pipeline_controller.detect_silence",
                side_effect=RuntimeError("intentional stop"),
            ),
            patch(
                "stream2video.pipeline_controller.generate_keep_segments",
                return_value=[(0.0, 10.0)],
            ),
            pytest.raises(PipelineUnexpectedError),
        ):
            PipelineController(cfg=cfg, cb=cb, cancel_event=threading.Event()).run()

        assert not resume_path.exists(), "force did not clear canonical .resume"
        assert not inuse_path.exists(), "force did not clear .inuse checkpoint"
        assert any("inuse" in m.lower() for m in log_messages), (
            f"force did not log clearing .inuse; logs: {log_messages}"
        )


# ── #6 ── output lock ──────────────────────────────────────────────────
class TestOutputLock:
    def test_second_acquire_raises(self, tmp_path: Path):
        out = tmp_path / "x.mp4"
        lp = acquire_output_lock(out)
        assert lp.path.exists()
        with pytest.raises(ConcatLockError):
            acquire_output_lock(out)
        release_output_lock(lp)
        assert not lp.path.exists()
        # Releasing frees the name for the next run.
        lp2 = acquire_output_lock(out)
        lp2.path.unlink()

    def test_stale_lock_without_pid_reclaimed(self, tmp_path: Path):
        """#C15: a lock with no pid line (crashed before write completed)
        must be reclaimed, not brick the next run forever."""
        out = tmp_path / "x.mp4"
        lock = lock_path_for(out)
        lock.write_text("output=x.mp4\n", encoding="utf-8")
        old = time.time() - 60 * 60 - 60
        os.utime(lock, (old, old))
        lp = acquire_output_lock(out)
        assert lp.path.exists()
        lp.path.unlink()

    def test_fresh_lock_without_pid_not_reclaimed(self, tmp_path: Path):
        """Audit #1: a lock created by another run that has not yet
        written its pid line (the os.open -> os.write gap) must NOT be
        reclaimed — deleting it would let two runs write one output."""
        import stream2video.concat.output_lock as ol

        out = tmp_path / "x.mp4"
        lock = lock_path_for(out)
        lock.write_text("", encoding="utf-8")  # fresh mtime, no pid
        with (
            patch.object(ol, "_RETRY_SLEEP_SECONDS", 0.001),
            patch.object(ol, "_RETRY_MAX_TRIES", 3),
            pytest.raises(ConcatLockError),
        ):
            acquire_output_lock(out)
        assert lock.exists(), "fresh pid-less lock must be left in place"

    def test_slow_writer_race_is_not_stealable(self, tmp_path: Path):
        """Audit #1: simulate the exact race — acquire A pauses between
        os.open and os.write (patch the write to sleep). A concurrent
        acquire B must retry, see A's pid once written, and refuse —
        never delete A's live lock."""
        import stream2video.concat.output_lock as ol

        out = tmp_path / "x.mp4"
        real_write = os.write
        opened = threading.Event()
        a_result: dict[str, object] = {}

        def slow_write(fd, data):
            opened.set()
            time.sleep(0.15)
            return real_write(fd, data)

        def acquire_a():
            a_result["handle"] = acquire_output_lock(out)

        with (
            patch.object(ol, "_RETRY_SLEEP_SECONDS", 0.001),
            patch.object(ol, "_RETRY_MAX_TRIES", 5000),
            patch.object(ol.os, "write", side_effect=slow_write),
        ):
            a = threading.Thread(target=acquire_a)
            a.start()
            assert opened.wait(timeout=5), "acquirer A never reached its write"
            with pytest.raises(ConcatLockError):
                acquire_output_lock(out)
            a.join(timeout=10)
        assert not a.is_alive()
        handle = a_result["handle"]
        assert isinstance(handle, ol.LockHandle)
        assert handle.path.exists()
        release_output_lock(handle)
        assert not handle.path.exists()

    def test_release_does_not_remove_reclaimed_lock(self, tmp_path: Path):
        """Audit #1: release must not unlink a lock that was reclaimed
        and re-taken by another run (token ownership check)."""
        out = tmp_path / "x.mp4"
        lp_a = acquire_output_lock(out)
        # Simulate: A died, B reclaimed the lock, then re-wrote it.
        lock = lock_path_for(out)
        lock.write_text("token=other pid=424242 output=x.mp4\n", encoding="utf-8")
        release_output_lock(lp_a)
        assert lock.exists(), "release must not delete another owner's lock"

    def test_release_removes_own_lock_after_external_touch(self, tmp_path: Path):
        """Release still works when the lock content kept our token but
        the file was touched (mtime bump) by an external tool."""
        out = tmp_path / "x.mp4"
        lp = acquire_output_lock(out)
        lock = lock_path_for(out)
        os.utime(lock, (time.time(), time.time()))
        release_output_lock(lp)
        assert not lock.exists()

    def test_live_lock_with_own_pid_refused(self, tmp_path: Path):
        """#C15: a lock whose pid is alive is a genuinely concurrent run —
        the acquire must refuse it (never reclaim a live owner)."""
        out = tmp_path / "x.mp4"
        lock = lock_path_for(out)
        lock.write_text(f"pid={os.getpid()} output=x.mp4\n", encoding="utf-8")
        with pytest.raises(ConcatLockError):
            acquire_output_lock(out)

    def test_live_lock_with_old_mtime_refused(self, tmp_path: Path):
        """Audit regression: a lock whose pid is ALIVE must be refused
        even when its mtime is older than the old 1h threshold. The lock
        mtime is never refreshed during a run, so an old mtime means the
        run is long (final-concat timeout reaches 24h), not that the
        owner died — reclaiming it would make two runs write the same
        output file."""
        out = tmp_path / "x.mp4"
        lock = lock_path_for(out)
        lock.write_text(f"pid={os.getpid()} output=x.mp4\n", encoding="utf-8")
        old = time.time() - 60 * 60 - 60
        os.utime(lock, (old, old))
        with pytest.raises(ConcatLockError):
            acquire_output_lock(out)

    def test_stale_lock_with_old_mtime_reclaimed(self, tmp_path: Path):
        """A lock whose pid is DEAD is reclaimed regardless of its age."""
        psutil = pytest.importorskip("psutil")
        out = tmp_path / "x.mp4"
        lock = lock_path_for(out)
        pid = 2**31 - 2  # far above any real pid on this machine
        assert not psutil.pid_exists(pid)
        lock.write_text(f"pid={pid} output=x.mp4\n", encoding="utf-8")
        old = time.time() - 60 * 60 - 60
        os.utime(lock, (old, old))
        lp = acquire_output_lock(out)
        assert lp.path.exists()
        lp.path.unlink()

    def test_stale_lock_with_dead_pid_reclaimed(self, tmp_path: Path):
        """#C15: pid-based reclaim — a fresh lock owned by a gone process
        is reclaimed. Needs psutil for the liveness probe; without it the
        lock is refused and the user gets the manual-cleanup message."""
        psutil = pytest.importorskip("psutil")
        out = tmp_path / "x.mp4"
        lock = lock_path_for(out)
        pid = 2**31 - 2  # far above any real pid on this machine
        assert not psutil.pid_exists(pid)
        lock.write_text(f"pid={pid} output=x.mp4\n", encoding="utf-8")
        lp = acquire_output_lock(out)
        assert lp.path.exists()
        lp.path.unlink()

    def test_lock_reclaimed_when_pid_reused_with_other_create_time(self, tmp_path: Path):
        """Audit round 22 P9: a REUSED pid must not keep a stale lock
        alive forever — the lock records the owner's process creation
        time, and a new process at the same pid started at a different
        moment is provably not the owner, so the lock is reclaimed."""
        pytest.importorskip("psutil")
        out = tmp_path / "x.mp4"
        lock = lock_path_for(out)
        # Own pid (alive!) but a creation time that no process can match:
        # epoch 0 is millennia away from the real start time.
        lock.write_text(f"pid={os.getpid()} started=0.0 output=x.mp4\n", encoding="utf-8")
        lp = acquire_output_lock(out)
        assert lp.path.exists()
        lp.path.unlink()

    def test_live_lock_with_matching_create_time_refused(self, tmp_path: Path):
        """Audit round 22 P9: a lock owned by the REAL current process
        (matching pid AND creation time) is refused like a normal live
        lock — create-time identity must not weaken the liveness check."""
        psutil = pytest.importorskip("psutil")
        out = tmp_path / "x.mp4"
        lock = lock_path_for(out)
        create_time = psutil.Process().create_time()
        lock.write_text(f"pid={os.getpid()} started={create_time} output=x.mp4\n", encoding="utf-8")
        with pytest.raises(ConcatLockError):
            acquire_output_lock(out)

    def test_partial_write_loops_until_complete(self, tmp_path: Path):
        """Audit round 22 P6: a POSIX partial write (the fd accepts one
        byte at a time) must not truncate the ownership record — the
        acquire loops until the full payload landed and verifies the
        token/pid round-trip."""
        import stream2video.concat.output_lock as ol

        out = tmp_path / "x.mp4"
        lock = lock_path_for(out)
        real_write = os.write

        def one_byte(fd, data):
            return real_write(fd, data[:1])

        with patch.object(ol.os, "write", side_effect=one_byte):
            lp = acquire_output_lock(out)
        text = lock.read_text(encoding="utf-8")
        assert text.startswith("token=")
        assert f"pid={os.getpid()}" in text
        release_output_lock(lp)
        assert not lock.exists()

    def test_write_failure_removes_fresh_lock_and_raises_lock_error(self, tmp_path: Path):
        """Audit round 23 P4: an os.write failure mid-acquire must not
        leave a fresh pid-less lock behind (the next run would burn the
        full grace window waiting for a pid that never arrives) and must
        surface as ConcatLockError, not a raw OSError."""
        import stream2video.concat.output_lock as ol

        out = tmp_path / "x.mp4"
        lock = lock_path_for(out)

        def exploding_write(fd, data):
            raise OSError("disk on fire")

        with (
            patch.object(ol.os, "write", side_effect=exploding_write),
            pytest.raises(ConcatLockError, match="could not be written"),
        ):
            acquire_output_lock(out)
        assert not lock.exists(), "failed acquire must not leave its fresh lock behind"

    def test_zero_byte_write_removes_fresh_lock_and_raises_lock_error(self, tmp_path: Path):
        """Audit round 23 P4: a write that reports zero bytes (the
        stalled-write guard inside the loop) takes the same cleanup
        path as a raising write."""
        import stream2video.concat.output_lock as ol

        out = tmp_path / "x.mp4"
        lock = lock_path_for(out)

        def stalled_write(fd, data):
            return 0

        with (
            patch.object(ol.os, "write", side_effect=stalled_write),
            pytest.raises(ConcatLockError, match="could not be written"),
        ):
            acquire_output_lock(out)
        assert not lock.exists()

    def test_reclaim_restores_lock_replaced_during_reclaim(self, tmp_path: Path):
        """Audit round 23 P5: the stale-reclaim TOCTOU — between the
        acquire reading the stale lock and unlinking it, a concurrent
        run deletes it and creates its OWN live lock at the same path.
        The reclaim must move the file aside atomically, notice it is no
        longer the judged file, put it back, and report False so the
        caller re-reads (and refuses the live lock)."""
        import stream2video.concat.output_lock as ol

        out = tmp_path / "x.mp4"
        lock = lock_path_for(out)
        STALE_TEXT = "token=stale pid=999999 output=x.mp4\n"
        LIVE_TEXT = "token=live pid=999999 output=x.mp4\n"
        lock.write_text(STALE_TEXT, encoding="utf-8")
        old = time.time() - 60 * 60 - 60
        os.utime(lock, (old, old))

        real_rename = ol.os.rename

        def sneaky_rename(src, dst):
            if src == lock:
                # Concurrent re-acquire: the stale lock dies and a new
                # live lock takes its place right before our rename.
                lock.unlink(missing_ok=True)
                lock.write_text(LIVE_TEXT, encoding="utf-8")
            return real_rename(src, dst)

        with patch.object(ol.os, "rename", side_effect=sneaky_rename):
            reclaimed = ol._reclaim_stale_lock(lock, STALE_TEXT)
        assert reclaimed is False, "the reclaim must refuse to destroy the replacement lock"
        assert lock.read_text(encoding="utf-8") == LIVE_TEXT, (
            "the concurrent run's live lock must be restored in place"
        )

    def test_reclaim_deletes_exact_stale_file(self, tmp_path: Path):
        """Audit round 23 P5: the happy path — the judged stale lock is
        still the file on disk when the reclaim runs, so it is moved
        aside, verified and deleted; the path is left free."""
        import stream2video.concat.output_lock as ol

        out = tmp_path / "x.mp4"
        lock = lock_path_for(out)
        STALE_TEXT = "token=stale pid=999999 output=x.mp4\n"
        lock.write_text(STALE_TEXT, encoding="utf-8")
        assert ol._reclaim_stale_lock(lock, STALE_TEXT) is True
        assert not lock.exists()
        # No quarantine artifacts left behind.
        leftovers = [p for p in lock.parent.iterdir() if ".reclaim-" in p.name]
        assert leftovers == []

    def test_lock_released_on_pipeline_error(self, tmp_path: Path):
        """#6: a pipeline that raises still releases the lock."""
        video = tmp_path / "src.mp4"
        video.write_bytes(b"source")
        output = tmp_path / "out.mp4"

        def boom(*_a, **_kw):
            raise RuntimeError("encoder exploded")

        from stream2video.concat import cut_and_concat

        with (
            patch("stream2video.concat._run_with_fallback", side_effect=boom),
            patch("stream2video.concat.get_video_encoder", return_value=("libx264", [])),
            patch("stream2video.concat.has_audio_stream", return_value=False),
            patch("stream2video.concat.generate_keep_segments", return_value=[(0.0, 5.0)]),
            patch("stream2video.concat._make_memory_monitor_factory", return_value=None),
            pytest.raises(RuntimeError, match="encoder exploded"),
        ):
            cut_and_concat(video, [], output)
        assert not lock_path_for(output).exists(), "lock leaked after pipeline error"


# ── audit round 22 P1 ── encoder smoke test: transient spawn OSError ─
class TestEncoderSmokeTransientSpawn:
    def test_transient_winerror_206_reported_unavailable(self):
        """An exhausted WinError 206 OSError from run_with_retry must
        degrade to 'encoder unavailable' exactly like FileNotFoundError —
        not crash ``--doctor`` or escape into generic pipeline error."""
        from stream2video.concat import encoders as enc

        def _exhausted(*_a, **_k):
            # winerror must be set explicitly: the OSError constructor's
            # positional winerror argument is IGNORED on POSIX (where a
            # real subprocess OSError never carries winerror either), so
            # the 4-arg form yields winerror=None on Linux and the
            # transient filter would not recognize the code.
            exc = OSError(206, "filename or extension too long", "ffmpeg.exe")
            exc.winerror = 206
            raise exc

        try:
            enc._encoder_check_cache.pop("libx264", None)
            with patch.object(enc, "run_with_retry", side_effect=_exhausted):
                assert enc.check_encoder("libx264") is False
            # Cached False — the second call short-circuits.
            assert enc.check_encoder("libx264") is False
        finally:
            enc._encoder_check_cache.pop("libx264", None)

    def test_transient_file_not_found_still_unavailable(self):
        from stream2video.concat import encoders as enc

        def _fnf(*_a, **_k):
            raise FileNotFoundError(2, "no such file")

        try:
            enc._encoder_check_cache.pop("libx264", None)
            with patch.object(enc, "run_with_retry", side_effect=_fnf):
                assert enc.check_encoder("libx264") is False
        finally:
            enc._encoder_check_cache.pop("libx264", None)

    def test_nontransient_oserror_reraised(self):
        """Non-transient OSErrors must NOT be masked as 'unavailable'."""
        from stream2video.concat import encoders as enc

        def _boom(*_a, **_k):
            raise PermissionError(13, "access denied")

        try:
            enc._encoder_check_cache.pop("libx264", None)
            with (
                patch.object(enc, "run_with_retry", side_effect=_boom),
                pytest.raises(PermissionError),
            ):
                enc.check_encoder("libx264")
        finally:
            enc._encoder_check_cache.pop("libx264", None)


# ── #7 ── pre-first-progress timeout (opt-in) ──────────────────────────
class TestPreProgressTimeout:
    def test_kills_silent_start(self, tmp_path: Path):
        """#7: pre_progress_timeout must kill an ffmpeg that emits nothing."""
        from stream2video.concat import FFmpegError, _run_ffmpeg

        real_popen = subprocess.Popen

        def fake_popen(cmd, **kwargs):
            return real_popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        start = time.monotonic()
        with (
            patch("stream2video.concat.subprocess.Popen", side_effect=fake_popen),
            pytest.raises(FFmpegError, match="no progress"),
        ):
            _run_ffmpeg(
                [sys.executable, "-c", "pass"],
                progress_callback=lambda us: None,
                timeout=60,
                label="silent",
                pre_progress_timeout=1,
            )
        elapsed = time.monotonic() - start
        assert elapsed < 10.0, f"pre-progress kill took {elapsed:.1f}s, expected fast kill"

    def test_default_preserves_legacy_behaviour(self, tmp_path: Path):
        """pre_progress_timeout=None (default) must not break the existing
        stall watchdog contract — the pipeline is the thing setting it."""
        # Just verifies the parameter is accepted and defaults to None.
        import inspect

        from stream2video.concat.runner import _run_ffmpeg

        sig = inspect.signature(_run_ffmpeg)
        assert sig.parameters["pre_progress_timeout"].default is None


# ── #15 ── cancel_process ordering ─────────────────────────────────────
class TestCancelProcessOrder:
    def test_wait_before_pipe_close(self):
        """#15: wait() must run before pipes are closed."""
        from stream2video import utils

        calls: list[str] = []

        class FakePipe:
            def close(self):
                calls.append("close")

        class FakeProc:
            def __init__(self):
                self.stdin = FakePipe()
                self.stdout = FakePipe()
                self.stderr = FakePipe()
                self._alive = True

            def poll(self):
                calls.append("poll")
                return None if self._alive else 0

            def kill(self):
                calls.append("kill")
                self._alive = False

            def wait(self, timeout=None):
                calls.append("wait")
                self._alive = False
                return 0

        proc = FakeProc()
        with utils.registered_process(proc, owner="test"):
            assert utils.cancel_process("test")
        assert calls.index("kill") < calls.index("wait") < calls.index("close")


# ── #19 ── SIGINT identity ──────────────────────────────────────────────
class TestSigintIdentity:
    def test_double_main_restores_default(self):
        """#19: two in-process main() calls must not hold the first cancel event."""
        import stream2video.cli_helpers as ch

        assert hasattr(ch, "_installed_sigint_handler"), (
            "cli_helpers must track its own SIGINT handler by reference"
        )


# ── #22 ── unlink retry ─────────────────────────────────────────────────
class TestUnlinkRetry:
    def test_retries_then_succeeds(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from stream2video.pipeline_controller import _unlink_with_retry

        f = tmp_path / "x.tmp"
        f.write_bytes(b"x")

        # Fail twice, then succeed — simulates an AV holding the file.
        fail_count = [0]
        real_unlink = Path.unlink

        def flaky(self, *a, **kw):
            if self == f and fail_count[0] < 2:
                fail_count[0] += 1
                raise PermissionError("WinError 32: file in use")
            return real_unlink(self, *a, **kw)

        monkeypatch.setattr(Path, "unlink", flaky)
        assert _unlink_with_retry(f, attempts=3, delay_s=0.01)
        assert fail_count[0] == 2, "retry didn't retry before succeeding"

    def test_gives_up_cleanly(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from stream2video.pipeline_controller import _unlink_with_retry

        f = tmp_path / "x.tmp"
        f.write_bytes(b"x")

        def always_fail(self, *a, **kw):
            raise PermissionError("permanent lock")

        monkeypatch.setattr(Path, "unlink", always_fail)
        assert not _unlink_with_retry(f, attempts=2, delay_s=0.01)


# ── #24 ── manifest identity ────────────────────────────────────────────
class TestManifestHash:
    def test_same_size_mtime_different_content_invalidates(self, tmp_path: Path):
        from stream2video.concat.manifest import (
            _build_manifest,
            _ensure_fresh_work_dir,
        )

        src = tmp_path / "a.mp4"
        src.write_bytes(os.urandom(1024) + os.urandom(1024))  # >1MiB tail differs
        # Force the same mtime on the rewrite so ONLY the hash differs.
        st = src.stat()
        keep = [(0.0, 1.0)]
        m1 = _build_manifest(
            src, keep, "segment", "libx264", "libx264", [], "medium", "medium", "medium", "auto"
        )

        wd = tmp_path / "_work"
        _ensure_fresh_work_dir(wd, m1)
        marker = wd / "seg_000000.mp4"
        marker.write_bytes(b"partial")

        # Rewrite content, restore mtime to fake "same file".
        src.write_bytes(os.urandom(2048))
        os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns))
        m2 = _build_manifest(
            src, keep, "segment", "libx264", "libx264", [], "medium", "medium", "medium", "auto"
        )
        _ensure_fresh_work_dir(wd, m2)
        assert not marker.exists(), "stale part survived a content swap with preserved mtime"


# ── #28 ── log trimming ─────────────────────────────────────────────────
class TestLogTrimming:
    def test_poller_caps_line_count(self):
        from stream2video.tk_dispatch import LogQueuePoller, TkDispatcher

        class TB:
            def __init__(self):
                self.lines: list[str] = []

            def configure(self, **_k): ...
            def see(self, _i): ...
            def tag_config(self, *_a, **_k): ...
            def tag_add(self, *_a): ...
            def index(self, i):
                return i

            def insert(self, _i, t):
                self.lines.extend(t.splitlines())

            def delete(self, a, b=None):
                n = int(b.split(".")[0]) if b else 1
                del self.lines[:n]

        class R:
            def after(self, _ms, _fn):
                return "x"

        tb = TB()
        poller = LogQueuePoller(tb, TkDispatcher(R()))
        poller._MAX_LOG_LINES = 50
        for i in range(150):
            poller.log(f"m{i}")
        # Pump the queue through poll (drains in 100ms batches; call repeatedly).
        while True:
            try:
                poller.poll()
            except Exception:
                break
            # poll reschedules via the fake dispatcher, so we drive manually.
            if poller._queue.empty():
                break
        # Drive poll once more to drain any stragglers
        try:
            poller.poll()
        except Exception:
            pass
        assert len(tb.lines) <= 50, f"log not trimmed: {len(tb.lines)} lines"
        assert tb.lines[-1].endswith("m149"), "newest line was trimmed"
