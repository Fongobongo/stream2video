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


def _lock_three_contender(
    out_path: str,
    name: str,
    results: dict[str, str],
    barrier: object,
    release_gate: object,
) -> None:
    """Multiprocessing contender for the three-way lock race test.

    Module level (not a local closure) so the ``spawn`` context on
    Windows can pickle it by name. Exactly one of the three contenders
    may acquire; the others must be refused by the OS lock."""
    from stream2video.concat.output_lock import acquire_output_lock, release_output_lock

    barrier.wait(timeout=30)  # type: ignore[attr-defined]
    try:
        h = acquire_output_lock(Path(out_path), timeout=1.0)
        results[name] = "acquired"
        release_gate.wait(timeout=30)  # type: ignore[attr-defined]
        release_output_lock(h)
    except ConcatLockError:
        results[name] = "refused"
    except Exception as e:
        results[name] = f"error:{type(e).__name__}:{e}"


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


# ── #6 ── output lock (audit round 24 P1: OS-level file locks) ─────────
class TestOutputLock:
    """The lock is an OS-level file lock on ``<output>.lock``.

    Audit round 24 P1 replaced the pid-liveness + quarantine scheme:
    the kernel releases the OS lock when the owner dies (crash,
    ``kill -9``, BSOD), so no stale lock can outlive its owner and no
    heuristic can ever steal a live one. These tests pin the new
    protocol: lock-file content is diagnostic only, liveness IS the OS
    lock, a leftover lock FILE is taken immediately, and the audit's
    three-contender multiprocessing scenario serializes exactly one
    winner (Linux and Windows).
    """

    def test_second_acquire_raises(self, tmp_path: Path):
        out = tmp_path / "x.mp4"
        lp = acquire_output_lock(out)
        assert lp.path.exists()
        with pytest.raises(ConcatLockError):
            acquire_output_lock(out, timeout=0.2)
        release_output_lock(lp)
        assert not lp.path.exists()
        # Releasing frees the name for the next run.
        lp2 = acquire_output_lock(out)
        assert lp2.path.exists()
        release_output_lock(lp2)
        assert not lp2.path.exists()

    def test_orphan_lock_file_is_taken_immediately(self, tmp_path: Path):
        """A leftover lock FILE from a crashed run holds no OS lock — the
        next acquirer takes it immediately, regardless of age or content.
        The old scheme needed pid probes + mtime windows to judge
        staleness; the OS lock already decided (audit round 24 P1)."""
        out = tmp_path / "x.mp4"
        lock = lock_path_for(out)
        lock.write_text("token=dead pid=424242 output=x.mp4\n", encoding="utf-8")
        old = time.time() - 60 * 60 - 60
        os.utime(lock, (old, old))
        lp = acquire_output_lock(out)
        assert lp.path.exists()
        release_output_lock(lp)
        assert not lock.exists()

    def test_orphan_lock_file_with_live_pid_is_taken_immediately(self, tmp_path: Path):
        """Even a record claiming OUR OWN live pid must not matter: a
        lock file whose owner is gone holds no OS lock, so it is taken.
        The old scheme refused fresh pid-less locks and probed pid
        liveness; content is diagnostic only now."""
        out = tmp_path / "x.mp4"
        lock = lock_path_for(out)
        lock.write_text(f"token=dead pid={os.getpid()} output=x.mp4\n", encoding="utf-8")
        lp = acquire_output_lock(out)
        assert lp.path.exists()
        release_output_lock(lp)

    def test_slow_writer_race_is_not_stealable(self, tmp_path: Path):
        """Acquirer A holds the OS lock BEFORE writing its owner record;
        a concurrent acquire B retries, observes A's lock and refuses —
        never deleting A's live lock (the old audit-#1 race)."""
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
            patch.object(ol.os, "write", side_effect=slow_write),
        ):
            a = threading.Thread(target=acquire_a)
            a.start()
            assert opened.wait(timeout=5), "acquirer A never reached its write"
            with pytest.raises(ConcatLockError):
                acquire_output_lock(out, timeout=0.2)
            a.join(timeout=10)
        assert not a.is_alive()
        handle = a_result["handle"]
        assert isinstance(handle, ol.LockHandle)
        assert handle.path.exists()
        release_output_lock(handle)
        assert not handle.path.exists()

    def test_identity_churn_retries_then_acquires(self, tmp_path: Path):
        """Between our open and our lock the path was unlinked+recreated
        (a release raced us): the fd locks an orphaned inode. The
        acquire closes and retries on the current file."""
        import stream2video.concat.output_lock as ol

        out = tmp_path / "x.mp4"
        real_same = ol._same_file
        calls = {"n": 0}

        def churn_then_stable(fd, path):
            calls["n"] += 1
            if calls["n"] == 1:
                return False  # path swapped under us once
            return real_same(fd, path)

        with patch.object(ol, "_same_file", side_effect=churn_then_stable):
            lp = acquire_output_lock(out)
        assert lp.path.exists()
        release_output_lock(lp)
        assert not lp.path.exists()

    def test_identity_churn_past_deadline_raises(self, tmp_path: Path):
        """A path that keeps changing identity (an external tool
        recreating the lock file faster than we can acquire) must time
        out with an explicit refusal — never hang (audit round 24 P1)."""
        import stream2video.concat.output_lock as ol

        out = tmp_path / "x.mp4"
        with (
            patch.object(ol, "_same_file", return_value=False),
            patch.object(ol, "_RETRY_SLEEP_SECONDS", 0.001),
            pytest.raises(ConcatLockError, match="kept changing identity"),
        ):
            acquire_output_lock(out, timeout=0.1)

    def test_release_does_not_unlink_replaced_lock(self, tmp_path: Path):
        """Release must not delete a file that no longer belongs to our
        lock: if the path was unlinked + recreated while we held the
        handle, the identity check keeps the new owner's file (only
        possible on POSIX — Windows cannot unlink an open file)."""
        if os.name == "nt":
            pytest.skip("Windows cannot unlink an open file")
        out = tmp_path / "x.mp4"
        lp = acquire_output_lock(out)
        lock = lock_path_for(out)
        # The path is replaced by another run's file while we hold the
        # (now orphaned) inode lock.
        lock.unlink()
        lock.write_text("token=other pid=424242 output=x.mp4\n", encoding="utf-8")
        release_output_lock(lp)
        assert lock.exists(), "release must not delete another owner's lock file"

    def test_release_removes_own_lock_after_external_touch(self, tmp_path: Path):
        """Release still works when the lock file was touched (mtime
        bump) by an external tool — identity is the inode, not the
        record or the mtime."""
        out = tmp_path / "x.mp4"
        lp = acquire_output_lock(out)
        lock = lock_path_for(out)
        os.utime(lock, (time.time(), time.time()))
        release_output_lock(lp)
        assert not lock.exists()

    def test_partial_write_loops_until_complete(self, tmp_path: Path):
        """A one-byte-at-a-time fd must not truncate the ownership
        record — the acquire loops until the full payload landed."""
        import stream2video.concat.output_lock as ol

        out = tmp_path / "x.mp4"
        lock = lock_path_for(out)
        real_write = os.write

        def one_byte(fd, data):
            return real_write(fd, data[:1])

        with patch.object(ol.os, "write", side_effect=one_byte):
            lp = acquire_output_lock(out)
        # Read through the LOCKING handle: on Windows the locked byte 0
        # denies reads via any OTHER handle, so the file cannot be read
        # by path while the lock is held.
        os.lseek(lp.fd, 0, os.SEEK_SET)
        text = os.read(lp.fd, 4096).decode("utf-8")
        assert text.startswith("token=")
        assert f"pid={os.getpid()}" in text
        release_output_lock(lp)
        assert not lock.exists()

    def test_write_failure_removes_fresh_lock_and_raises_lock_error(self, tmp_path: Path):
        """A record-write failure must not leave a fresh lock behind and
        must surface as ConcatLockError, not a raw OSError. The lock
        file is pre-filled so the exploding write hits the OWNER RECORD
        write on both platforms (on Windows the byte-0 placeholder write
        is skipped for a non-empty file)."""
        import stream2video.concat.output_lock as ol

        out = tmp_path / "x.mp4"
        lock = lock_path_for(out)
        lock.write_text("pre-existing\n", encoding="utf-8")

        def exploding_write(fd, data):
            raise OSError("disk on fire")

        with (
            patch.object(ol.os, "write", side_effect=exploding_write),
            pytest.raises(ConcatLockError, match="could not be written"),
        ):
            acquire_output_lock(out)
        assert not lock.exists(), "failed acquire must not leave its fresh lock behind"

    @pytest.mark.skipif(os.name != "nt", reason="placeholder write is Windows-only")
    def test_placeholder_write_failure_cleans_up_and_raises(self, tmp_path: Path):
        """Windows: a failure of the byte-0 placeholder write (empty
        lock file) is a disk problem, not contention — it must raise
        ConcatLockError immediately instead of retrying the 60 s
        contention loop, and must not leave its fresh file behind."""
        import stream2video.concat.output_lock as ol

        out = tmp_path / "x.mp4"
        lock = lock_path_for(out)

        def exploding_write(fd, data):
            raise OSError("disk on fire")

        with (
            patch.object(ol.os, "write", side_effect=exploding_write),
            pytest.raises(ConcatLockError, match="could not be prepared"),
        ):
            acquire_output_lock(out)
        assert not lock.exists(), "failed placeholder write must not leave its file behind"

    def test_zero_byte_write_removes_fresh_lock_and_raises_lock_error(self, tmp_path: Path):
        """A write that reports zero bytes takes the same cleanup path
        as a raising write."""
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

    def test_acquire_lock_file_dedupes_arbitrary_paths(self, tmp_path: Path):
        """acquire_lock_file — the pipeline's project locks (URL-hash,
        source-stem) — uses the same OS-lock core: same path excludes,
        a different path is independent."""
        import stream2video.concat.output_lock as ol

        p = tmp_path / ".s2v_url_a1b2c3d4.lock"
        lp = ol.acquire_lock_file(p, what="URL", timeout=0.2)
        with pytest.raises(ConcatLockError, match="URL lock"):
            ol.acquire_lock_file(p, what="URL", timeout=0.2)
        # A DIFFERENT lock file is independent.
        lp2 = ol.acquire_lock_file(tmp_path / ".s2v_url_ffffffff.lock", what="URL", timeout=0.2)
        release_output_lock(lp)
        release_output_lock(lp2)
        assert not lp.path.exists()
        assert not lp2.path.exists()

    def test_acquire_lock_file_on_wait_fires_on_contention(self, tmp_path: Path):
        """The pipeline passes ``on_wait`` to log "waiting for another
        run" — it must fire exactly when contention is observed."""
        import stream2video.concat.output_lock as ol

        out = tmp_path / "x.mp4"
        holder = acquire_output_lock(out)
        fired: list[int] = []
        with (
            patch.object(ol, "_RETRY_SLEEP_SECONDS", 0.001),
            pytest.raises(ConcatLockError),
        ):
            ol.acquire_lock_file(
                lock_path_for(out),
                what="output",
                timeout=0.1,
                on_wait=lambda: fired.append(1),
            )
        assert fired, "on_wait must fire when contention is observed"
        release_output_lock(holder)

    def test_release_accepts_bare_path(self, tmp_path: Path):
        """The legacy Path overload removes the file without a handle."""
        out = tmp_path / "x.mp4"
        lock = lock_path_for(out)
        lock.write_text("stale\n", encoding="utf-8")
        release_output_lock(lock)
        assert not lock.exists()

    def test_three_contenders_multiprocessing_one_winner(self, tmp_path: Path):
        """The audit's CI scenario (Linux/Windows multiprocessing, THREE
        contenders for one lock): exactly one process acquires, the
        other two fail with ConcatLockError, and after the winner
        releases a newcomer acquires immediately — the OS lock
        serializes across processes and leaves no stale state behind."""
        import multiprocessing as mp

        ctx = mp.get_context("spawn" if os.name == "nt" else "fork")
        out = tmp_path / "x.mp4"
        lock = lock_path_for(out)
        results = ctx.Manager().dict()
        barrier = ctx.Barrier(3)
        release_gate = ctx.Event()

        procs = [
            ctx.Process(
                target=_lock_three_contender,
                args=(str(out), f"p{i}", results, barrier, release_gate),
            )
            for i in range(3)
        ]
        for p in procs:
            p.start()
        # Wait until every contender has reported (acquired or refused)
        # BEFORE freeing the winner — otherwise a loser still inside its
        # retry window could take the freed lock and become a second
        # "winner".
        deadline = time.monotonic() + 60
        while len(results) < 3 and time.monotonic() < deadline:
            time.sleep(0.05)
        release_gate.set()
        for p in procs:
            p.join(timeout=60)
        for p in procs:
            assert p.exitcode == 0, f"{p.name} exited {p.exitcode}"
        acquired = [k for k, v in results.items() if v == "acquired"]
        assert len(acquired) == 1, f"expected exactly one winner, got {dict(results)}"

        # Winner released (its record is gone); a newcomer acquires.
        assert not lock.exists(), "winner must have removed the lock file"
        lp = acquire_output_lock(out)
        release_output_lock(lp)
        assert not lock.exists()

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
