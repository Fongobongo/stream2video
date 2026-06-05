"""Integration tests for stream2video pipeline."""

import sys
import pytest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from stream2video.download import download
from stream2video.silence import SilenceSegment
from stream2video.concat import (
    ConcatError,
    cut_and_concat,
    generate_keep_segments,
    _run_ffmpeg,
)


class TestPipelineIntegration:
    """Integration tests for the full pipeline."""

    def test_full_pipeline_with_local_file(self):
        """Test pipeline with local file input."""
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
            SilenceSegment(5.0, 5.0),   # zero-duration
            SilenceSegment(6.0, 4.0),   # inverted
            SilenceSegment(2.0, 3.0),   # valid
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
        with patch("stream2video.concat.get_video_duration", return_value=0.0):
            with pytest.raises(Exception, match="Invalid video duration"):
                generate_keep_segments(Path("dummy.mp4"), [])

    def test_generate_keep_segments_no_duration(self):
        """Should raise when ffprobe can't determine duration."""
        with patch("stream2video.concat.get_video_duration", return_value=None):
            with pytest.raises(Exception, match="Could not determine"):
                generate_keep_segments(Path("dummy.mp4"), [])


class TestErrorRecovery:
    """Test error handling and recovery."""

    def test_missing_video_file_error(self):
        """Test appropriate error when video file not found."""
        with TemporaryDirectory() as tmpdir:
            with pytest.raises(ConcatError, match="not found"):
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

            with patch("stream2video.concat.get_video_duration", return_value=100.0):
                with pytest.raises(ConcatError, match="No video segments"):
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
        with patch("stream2video.concat.subprocess.Popen", side_effect=fake_popen):
            with pytest.raises(CancelledError):
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
            ffmpeg, "-y", "-v", "error",
            "-f", "lavfi", "-i", f"sine=frequency=1000:duration={duration}",
            "-f", "lavfi", "-i", "color=c=black:s=160x120:r=10",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-t", str(duration),
            str(out_path),
        ]
        subprocess.run(
            cmd, check=True,
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



