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
    _run_ffmpeg,
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
                "-preset", "p7", "-rc", "vbr", "-b:v", "7000k",
                "-maxrate", "7000k", "-cq", "18",
            ],
            "libx264": ["-crf", "23", "-preset", "medium"],
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

    def test_hw_bitrate_tracks_quality(self):
        from stream2video.concat import _VIDEO_BITRATES

        assert _VIDEO_BITRATES == {"high": "10000k", "medium": "7000k", "low": "3500k"}
        for enc in ("h264_mf", "h264_amf", "h264_nvenc"):
            for q, br in _VIDEO_BITRATES.items():
                opts = encoder_opts(enc, q)
                assert "-b:v" in opts
                idx = opts.index("-b:v")
                assert opts[idx + 1] == br, f"{enc} {q}: -b:v must be {br}"
                if enc == "h264_nvenc":
                    assert "-maxrate" in opts
                    m_idx = opts.index("-maxrate")
                    assert opts[m_idx + 1] == br, f"{enc} {q}: -maxrate must be {br}"

    def test_libx264_crf_tracks_quality(self):
        from stream2video.concat import _X264_CRF

        assert _X264_CRF == {"high": "18", "medium": "23", "low": "28"}
        for q, crf in _X264_CRF.items():
            opts = encoder_opts("libx264", q)
            idx = opts.index("-crf")
            assert opts[idx + 1] == crf
            # libx264 ignores bitrate, so -b:v must NOT be present
            assert "-b:v" not in opts

    def test_unknown_encoder_raises(self):
        with pytest.raises(ConcatError, match="Unknown encoder"):
            encoder_opts("vp9", "medium")

    def test_unknown_quality_raises(self):
        with pytest.raises(ConcatError, match="Unknown video quality"):
            encoder_opts("libx264", "ultra")

    def test_get_video_encoder_passes_quality(self):
        # libx264 always passes the encoder check. Verify the quality
        # preset flows through get_video_encoder into the returned opts.
        for q in ("high", "medium", "low"):
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
            )

        # Two attempts: first h264_nvenc (fails), then libx264 (succeeds).
        assert [enc for enc, _ in calls] == ["h264_nvenc", "libx264"]
        # The fallback libx264 call must use CRF 28 (low preset).
        libx264_opts = calls[-1][1]
        crf_idx = libx264_opts.index("-crf")
        assert libx264_opts[crf_idx + 1] == _X264_CRF["low"]
