"""Integration tests for stream2video pipeline."""

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from stream2video.concat import (
    ENCODER_OPTS,
    CancelledError,
    ConcatError,
    _build_manifest,
    _ffprobe_is_valid_media,
    _ffprobe_is_valid_mp4,
    _manifest_path,
    _run_ffmpeg,
    _source_identity,
    _validate_manifest,
    _with_libx264_fallback,
    cut_and_concat,
    encoder_opts,
    generate_keep_segments,
    get_video_encoder,
)
from stream2video.download import download
from stream2video.silence import SilenceSegment


class TestPipelineIntegration:
    """Integration tests for the full pipeline."""

    def test_download_passthrough_with_local_file(self):
        """Local file input is passed through by `download()` (no network).

        End-to-end pipeline coverage (detect → cut → concat) lives in the
        silence / concat / integration suites; this test only pins the
        download local-passthrough branch.
        """
        with TemporaryDirectory() as tmpdir:
            # Create a dummy video file
            video_file = Path(tmpdir) / "input.mp4"
            video_file.write_text("dummy video data")

            # Step 1: Download (should just return local file)
            downloaded = download(str(video_file), Path(tmpdir))
            assert downloaded.path == video_file
            assert downloaded.path.exists()
            assert downloaded.is_downloaded is False

    def test_generate_keep_segments(self):
        """Test keep segment generation from silence segments."""
        silence_segments = [
            SilenceSegment(1.0, 2.0),
            SilenceSegment(4.0, 5.0),
        ]

        # Mock video with 10 second duration
        with patch("stream2video.concat.get_video_duration", return_value=10.0):
            keep_segments = generate_keep_segments(Path("dummy.mp4"), silence_segments)

        # Expected: [0-1], [2-4], [5-10]
        assert len(keep_segments) == 3
        assert keep_segments[0] == (0.0, 1.0)
        assert keep_segments[1] == (2.0, 4.0)
        assert keep_segments[2] == (5.0, 10.0)

    def test_generate_keep_segments_no_silence(self):
        """Test keep segment when no silence detected."""
        silence_segments = []

        with patch("stream2video.concat.get_video_duration", return_value=10.0):
            keep_segments = generate_keep_segments(Path("dummy.mp4"), silence_segments)

        # Expected: entire video
        assert len(keep_segments) == 1
        assert keep_segments[0] == (0.0, 10.0)

    def test_generate_keep_segments_all_silence(self):
        """Test keep segment when entire video is silence."""
        silence_segments = [
            SilenceSegment(0.0, 10.0),
        ]

        with patch("stream2video.concat.get_video_duration", return_value=10.0):
            keep_segments = generate_keep_segments(Path("dummy.mp4"), silence_segments)

        # Expected: empty (nothing to keep)
        assert len(keep_segments) == 0

    def test_generate_keep_segments_consecutive_silence(self):
        """Test keep segments with consecutive silence."""
        silence_segments = [
            SilenceSegment(1.0, 2.0),
            SilenceSegment(2.1, 3.0),  # Nearly adjacent
        ]

        with patch("stream2video.concat.get_video_duration", return_value=10.0):
            keep_segments = generate_keep_segments(Path("dummy.mp4"), silence_segments)

        # Expected: [0-1], [2-2.1], [3-10]
        assert len(keep_segments) == 3
        assert keep_segments[0] == (0.0, 1.0)
        assert keep_segments[1] == (2.0, 2.1)
        assert keep_segments[2] == (3.0, 10.0)

    def test_generate_keep_segments_clamps_negative_start(self):
        """Silence with negative start should be clamped to 0."""
        silence_segments = [SilenceSegment(-1.0, 2.0)]
        with patch("stream2video.concat.get_video_duration", return_value=10.0):
            keep = generate_keep_segments(Path("dummy.mp4"), silence_segments)
        assert keep == [(2.0, 10.0)]

    def test_generate_keep_segments_clamps_overrun_end(self):
        """Silence extending past duration should be clamped to duration."""
        silence_segments = [SilenceSegment(8.0, 15.0)]
        with patch("stream2video.concat.get_video_duration", return_value=10.0):
            keep = generate_keep_segments(Path("dummy.mp4"), silence_segments)
        assert keep == [(0.0, 8.0)]

    def test_generate_keep_segments_drops_invalid(self):
        """Silence with end <= start should be dropped."""
        silence_segments = [
            SilenceSegment(5.0, 5.0),  # zero-duration
            SilenceSegment(6.0, 4.0),  # inverted
            SilenceSegment(2.0, 3.0),  # valid
        ]
        with patch("stream2video.concat.get_video_duration", return_value=10.0):
            keep = generate_keep_segments(Path("dummy.mp4"), silence_segments)
        assert keep == [(0.0, 2.0), (3.0, 10.0)]

    def test_generate_keep_segments_handles_overlap(self):
        """Overlapping silences should merge via max(end) progression."""
        silence_segments = [
            SilenceSegment(1.0, 4.0),
            SilenceSegment(2.0, 5.0),  # overlaps previous
        ]
        with patch("stream2video.concat.get_video_duration", return_value=10.0):
            keep = generate_keep_segments(Path("dummy.mp4"), silence_segments)
        assert keep == [(0.0, 1.0), (5.0, 10.0)]

    def test_generate_keep_segments_invalid_duration(self):
        """Should raise on non-positive duration."""
        with (
            patch("stream2video.concat.get_video_duration", return_value=0.0),
            pytest.raises(Exception, match="Invalid video duration"),
        ):
            generate_keep_segments(Path("dummy.mp4"), [])

    def test_generate_keep_segments_no_duration(self):
        """Should raise when ffprobe can't determine duration."""
        with (
            patch("stream2video.concat.get_video_duration", return_value=None),
            pytest.raises(Exception, match="Could not determine"),
        ):
            generate_keep_segments(Path("dummy.mp4"), [])


class TestErrorRecovery:
    """Test error handling and recovery."""

    def test_missing_video_file_error(self):
        """Test appropriate error when video file not found."""
        with TemporaryDirectory() as tmpdir, pytest.raises(ConcatError, match="not found"):
            cut_and_concat(
                Path(tmpdir) / "nonexistent.mp4",
                [],
                Path(tmpdir) / "output.mp4",
            )

    def test_concat_with_no_keep_segments(self):
        """Test error when no segments to keep after silence removal."""
        with TemporaryDirectory() as tmpdir:
            video_file = Path(tmpdir) / "input.mp4"
            video_file.write_text("dummy")

            silence_segments = [
                SilenceSegment(0.0, 100.0),  # Entire video is silence
            ]

            with (
                patch("stream2video.concat.get_video_duration", return_value=100.0),
                pytest.raises(ConcatError, match="No video segments"),
            ):
                cut_and_concat(
                    video_file,
                    silence_segments,
                    Path(tmpdir) / "output.mp4",
                )


class TestFfmpegInvocation:
    """Regression tests for the ffmpeg subprocess wrapper.

    The wrapper runs both track-progress (concat) and non-track-progress
    (segment encode) variants. A previous bug called process.stdout.close()
    unconditionally in the finally block, which raised AttributeError on the
    segment path because stdout=subprocess.DEVNULL leaves process.stdout=None.
    """

    def test_run_ffmpeg_no_progress_does_not_crash_in_finally(self):
        """track_progress=False (segment path) must not raise AttributeError
        when process.stdout is None after the encode finishes."""
        import subprocess

        real_popen = subprocess.Popen

        def fake_popen(cmd, **kwargs):
            return real_popen(
                [sys.executable, "-c", "import sys; sys.exit(0)"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )

        with patch("stream2video.concat.subprocess.Popen", side_effect=fake_popen):
            _run_ffmpeg(
                [sys.executable, "-c", "pass"],
                progress_callback=None,
                timeout=30,
                label="test",
                track_progress=False,
            )

    def test_run_ffmpeg_with_progress_still_works(self):
        """track_progress=True (default) must keep working unchanged."""
        import subprocess

        real_popen = subprocess.Popen

        def fake_popen(cmd, **kwargs):
            return real_popen(
                [sys.executable, "-c", "import sys; sys.exit(0)"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        with patch("stream2video.concat.subprocess.Popen", side_effect=fake_popen):
            _run_ffmpeg(
                [sys.executable, "-c", "pass"],
                progress_callback=lambda us: None,
                timeout=30,
                label="test",
            )

    def test_run_ffmpeg_track_progress_cancel_during_silent_pipe(self):
        """Cancel must be detected within ~1s even when ffmpeg produces no
        -progress output (the for-loop is otherwise blocked on readline).

        Regression: previously the main loop only checked cancel between stdout
        lines, so a silent ffmpeg run could block for its full duration before
        noticing the cancel request. The fix adds a daemon cancel-monitor thread
        that polls cancel_callback every _CANCEL_POLL_INTERVAL seconds.
        """
        import subprocess
        import time

        from stream2video.concat import CancelledError

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
            pytest.raises(CancelledError),
        ):
            _run_ffmpeg(
                [sys.executable, "-c", "pass"],
                progress_callback=lambda us: None,
                timeout=60,
                label="silent",
                cancel_callback=lambda: True,
            )
        elapsed = time.monotonic() - start
        assert elapsed < 5.0, f"Cancel took {elapsed:.1f}s, expected <5s"

    def test_run_ffmpeg_stall_watchdog_kills_on_no_progress(self):
        """Fix-plan P1.5 / section 4: "Stall detector не срабатывает при
        полном молчании ffmpeg".

        When ffmpeg produces no ``out_time_us=`` progress line for longer
        than ``stall_kill`` seconds, the stall watchdog (a daemon thread
        separate from the readline loop) must kill the process and raise
        ``FFmpegError``. This is the regression for the original bug where
        a fully-hung ffmpeg blocked the readline loop indefinitely.

        Uses a real subprocess (``python -c "time.sleep(30)"``) that
        never emits progress; ``stall_kill`` is set to a tiny value so
        the test runs in seconds, not minutes.
        """
        import subprocess
        import time

        from stream2video.concat import FFmpegError

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
            pytest.raises(FFmpegError),
        ):
            _run_ffmpeg(
                [sys.executable, "-c", "pass"],
                progress_callback=lambda us: None,
                timeout=60,
                label="hung",
                # Tight stall window so the test runs in seconds. The
                # default 300s would make this test impractically slow.
                stall_kill=2,
                stall_warning=1,
            )
        elapsed = time.monotonic() - start
        # Watchdog fires after stall_kill (2s) + the poll interval (~0.5s).
        # Allow generous slack for slow CI but well under the 30s sleep.
        assert elapsed < 10.0, f"Stall watchdog took {elapsed:.1f}s, expected <10s"
        assert elapsed >= 2.0, (
            f"Stall watchdog fired too early ({elapsed:.1f}s) — it should wait "
            f"at least stall_kill=2s before killing."
        )

    def test_stall_killed_reports_stall_not_oom(self):
        """Regression for P1 audit v0.3 §4: a process killed by the
        stall watchdog gets rc=-9 on POSIX. Without the stall_killed
        flag, looks_like_oom(rc=-9, "") would misreport this as "ran
        out of memory". The flag must be checked FIRST so the user
        sees "stalled" instead.

        Drives a real hung subprocess (Python sleep), patches
        looks_like_oom to assert it's never even consulted on a
        stall-kill, and checks the raised message contains "stall".
        """
        import subprocess

        from stream2video.concat import FFmpegError, FFmpegOutOfMemoryError

        real_popen = subprocess.Popen

        def fake_popen(cmd, **kwargs):
            return real_popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        oom_called = {"yes": False}

        def fake_looks_like_oom(rc, stderr_text):
            oom_called["yes"] = True
            return True

        with (
            patch("stream2video.concat.subprocess.Popen", side_effect=fake_popen),
            patch("stream2video.concat.looks_like_oom", side_effect=fake_looks_like_oom),
            pytest.raises(FFmpegError) as exc,
        ):
            _run_ffmpeg(
                [sys.executable, "-c", "pass"],
                progress_callback=lambda us: None,
                timeout=60,
                label="hung",
                stall_kill=2,
                stall_warning=1,
            )
        assert "stall" in str(exc.value).lower(), (
            f"Expected 'stall' in error message, got: {exc.value}"
        )
        assert not isinstance(exc.value, FFmpegOutOfMemoryError), (
            "Stall-kill must NOT be reported as OOM"
        )
        assert not oom_called["yes"], "looks_like_oom must not be called when stall_killed.is_set()"

    def test_real_oom_rc_reports_oom_not_stall(self):
        """Counter-test: a non-zero rc with NO stall_killed flag and
        looks_like_oom=True must raise FFmpegOutOfMemoryError, not a
        stall message. Guards against the opposite mistake of always
        reporting stall for every rc=-9 (P1 audit v0.3 §4.2).

        Strategy: spawn a *short* daytime process that already exited
        (rc=0). The post-mortem block is bypassed so we can't reach the
        looks_like_oom path that way, but we CAN exercise it by zeroing
        the rc after wait — patch _run_ffmpeg internals:
        ``_wait_with_cancel`` is stubbed to return 137, ``looks_like_oom``
        returns True, and we feed a fast-exiting process so the watchdog
        never fires (stall_killed stays clear)."""

        import subprocess

        from stream2video.concat import FFmpegOutOfMemoryError

        real_popen = subprocess.Popen

        # Fast-exiting process: returns rc=0 real, but our stub uses 137.
        def fake_popen(cmd, **kwargs):
            return real_popen(
                [sys.executable, "-c", "pass"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )

        with (
            patch("stream2video.concat.subprocess.Popen", side_effect=fake_popen),
            patch("stream2video.concat._wait_with_cancel", return_value=137),
            patch("stream2video.concat.looks_like_oom", return_value=True),
            pytest.raises(FFmpegOutOfMemoryError),
        ):
            _run_ffmpeg(
                [sys.executable, "-c", "pass"],
                progress_callback=None,
                timeout=30,
                label="oom-kill",
                track_progress=False,
                # Generous stall window so the watchdog doesn't fire
                # before our fast process exits and _wait_with_cancel
                # returns 137 (the patched value).
                stall_kill=300,
                stall_warning=120,
            )


class TestSegmentModeProgressStreaming:
    """Regression: with 0 silence segments the whole video is ONE keep segment.
    Previously the per-segment encode ffmpeg call used track_progress=False, so
    the progress callback never fired during the 1.5h+ encode — the user saw
    'Cutting 0%' the entire time, then a sudden jump to 100%.

    The fix streams -progress pipe:1 from the per-segment ffmpeg and maps
    out_time_us (time within the segment) to absolute progress across the
    whole video. The concat step at the end still uses 0.9..1.0 for the
    final progress, so segment encode maxes at 0.9.
    """

    @pytest.fixture
    def has_ffmpeg(self):
        import shutil

        return shutil.which("ffmpeg") is not None

    def _make_video(self, out_path: Path, duration: int = 5) -> None:
        import shutil

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            pytest.skip("ffmpeg not available")
        import subprocess

        cmd = [
            ffmpeg,
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=1000:duration={duration}",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=160x120:r=10",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-t",
            str(duration),
            str(out_path),
        ]
        subprocess.run(
            cmd,
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=60,
        )

    def test_progress_fires_during_single_segment_encode(self, has_ffmpeg):
        """With 0 silence segments, the single keep-segment encode must
        report multiple progress updates (not just 0% and 100%)."""
        if not has_ffmpeg:
            pytest.skip("ffmpeg not available")

        with TemporaryDirectory() as tmp:
            video = Path(tmp) / "in.mp4"
            self._make_video(video, duration=4)
            out = Path(tmp) / "out.mp4"

            progress_calls: list = []

            cut_and_concat(
                video,
                silence_segments=[],
                output_path=out,
                progress_callback=progress_calls.append,
                method="segment",
                encoder="libx264",
            )

            # Must have more than 2 progress reports (the start and end).
            assert len(progress_calls) > 2, (
                f"Expected multiple progress reports during 1-segment encode, "
                f"got {len(progress_calls)}: {progress_calls}"
            )
            # Values must be monotonically non-decreasing and within [0, 1].
            assert all(0.0 <= p <= 1.0 for p in progress_calls), progress_calls
            assert progress_calls == sorted(progress_calls), (
                f"Progress not monotonic: {progress_calls}"
            )
            # Final progress must reach >= 0.9 (concat step covers 0.9..1.0).
            assert progress_calls[-1] >= 0.9, (
                f"Final progress {progress_calls[-1]} should reach >= 0.9"
            )


class TestEncoderFallbackCleanup:
    """Regression: when the primary encoder (e.g. h264_mf) writes corrupt
    output (e.g. MP4 without moov atom) the fallback to libx264 must
    re-encode from scratch — the resume-skip logic in _run_segment_concat
    would otherwise reuse the corrupt seg files and the libx264 retry
    would also fail with the same moov-atom error.
    """

    def test_fallback_calls_on_fallback_with_failing_encoder(self):
        """on_fallback is invoked with the failing encoder name, BEFORE
        the libx264 retry runs. CancelledError skips fallback (and
        skips on_fallback)."""
        from stream2video.concat import ENCODER_OPTS

        calls = {"try": [], "cleanup": []}

        def _try(enc, opts):
            calls["try"].append(enc)
            if enc == "h264_mf":
                raise ConcatError("moov atom not found")
            # libx264 succeeds

        def _cleanup(failed_enc):
            calls["cleanup"].append(failed_enc)

        _with_libx264_fallback(
            "h264_mf",
            ENCODER_OPTS["h264_mf"][:],
            _try,
            (ConcatError, OSError),
            _cleanup,
            software_fallback="enabled",
        )
        assert calls["try"] == ["h264_mf", "libx264"]
        assert calls["cleanup"] == ["h264_mf"], (
            "on_fallback must be called once with the failing encoder name before the libx264 retry"
        )

    def test_no_cleanup_when_primary_succeeds(self):
        from stream2video.concat import ENCODER_OPTS

        calls = {"cleanup": []}

        def _try(enc, opts):
            pass  # primary succeeds

        _with_libx264_fallback(
            "h264_mf",
            ENCODER_OPTS["h264_mf"][:],
            _try,
            (ConcatError, OSError),
            lambda e: calls["cleanup"].append(e),
            software_fallback="enabled",
        )
        assert calls["cleanup"] == [], (
            "on_fallback must not be called when the primary encoder succeeds"
        )

    def test_no_cleanup_on_cancelled(self):
        from stream2video.concat import ENCODER_OPTS

        calls = {"cleanup": []}

        def _try(enc, opts):
            raise CancelledError("user pressed cancel")

        def _cleanup(name):
            calls["cleanup"].append(name)

        with pytest.raises(CancelledError):
            _with_libx264_fallback(
                "h264_mf",
                ENCODER_OPTS["h264_mf"][:],
                _try,
                (ConcatError, OSError),
                _cleanup,
                software_fallback="enabled",
            )
        assert calls["cleanup"] == [], (
            "on_fallback must not run on CancelledError (no fallback retry)"
        )

    def test_libx264_failure_propagates_after_cleanup(self):
        """If libx264 also fails, the exception propagates — but
        on_fallback is NOT called again (libx264 is the last attempt)."""
        from stream2video.concat import ENCODER_OPTS

        calls = {"cleanup": []}

        def _try(enc, opts):
            raise ConcatError(f"{enc} failed")

        def _cleanup(name):
            calls["cleanup"].append(name)

        with pytest.raises(ConcatError, match="libx264 failed"):
            _with_libx264_fallback(
                "h264_mf",
                ENCODER_OPTS["h264_mf"][:],
                _try,
                (ConcatError, OSError),
                _cleanup,
                software_fallback="enabled",
            )
        assert calls["cleanup"] == ["h264_mf"], (
            "on_fallback should only fire once (before the libx264 retry)"
        )


class TestEncoderQualityPresets:
    """encoder_opts(encoder, quality) — bitrate (HW) and CRF (libx264)
    must track the quality preset. ``medium`` reproduces the previously
    hard-coded defaults so existing output size/quality is unchanged."""

    def test_medium_matches_legacy_encoder_opts(self):
        # The legacy hard-coded values (7000k / CRF 23) must be exactly
        # reproduced by encoder_opts(enc, "medium"). This is the
        # backward-compat guarantee for users upgrading.
        legacy = {
            "h264_mf": ["-b:v", "7000k", "-quality", "100"],
            "h264_amf": ["-usage", "transcoding", "-quality", "speed", "-b:v", "7000k"],
            "h264_nvenc": [
                "-preset",
                "p7",
                "-rc",
                "vbr",
                "-b:v",
                "7000k",
                "-maxrate",
                "7000k",
                "-cq",
                "18",
            ],
            "libx264": [
                "-b:v",
                "7000k",
                "-maxrate",
                "7000k",
                "-bufsize",
                "7000k",
                "-preset",
                "medium",
            ],
        }
        for enc in ("h264_mf", "h264_amf", "h264_nvenc", "libx264"):
            assert encoder_opts(enc, "medium") == legacy[enc], (
                f"medium preset must reproduce legacy opts for {enc}"
            )

    def test_encoder_opts_registry_matches_default(self):
        # The module-level ENCODER_OPTS dict is a back-compat registry
        # mapping encoder -> default (medium) opts.
        for enc in ("h264_mf", "h264_amf", "h264_nvenc", "libx264"):
            assert ENCODER_OPTS[enc] == encoder_opts(enc, "medium")

    def test_bitrate_mode_tracks_quality_for_all_encoders(self):
        from stream2video.concat import _VIDEO_BITRATES

        assert _VIDEO_BITRATES == {"high": "10000k", "medium": "7000k", "low": "3500k"}
        for enc in ("h264_mf", "h264_amf", "h264_nvenc", "libx264"):
            for q, br in _VIDEO_BITRATES.items():
                opts = encoder_opts(enc, q)
                assert "-b:v" in opts
                idx = opts.index("-b:v")
                assert opts[idx + 1] == br, f"{enc} {q}: -b:v must be {br}"
                if enc == "h264_nvenc":
                    assert "-maxrate" in opts
                    m_idx = opts.index("-maxrate")
                    assert opts[m_idx + 1] == br, f"{enc} {q}: -maxrate must be {br}"

    def test_use_crf_tracks_quality_for_all_encoders(self):
        from stream2video.concat import _X264_CRF

        assert _X264_CRF == {"high": "18", "medium": "23", "low": "28"}
        for q, crf in _X264_CRF.items():
            opts = encoder_opts("libx264", q, use_crf=True)
            idx = opts.index("-crf")
            assert opts[idx + 1] == crf
            assert "-b:v" not in opts

            nvenc = encoder_opts("h264_nvenc", q, use_crf=True)
            assert "-cq" in nvenc
            assert nvenc[nvenc.index("-cq") + 1] == crf
            assert "-b:v" not in nvenc

            amf = encoder_opts("h264_amf", q, use_crf=True)
            assert "-qp_i" in amf
            assert amf[amf.index("-qp_i") + 1] == crf
            assert "-qp_p" in amf
            assert amf[amf.index("-qp_p") + 1] == crf
            assert "-qp_b" in amf
            assert amf[amf.index("-qp_b") + 1] == crf
            assert "-b:v" not in amf

    def test_use_crf_maps_mf_to_quality_scale(self):
        expected = {"high": "100", "medium": "75", "low": "50"}
        for q, quality in expected.items():
            opts = encoder_opts("h264_mf", q, use_crf=True)
            assert "-rate_control" in opts
            assert opts[opts.index("-rate_control") + 1] == "quality"
            assert "-quality" in opts
            assert opts[opts.index("-quality") + 1] == quality
            assert "-b:v" not in opts

    def test_source_video_quality_uses_source_bitrate_in_bitrate_mode(self):
        for enc in ("h264_mf", "h264_amf", "h264_nvenc", "libx264"):
            opts = encoder_opts(enc, "source", source_bitrate=5_432_100)
            assert "-b:v" in opts
            assert opts[opts.index("-b:v") + 1] == "5432k"
            assert "-crf" not in opts

    def test_source_video_quality_keeps_operational_x264_opts(self):
        opts = encoder_opts(
            "libx264",
            "source",
            x264_preset="veryfast",
            encoder_threads=2,
            x264_low_memory=True,
            source_bitrate=5_432_100,
        )
        assert opts[opts.index("-b:v") + 1] == "5432k"
        assert opts[opts.index("-preset") + 1] == "veryfast"
        assert "-threads" in opts
        assert "2" in opts
        assert "-x264-params" in opts

    def test_source_video_quality_maps_to_high_in_crf_mode(self):
        for enc in ("h264_mf", "h264_amf", "h264_nvenc", "libx264"):
            opts = encoder_opts(enc, "source", use_crf=True)
            assert "-b:v" not in opts
            if enc == "libx264":
                assert opts[opts.index("-crf") + 1] == "18"
            elif enc == "h264_nvenc":
                assert opts[opts.index("-cq") + 1] == "18"
            elif enc == "h264_amf":
                assert opts[opts.index("-qp_i") + 1] == "18"
            else:
                assert opts[opts.index("-quality") + 1] == "100"

    def test_unknown_encoder_raises(self):
        with pytest.raises(ConcatError, match="Unknown encoder"):
            encoder_opts("vp9", "medium")

    def test_unknown_quality_raises(self):
        with pytest.raises(ConcatError, match="Unknown video quality"):
            encoder_opts("libx264", "ultra")

    def test_get_video_encoder_passes_quality(self):
        # libx264 always passes the encoder check. Verify the quality
        # preset flows through get_video_encoder into the returned opts.
        for q in ("source", "high", "medium", "low"):
            enc, opts = get_video_encoder("libx264", q)
            assert enc == "libx264"
            assert opts == encoder_opts("libx264", q)

    def test_get_video_encoder_fallback_carries_quality(self):
        """When the primary encoder is unavailable, the libx264 fallback
        must use the same video_quality preset (CRF) the user requested.
        """
        from stream2video.concat import _X264_CRF

        calls: list[tuple[str, list[str]]] = []

        def _try_fn(enc, opts):
            calls.append((enc, opts))
            if enc == "h264_nvenc":
                raise ConcatError("nvenc unavailable")

        with patch("stream2video.concat.check_encoder", return_value=False):
            _with_libx264_fallback(
                "h264_nvenc",
                encoder_opts("h264_nvenc", "low"),
                _try_fn,
                (ConcatError, OSError),
                None,
                video_quality="low",
                software_fallback="enabled",
                use_crf=True,
            )

        # Two attempts: first h264_nvenc (fails), then libx264 (succeeds).
        assert [enc for enc, _ in calls] == ["h264_nvenc", "libx264"]
        # The fallback libx264 call must use CRF 28 (low preset).
        libx264_opts = calls[-1][1]
        crf_idx = libx264_opts.index("-crf")
        assert libx264_opts[crf_idx + 1] == _X264_CRF["low"]


class TestEncoderThreadsPosition:
    """``-threads`` must appear after the encoder spec and before output path.

    The fix plan (Этап 3, item 17) requires that ``-threads N`` is
    positioned AFTER ``-c:v libx264`` (or the HW encoder equivalent) so
    it caps the encoder's thread pool, not the decoder's. This is tested
    at the ``encoder_opts()`` level since the full command construction
    appends opts after the encoder declaration.
    """

    def test_auto_omits_threads_flag(self):
        for enc in ("h264_mf", "h264_amf", "h264_nvenc", "libx264"):
            opts = encoder_opts(enc, "medium", x264_preset="medium", encoder_threads="auto")
            assert "-threads" not in opts, f"{enc}: auto should not add -threads"

    def test_explicit_threads_appended_at_end(self):
        for enc in ("h264_mf", "h264_amf", "h264_nvenc", "libx264"):
            opts = encoder_opts(enc, "medium", x264_preset="medium", encoder_threads=2)
            assert "-threads" in opts, f"{enc}: explicit threads should add -threads"
            # ``-threads`` must be one of the final arguments (after codec opts).
            # ``encoder_opts()`` appends ``*threads_opt`` last, so ``-threads``
            # must appear in the last two positions.
            threads_idx = opts.index("-threads")
            assert threads_idx >= len(opts) - 2, (
                f"{enc}: -threads at position {threads_idx}/{len(opts)} "
                f"but expected near end: {opts}"
            )
            assert opts[threads_idx + 1] == "2", f"{enc}: -threads value should be '2'"

    def test_low_memory_adds_x264_params_only_for_libx264(self):
        for enc in ("h264_mf", "h264_amf", "h264_nvenc"):
            opts = encoder_opts(enc, "medium", x264_low_memory=True)
            assert "-x264-params" not in opts, f"{enc}: low_memory should not affect HW encoders"
        opts = encoder_opts("libx264", "medium", x264_low_memory=True)
        assert "-x264-params" in opts
        assert "rc-lookahead=10" in " ".join(opts)
        assert "ref=1" in " ".join(opts)
        assert "bframes=0" in " ".join(opts)

    def test_low_memory_omitted_by_default(self):
        opts = encoder_opts("libx264", "medium")
        assert "-x264-params" not in opts


class TestResumeManifestValidation:
    """P3.4 / fix-plan §4 Resume/failure: manifest mismatch scenarios.

    These cover the resume-failure matrix items that don't need a real
    ffmpeg encode — manifest validation is pure dict comparison and can
    be unit-tested without subprocess. The crash-mid-encode scenarios
    (segment / batch) require a fake subprocess and are deferred to
    the integration test matrix.
    """

    def _build_base_manifest(
        self, video_path: Path, keep_segments: list[tuple[float, float]]
    ) -> dict:
        return _build_manifest(
            video_path=video_path,
            keep_segments=keep_segments,
            method="segment",
            encoder="libx264",
            vcodec="libx264",
            vcodec_opts=["-preset", "medium"],
            video_quality="medium",
            audio_quality="medium",
            x264_preset="medium",
            encoder_threads="auto",
        )

    def test_matching_manifest_is_valid(self, tmp_path: Path):
        # Baseline: a manifest written by the current run validates True.
        # Without this, all the mismatch tests below are meaningless.
        video = tmp_path / "src.mp4"
        video.write_bytes(b"video data")
        keep = [(0.0, 2.0), (4.0, 6.0)]
        current = self._build_base_manifest(video, keep)
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        from stream2video.concat import _write_manifest

        _write_manifest(work_dir, current)
        assert _validate_manifest(work_dir, current) is True

    def test_encoder_change_after_crash_invalidates(self, tmp_path: Path):
        # Crash mid-encode, user switches encoder h264_mf → libx264.
        # Old segments (h264_mf-encoded) must NOT be reused by the
        # libx264 retry — manifest mismatch on `encoder` catches it.
        video = tmp_path / "src.mp4"
        video.write_bytes(b"video data")
        keep = [(0.0, 2.0)]
        old = self._build_base_manifest(video, keep)
        old["encoder"] = "h264_mf"
        old["resolved_encoder"] = "h264_mf"
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        from stream2video.concat import _write_manifest

        _write_manifest(work_dir, old)
        current = self._build_base_manifest(video, keep)
        assert _validate_manifest(work_dir, current) is False

    def test_quality_change_after_crash_invalidates(self, tmp_path: Path):
        # User changes video_quality medium → high after a crash. Old
        # segments encoded at medium bitrate must not be reused.
        video = tmp_path / "src.mp4"
        video.write_bytes(b"video data")
        keep = [(0.0, 2.0)]
        old = self._build_base_manifest(video, keep)
        old["video_quality"] = "low"
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        from stream2video.concat import _write_manifest

        _write_manifest(work_dir, old)
        current = self._build_base_manifest(video, keep)
        assert _validate_manifest(work_dir, current) is False

    def test_source_swap_same_filename_invalidates(self, tmp_path: Path):
        # User replaces src.mp4 with a different file (same name, different
        # content / size / mtime). The keep_segments boundaries are now
        # meaningless against the new content — source identity check
        # (path/size/mtime_ns) catches the swap.
        video = tmp_path / "src.mp4"
        video.write_bytes(b"original content")
        keep = [(0.0, 2.0)]
        old = self._build_base_manifest(video, keep)
        # Simulate the user replacing the file: mtime changes when the
        # filesystem rewrites it, and size may change too.
        video.write_bytes(b"replacement content - different size")
        # Force a newer mtime so the identity check fires even on
        # filesystems with coarse mtime resolution.
        import os
        import time

        os.utime(video, (time.time() + 10, time.time() + 10))
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        from stream2video.concat import _write_manifest

        _write_manifest(work_dir, old)
        current = self._build_base_manifest(video, keep)
        assert _validate_manifest(work_dir, current) is False

    def test_keep_segments_change_invalidates(self, tmp_path: Path):
        # User adjusts threshold/min_silence/margin after a crash → the
        # keep_segments list changes. Old segments encoded for the old
        # boundaries don't align with the new ones.
        video = tmp_path / "src.mp4"
        video.write_bytes(b"video data")
        old_keep = [(0.0, 2.0), (4.0, 6.0)]
        old = self._build_base_manifest(video, old_keep)
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        from stream2video.concat import _write_manifest

        _write_manifest(work_dir, old)
        new_keep = [(0.0, 3.0), (5.0, 6.0)]
        current = self._build_base_manifest(video, new_keep)
        assert _validate_manifest(work_dir, current) is False

    def test_pipeline_version_change_invalidates(self, tmp_path: Path):
        # After a stream2video upgrade, the on-disk manifest was written
        # by an older pipeline version. New version may use different
        # encoder opts / segment boundaries → must re-encode from scratch.
        video = tmp_path / "src.mp4"
        video.write_bytes(b"video data")
        keep = [(0.0, 2.0)]
        old = self._build_base_manifest(video, keep)
        old["pipeline_version"] = "0.1"  # older version
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        from stream2video.concat import _write_manifest

        _write_manifest(work_dir, old)
        current = self._build_base_manifest(video, keep)
        assert _validate_manifest(work_dir, current) is False

    def test_missing_manifest_is_invalid(self, tmp_path: Path):
        # Work dir exists but no _manifest.json (crash before manifest
        # was written, or pre-manifest system). Must be treated as
        # stale so segments aren't blindly reused.
        video = tmp_path / "src.mp4"
        video.write_bytes(b"video data")
        keep = [(0.0, 2.0)]
        current = self._build_base_manifest(video, keep)
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        # No _write_manifest call — dir is empty.
        assert _validate_manifest(work_dir, current) is False

    def test_corrupt_manifest_is_invalid(self, tmp_path: Path):
        # _manifest.json exists but is corrupt JSON (e.g. crash mid-write
        # truncated it). _load_manifest returns None → treated as stale.
        video = tmp_path / "src.mp4"
        video.write_bytes(b"video data")
        keep = [(0.0, 2.0)]
        current = self._build_base_manifest(video, keep)
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        # Write garbage to the manifest path.
        _manifest_path(work_dir).write_text("{ this is not valid json")
        assert _validate_manifest(work_dir, current) is False


class TestSourceIdentity:
    """_source_identity captures the signals used to detect a swapped
    source file. Pinned so a future refactor doesn't silently drop one
    of the fields _validate_manifest compares against."""

    def test_captures_path_size_mtime(self, tmp_path: Path):
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"x" * 1234)
        ident = _source_identity(video)
        assert "path" in ident
        assert "size" in ident
        assert "mtime_ns" in ident
        assert ident["size"] == 1234
        # mtime_ns matches the filesystem stat (so a re-stat of the same
        # file produces the same identity, but a rewrite changes it).
        import os

        assert ident["mtime_ns"] == os.stat(video).st_mtime_ns

    def test_different_files_have_different_identity(self, tmp_path: Path):
        # Two distinct files must produce distinct identities so a
        # keep-segment list built against file A doesn't get reused
        # when file B is at the same path.
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"content A")
        ident_a = _source_identity(video)
        video.write_bytes(b"content B is longer than A")
        ident_b = _source_identity(video)
        assert ident_a != ident_b


class TestFfprobeIsValidMp4:
    """_ffprobe_is_valid_mp4 — corrupt/missing-moov detection (fix-plan §4
    Resume/failure: corrupt/missing-moov temp file).

    Skipped when ffprobe isn't on PATH so the suite still runs in
    environments without ffmpeg installed.
    """

    def _have_ffprobe(self) -> bool:
        import shutil

        return shutil.which("ffprobe") is not None

    def test_missing_file_is_invalid(self, tmp_path: Path):
        # Non-existent path → False (ffprobe can't open it).
        assert _ffprobe_is_valid_mp4(tmp_path / "does_not_exist.mp4") is False

    def test_truncated_file_is_invalid(self, tmp_path: Path):
        # A few random bytes are not a valid MP4 — ffprobe fails to
        # parse the moov atom and returns non-zero. This is the
        # crash-mid-encode scenario: ffmpeg was killed before flushing
        # the moov atom, leaving a truncated .mp4 on disk.
        if not self._have_ffprobe():
            pytest.skip("ffprobe not available")
        corrupt = tmp_path / "truncated.mp4"
        corrupt.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00")
        assert _ffprobe_is_valid_mp4(corrupt) is False

    def test_random_bytes_is_invalid(self, tmp_path: Path):
        # Pure noise — no MP4 structure at all.
        if not self._have_ffprobe():
            pytest.skip("ffprobe not available")
        corrupt = tmp_path / "noise.mp4"
        corrupt.write_bytes(b"this is definitely not an mp4 file")
        assert _ffprobe_is_valid_mp4(corrupt) is False


class TestFfprobeIsValidMedia:
    """_ffprobe_is_valid_media — stream_type-aware validity probe
    (P0 audit: resume in audio extract never skipped, because the
    video-stream probe rejected every valid audio chunk)."""

    def _have_ffmpeg(self) -> bool:
        import shutil

        return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None

    def test_valid_audio_passes_with_stream_type_a(self, tmp_path: Path):
        """A real, complete audio file (mp3) has an audio stream but no
        video stream. With stream_type="a" ffprobe returns 0 → valid."""
        if not self._have_ffmpeg():
            pytest.skip("ffmpeg/ffprobe not available")
        out = tmp_path / "tone.mp3"
        import subprocess

        from stream2video.utils import no_window_kwargs

        r = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=0.2",
                "-c:a",
                "libmp3lame",
                str(out),
            ],
            capture_output=True,
            text=True,
            **no_window_kwargs(),
        )
        assert r.returncode == 0, r.stderr
        assert out.exists() and out.stat().st_size > 0
        assert _ffprobe_is_valid_media(out, stream_type="a") is True

    def test_valid_audio_fails_with_stream_type_v(self, tmp_path: Path):
        """Regression for the P0 bug: the same valid mp3 with a video
        probe (stream_type="v") must be reported invalid, which is why
        the old _ffprobe_is_valid_mp4 call rejected every resume chunk
        in audio extract."""
        if not self._have_ffmpeg():
            pytest.skip("ffmpeg/ffprobe not available")
        out = tmp_path / "tone.mp3"
        import subprocess

        from stream2video.utils import no_window_kwargs

        r = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=0.2",
                "-c:a",
                "libmp3lame",
                str(out),
            ],
            capture_output=True,
            text=True,
            **no_window_kwargs(),
        )
        assert r.returncode == 0, r.stderr
        assert _ffprobe_is_valid_media(out, stream_type="v") is False

    def test_truncated_audio_is_invalid_stream_type_a(self, tmp_path: Path):
        """Truncated mp3 — ffmpeg crashed before writing the final
        frames. ffprobe should reject it (no valid audio stream)."""
        if not self._have_ffmpeg():
            pytest.skip("ffmpeg/ffprobe not available")
        corrupt = tmp_path / "truncated.mp3"
        corrupt.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x00some garbage")
        assert _ffprobe_is_valid_media(corrupt, stream_type="a") is False

    def test_default_stream_type_is_video(self, tmp_path: Path):
        """The default of stream_type is "v" — keeps the historical
        behaviour of _ffprobe_is_valid_mp4 for video paths."""
        assert _ffprobe_is_valid_media(tmp_path / "missing.mp4") is False


class TestSegmentResumeSkipCrashArtifact:
    """Fix-plan section 4 Resume/failure: crash mid-segment.

    When ffmpeg crashes mid-encode (e.g. power failure, OOM kill), it
    leaves a truncated MP4 without a moov atom on disk. On the next run,
    ``_run_segment_concat`` must detect the corrupt file via
    ``_ffprobe_is_valid_mp4`` and re-encode it, instead of blindly
    reusing it (which would corrupt the final concat at that segment
    boundary).

    This test mocks ``_run_ffmpeg`` / ``_ffprobe_is_valid_mp4`` /
    ``_run_final_concat`` / ``_ensure_fresh_work_dir`` so it runs
    without a real ffmpeg binary; the assertions verify the skip
    decision and re-encode invocation, not the encoded output.
    """

    def test_corrupt_segment_is_re_encoded_not_skipped(self, tmp_path: Path):
        """A segment that exists on disk but fails ffprobe must be
        re-encoded, not skipped. A valid segment must be skipped."""
        from unittest.mock import patch

        video = tmp_path / "src.mp4"
        video.write_bytes(b"fake video data")
        output = tmp_path / "out.mp4"
        keep = [(0.0, 2.0), (2.0, 4.0)]

        seg_dir = tmp_path / f"_{output.stem}_segments"
        seg_dir.mkdir()
        # seg_000000: truncated (crash artifact — ffprobe will fail).
        # Must be >= min_part_bytes (1024) so the size check passes and
        # the ffprobe check fires (otherwise it's re-encoded purely on
        # size, which doesn't test the ffprobe path).
        (seg_dir / "seg_000000.mp4").write_bytes(b"\x00" * 2048)
        # seg_000001: valid-looking (ffprobe will pass)
        (seg_dir / "seg_000001.mp4").write_bytes(b"\x00" * 2048)

        encode_calls: list[str] = []

        def fake_run_ffmpeg(cmd, *args, **kwargs):
            # The output path is the last positional arg in the ffmpeg
            # command (after all -i / -filter / -c:v flags). Input
            # paths sit after ``-i`` and must not be counted as encodes.
            out = str(cmd[-1])
            if out.endswith(".mp4") and ("seg_" in out or "chunk_" in out):
                encode_calls.append(out)
                Path(out).write_bytes(b"re-encoded valid mp4")
            return None

        def fake_ffprobe(path):
            # First segment is corrupt (crash artifact), second is valid.
            return Path(path).name != "seg_000000.mp4"

        # The resume check now also probes the audio stream for sources
        # with audio (mirrors cut_encode.py). Return True so the extra
        # probe doesn't re-encode "valid" segments; the test's intent is
        # to exercise the video-stream gate, not ffmpeg detail probing.
        def fake_ffprobe_any_stream(path, stream_type="v"):
            return fake_ffprobe(path)

        with (
            patch("stream2video.concat._run_ffmpeg", side_effect=fake_run_ffmpeg),
            patch("stream2video.concat._ffprobe_is_valid_mp4", side_effect=fake_ffprobe),
            patch(
                "stream2video.concat._ffprobe_is_valid_media",
                side_effect=fake_ffprobe_any_stream,
            ),
            patch("stream2video.concat._run_final_concat"),
            patch("stream2video.concat._ensure_fresh_work_dir"),
        ):
            from stream2video.concat import _run_segment_concat

            _run_segment_concat(
                video,
                keep,
                output,
                "libx264",
                ["-preset", "medium"],
                None,  # progress_callback
                None,  # cancel_callback
            )

        # The corrupt segment (seg_000000) was re-encoded via _run_ffmpeg.
        assert len(encode_calls) == 1, (
            f"Expected 1 re-encode call (for the corrupt segment), "
            f"got {len(encode_calls)}: {encode_calls}"
        )
        assert "seg_000000.mp4" in encode_calls[0]

    def test_all_valid_segments_are_skipped(self, tmp_path: Path):
        """When all segments are valid (ffprobe passes), none are
        re-encoded — the resume-skip logic short-circuits all of them."""
        from unittest.mock import patch

        video = tmp_path / "src.mp4"
        video.write_bytes(b"fake video data")
        output = tmp_path / "out.mp4"
        keep = [(0.0, 2.0), (2.0, 4.0)]

        seg_dir = tmp_path / f"_{output.stem}_segments"
        seg_dir.mkdir()
        for i in range(2):
            # Must be >= min_part_bytes (1024) so the size check passes.
            (seg_dir / f"seg_{i:06d}.mp4").write_bytes(b"\x00" * 2048)

        encode_calls: list[str] = []

        def fake_run_ffmpeg(cmd, *args, **kwargs):
            out = str(cmd[-1])
            if out.endswith(".mp4") and ("seg_" in out or "chunk_" in out):
                encode_calls.append(out)
                Path(out).write_bytes(b"re-encoded valid mp4")
            return None

        with (
            patch("stream2video.concat._run_ffmpeg", side_effect=fake_run_ffmpeg),
            patch("stream2video.concat._ffprobe_is_valid_mp4", return_value=True),
            # Resume check also probes the audio stream — stub it so
            # valid-video segments still count as fully valid.
            patch("stream2video.concat._ffprobe_is_valid_media", return_value=True),
            patch("stream2video.concat._run_final_concat"),
            patch("stream2video.concat._ensure_fresh_work_dir"),
        ):
            from stream2video.concat import _run_segment_concat

            _run_segment_concat(
                video,
                keep,
                output,
                "libx264",
                ["-preset", "medium"],
                None,
                None,
            )

        assert encode_calls == [], (
            f"All segments were valid — none should be re-encoded, "
            f"but _run_ffmpeg was called for: {encode_calls}"
        )

    def test_small_file_below_threshold_is_re_encoded(self, tmp_path: Path):
        """A segment file smaller than ``min_part_bytes`` (default 1024)
        is treated as a crash artifact and re-encoded, even if ffprobe
        would have passed (the file is too small to be a valid encode)."""
        from unittest.mock import patch

        video = tmp_path / "src.mp4"
        video.write_bytes(b"fake video data")
        output = tmp_path / "out.mp4"
        keep = [(0.0, 2.0)]

        seg_dir = tmp_path / f"_{output.stem}_segments"
        seg_dir.mkdir()
        # Write a tiny file (10 bytes, well below the 1024 threshold)
        (seg_dir / "seg_000000.mp4").write_bytes(b"tiny")

        encode_calls: list[str] = []

        def fake_run_ffmpeg(cmd, *args, **kwargs):
            out = str(cmd[-1])
            if out.endswith(".mp4") and ("seg_" in out or "chunk_" in out):
                encode_calls.append(out)
                Path(out).write_bytes(b"re-encoded valid mp4")
            return None

        with (
            patch("stream2video.concat._run_ffmpeg", side_effect=fake_run_ffmpeg),
            patch("stream2video.concat._ffprobe_is_valid_mp4", return_value=True),
            patch("stream2video.concat._run_final_concat"),
            patch("stream2video.concat._ensure_fresh_work_dir"),
        ):
            from stream2video.concat import _run_segment_concat

            _run_segment_concat(
                video,
                keep,
                output,
                "libx264",
                ["-preset", "medium"],
                None,
                None,
            )

        # The tiny file was below min_part_bytes → re-encoded despite
        # ffprobe returning True (the size check fires before ffprobe).
        assert len(encode_calls) == 1, (
            f"Expected 1 re-encode (file below min_part_bytes), got {len(encode_calls)}"
        )


class TestBatchResumeSkipCrashArtifact:
    """Fix-plan section 4 Resume/failure: crash mid-batch.

    Same scenario as TestSegmentResumeSkipCrashArtifact but for the
    batch path: a truncated chunk file (crash artifact) must be
    re-encoded, not reused.
    """

    def test_corrupt_chunk_is_re_encoded(self, tmp_path: Path):
        from unittest.mock import patch

        video = tmp_path / "src.mp4"
        video.write_bytes(b"fake video data")
        output = tmp_path / "out.mp4"
        keep = [(0.0, 2.0), (2.0, 4.0)]

        batch_dir = tmp_path / f"_{output.stem}_batch"
        batch_dir.mkdir()
        # chunk_0000: truncated (crash artifact). Must be >= min_part_bytes.
        (batch_dir / "chunk_0000.mp4").write_bytes(b"\x00" * 2048)
        # chunk_0001: valid
        (batch_dir / "chunk_0001.mp4").write_bytes(b"\x00" * 2048)

        encode_calls: list[str] = []

        def fake_run_ffmpeg(cmd, *args, **kwargs):
            out = str(cmd[-1])
            if out.endswith(".mp4") and ("seg_" in out or "chunk_" in out):
                encode_calls.append(out)
                Path(out).write_bytes(b"re-encoded valid mp4")
            return None

        def fake_ffprobe(path):
            return Path(path).name != "chunk_0000.mp4"

        with (
            patch("stream2video.concat._run_ffmpeg", side_effect=fake_run_ffmpeg),
            patch("stream2video.concat._ffprobe_is_valid_mp4", side_effect=fake_ffprobe),
            patch("stream2video.concat._run_final_concat"),
            patch("stream2video.concat._ensure_fresh_work_dir"),
        ):
            from stream2video.concat import _run_batch_concat

            _run_batch_concat(
                video,
                keep,
                output,
                "libx264",
                ["-preset", "medium"],
                None,
                None,
            )

        assert len(encode_calls) == 1, (
            f"Expected 1 re-encode (corrupt chunk), got {len(encode_calls)}: {encode_calls}"
        )
        assert "chunk_0000.mp4" in encode_calls[0]


class TestAudioExtractResumeStreamType:
    """Resume in _run_audio_extract must use the audio-stream probe.

    Regression for the P0 bug: _run_audio_extract used
    _ffprobe_is_valid_mp4 (a video-stream probe) to validate per-segment
    mp3/opus/aac/wav/flac chunks. Audio-only chunks have no video
    stream, so the probe always returned False, meaning *every* resume
    chunk was treated as corrupt and re-encoded — the resume code path
    was effectively dead.

    This test mocks _run_ffmpeg/_ffprobe_is_valid_media/_ensure_fresh_work_dir
    so it runs without an ffmpeg binary; the assertions check that
    a valid on-disk segment is *skipped* (no encode call), which only
    happens if the audio probe accepts it.
    """

    def test_valid_audio_segment_is_skipped_on_resume(self, tmp_path: Path):
        from unittest.mock import patch

        video = tmp_path / "src.mkv"
        video.write_bytes(b"fake source")
        output = tmp_path / "out.mp3"
        keep = [(0.0, 2.0), (2.0, 4.0)]

        work_dir = tmp_path / f"_{output.stem}_audio_mp3"
        work_dir.mkdir()
        # Pre-existing valid segments from a previous run. Size >=
        # min_part_bytes so the size check passes and the ffprobe
        # check fires (otherwise the skip happens on size alone and
        # the probe path isn't exercised).
        for i in range(2):
            (work_dir / f"seg_{i:06d}.mp3").write_bytes(b"\x00" * 2048)

        encode_calls: list[str] = []

        def fake_run_ffmpeg(cmd, *args, **kwargs):
            out = str(cmd[-1])
            if out.endswith(".mp3") and "seg_" in out:
                encode_calls.append(out)
                Path(out).write_bytes(b"re-encoded valid mp3 data")
            return None

        with (
            patch("stream2video.concat._run_ffmpeg", side_effect=fake_run_ffmpeg),
            # Pretend the probe accepts every chunk (valid audio).
            patch("stream2video.concat._ffprobe_is_valid_media", return_value=True),
            patch("stream2video.concat._run_audio_concat_filter") as m_acf,
            patch("stream2video.concat._run_final_concat") as m_fc,
            patch("stream2video.concat._ensure_fresh_work_dir"),
        ):
            # Make the audio-concat-dispatch pick the demuxer path for mp3
            # (not flac) so _run_final_concat is the expected join.
            from stream2video.concat import _run_audio_extract

            _run_audio_extract(
                video,
                keep,
                output,
                "mp3",
                None,
                None,
            )

        # Resume should have skipped both segments — no encode calls.
        assert encode_calls == [], (
            f"Both segments were valid audio chunks — none should have been "
            f"re-encoded, but _run_ffmpeg was called for: {encode_calls}"
        )
        # And the demuxer join should have run for mp3 (the flac filter
        # path is only chosen for output_format=="flac").
        assert m_acf.call_count == 0
        assert m_fc.call_count == 1


class TestAudioQualityParametric:
    """_audio_bitrate / _audio_opts are now parameters (P1 audit v0.3 §6.1):
    the module-level ``_audio_quality`` global is gone. The bitrate picker
    must reflect the *passed* value, and two consecutive calls with
    different values must not influence each other (the original bug —
    since the global was set once per ``cut_and_concat`` invocation, a
    second run premultiplying ``high`` first then a ``low`` run silently
    inherited 'high' from the prior state if the setter was bypassed by
    a code path that didn't call _set_audio_quality)."""

    def test_audio_bitrate_high(self):
        from stream2video.concat import _audio_bitrate

        assert _audio_bitrate("high") == "256k"

    def test_audio_bitrate_medium(self):
        from stream2video.concat import _audio_bitrate

        assert _audio_bitrate("medium") == "192k"

    def test_audio_bitrate_low(self):
        from stream2video.concat import _audio_bitrate

        assert _audio_bitrate("low") == "128k"

    def test_audio_bitrate_source_omits_bitrate(self):
        from stream2video.concat import _audio_bitrate, _audio_bitrate_opts

        assert _audio_bitrate("source") == ""
        assert _audio_bitrate_opts("source") == []

    def test_audio_bitrate_invalid_raises(self):
        from stream2video.concat import ConcatError, _audio_bitrate

        with pytest.raises(ConcatError, match="Unknown audio quality"):
            _audio_bitrate("ultra")

    def test_audio_bitrate_empty_falls_back_to_default(self):
        from stream2video.concat import _audio_bitrate

        assert _audio_bitrate("") == "128k"

    def test_audio_opts_returns_required_listing(self):
        from stream2video.concat import _audio_opts

        opts = _audio_opts("medium")
        assert "-ar" in opts
        assert "48000" in opts
        assert "-ac" in opts
        assert "2" in opts

    def test_audio_opts_source_preserves_native_stream_shape(self):
        from stream2video.concat import _audio_opts

        assert _audio_opts("source") == []

    def test_audio_opts_rejects_unknown_quality(self):
        """_audio_opts validates the quality so a typo propagates to a
        ConcatError rather than silently producing mediocre output."""
        from stream2video.concat import ConcatError, _audio_opts

        with pytest.raises(ConcatError, match="Unknown audio quality"):
            _audio_opts("bogus")

    def test_audio_bitrate_double_call_is_stateless(self):
        """Two consecutive calls with different quality must not
        influence each other — the parameter is the source of truth,
        not a shared mutable global."""
        from stream2video.concat import _audio_bitrate

        assert _audio_bitrate("high") == "256k"
        assert _audio_bitrate("low") == "128k"
        assert _audio_bitrate("medium") == "192k"

    def test_cut_and_concat_forwards_audio_quality_per_call(self, tmp_path: Path):
        """Back-to-back pipeline calls must carry their own audio_quality.

        This stays cheap by patching out ffmpeg work; the regression signal is
        that ``cut_and_concat`` no longer writes a module-level preset that a
        later run can accidentally inherit.
        """
        from stream2video.concat import cut_and_concat

        video = tmp_path / "src.mp4"
        video.write_bytes(b"source")
        output = tmp_path / "out.mp4"
        seen: list[str] = []

        def fake_run_with_fallback(*args, **kwargs):
            seen.append(kwargs["audio_quality"])

        with (
            patch("stream2video.concat.get_video_duration", return_value=2.0),
            patch(
                "stream2video.concat.get_video_encoder", return_value=("libx264", ["-crf", "23"])
            ),
            patch("stream2video.concat.has_audio_stream", return_value=True),
            patch("stream2video.concat._run_with_fallback", side_effect=fake_run_with_fallback),
        ):
            cut_and_concat(video, [], output, audio_quality="high")
            cut_and_concat(video, [], output, audio_quality="low")

        assert seen == ["high", "low"]


class TestCutThenEncodeCutPhaseProtection:
    """Phase-1 cut-фаза in _run_cut_then_encode previously ran via a bare
    ``subprocess.run(check=True, capture_output=True)`` with no timeout,
    no cancel, no process registration, and the resulting
    ``CalledProcessError`` did NOT match the ``exc_types`` filter in
    ``_with_libx264_fallback`` — so a corrupt-source cut surfaced as a raw
    traceback (P0 audit v0.3 §3). Tests below mock the helper
    ``_run_subprocess_cmd`` and assert the right exceptions are raised
    so the GUI/CLI see a friendly message instead.
    """

    def test_cut_phase_called_in_phase1(self, tmp_path: Path):
        from unittest.mock import patch

        video = tmp_path / "src.mp4"
        video.write_bytes(b"source")
        output = tmp_path / "out.mkv"
        keep = [(0.0, 1.0), (1.0, 2.0)]
        calls: list[str] = []

        def fake_ffmpeg_helper(cmd, *, timeout, label, **kwargs):
            # Track the "cut phase segment N" calls only — out_path is last arg.
            out = str(cmd[-1])
            calls.append(out)
            Path(out).write_bytes(b"\x00" * 2048)
            return None

        with (
            # Phase-1 cut now runs each segment through _run_ffmpeg (the
            # lossless ``-ss``→``-t``→``-c:v`` encode needs the progress
            # pump + stall watchdog + memory monitor that ``_run_ffmpeg``
            # provides). Phase-3 mux uses the lighter ``_run_subprocess_cmd``
            # for the ``-c copy`` rewrite — keep it mocked as a no-op.
            patch("stream2video.concat._run_ffmpeg", side_effect=fake_ffmpeg_helper),
            patch("stream2video.concat._run_subprocess_cmd"),
            patch("stream2video.concat._run_final_concat"),
            patch("stream2video.concat._ffprobe_is_valid_mp4", return_value=True),
            patch("stream2video.concat._ffprobe_is_valid_media", return_value=True),
            patch("stream2video.concat._ffprobe_duration_ok", return_value=True),
            patch("stream2video.concat._ensure_fresh_work_dir"),
        ):
            from stream2video.concat import _run_cut_then_encode

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
        # Phase-1 cut encode runs exactly once per keep segment.
        assert len(calls) == 2, calls

    def test_cut_phase_concat_distance_wraps_in_concat_error(self, tmp_path: Path):
        from unittest.mock import patch

        from stream2video.concat import ConcatError, _run_cut_then_encode

        video = tmp_path / "src.mp4"
        video.write_bytes(b"source")
        output = tmp_path / "out.mkv"
        keep = [(0.0, 1.0)]

        def failed_helper(cmd, *, timeout, label, **kwargs):
            # Simulate a corrupt-source ffmpeg failure during phase-1 cut encode.
            raise ConcatError(f"{label} failed (rc=1): Streamcopy failed at sub-zero pts")

        with (
            # Phase-1 cut encode now runs via _run_ffmpeg; phase-3 mux
            # uses _run_subprocess_cmd — keep both mocked as no-ops so the
            # test only observes the deliberate failure in phase 1.
            patch("stream2video.concat._run_ffmpeg", side_effect=failed_helper),
            patch("stream2video.concat._run_subprocess_cmd"),
            patch("stream2video.concat._ensure_fresh_work_dir"),
            pytest.raises(ConcatError, match="cut phase segment"),
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

    def test_cut_phase_cancel_raises_cancelled_error(self, tmp_path: Path):
        from unittest.mock import patch

        from stream2video.concat import CancelledError, _run_cut_then_encode

        video = tmp_path / "src.mp4"
        video.write_bytes(b"source")
        output = tmp_path / "out.mkv"
        keep = [(0.0, 1.0)]

        def cancelled_helper(cmd, *, timeout, label, **kwargs):
            raise CancelledError("cut cancelled")

        with (
            # Phase-1 cut now uses _run_ffmpeg (frame-accurate encode
            # with the chosen codec), so the CancelledError surfaces
            # there; phase-3 mux uses _run_subprocess_cmd and is mocked
            # as a no-op (the test never reaches phase 3 because the
            # cut phase aborts first).
            patch("stream2video.concat._run_ffmpeg", side_effect=cancelled_helper),
            patch("stream2video.concat._run_subprocess_cmd"),
            patch("stream2video.concat._ensure_fresh_work_dir"),
            pytest.raises(CancelledError),
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

    def test_cut_phase_timeout_raises_ffmpeg_error(self, tmp_path: Path):
        from unittest.mock import patch

        from stream2video.concat import FFmpegError, _run_cut_then_encode

        video = tmp_path / "src.mp4"
        video.write_bytes(b"source")
        output = tmp_path / "out.mkv"
        keep = [(0.0, 1.0)]

        def timeout_helper(cmd, *, timeout, label, **kwargs):
            raise FFmpegError(f"{label} timeout after 600s")

        with (
            # Phase-1 cut encode is the only path that runs _run_ffmpeg;
            # mock _run_subprocess_cmd as a no-op so the test never
            # exercises phase-3.
            patch("stream2video.concat._run_ffmpeg", side_effect=timeout_helper),
            patch("stream2video.concat._run_subprocess_cmd"),
            patch("stream2video.concat._ensure_fresh_work_dir"),
            pytest.raises(FFmpegError, match="timeout"),
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
