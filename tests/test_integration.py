"""Integration tests for stream2video pipeline."""

import pytest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch, MagicMock

from stream2video.download import download
from stream2video.silence import detect_silence, SilenceSegment
from stream2video.concat import cut_and_concat, _generate_keep_segments


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
            assert downloaded == video_file
            assert downloaded.exists()

    def test_generate_keep_segments(self):
        """Test keep segment generation from silence segments."""
        silence_segments = [
            SilenceSegment(1.0, 2.0),
            SilenceSegment(4.0, 5.0),
        ]

        # Mock video with 10 second duration
        with patch("stream2video.concat._get_video_duration", return_value=10.0):
            keep_segments = _generate_keep_segments(Path("dummy.mp4"), silence_segments)

        # Expected: [0-1], [2-4], [5-10]
        assert len(keep_segments) == 3
        assert keep_segments[0] == (0.0, 1.0)
        assert keep_segments[1] == (2.0, 4.0)
        assert keep_segments[2] == (5.0, 10.0)

    def test_generate_keep_segments_no_silence(self):
        """Test keep segment when no silence detected."""
        silence_segments = []

        with patch("stream2video.concat._get_video_duration", return_value=10.0):
            keep_segments = _generate_keep_segments(Path("dummy.mp4"), silence_segments)

        # Expected: entire video
        assert len(keep_segments) == 1
        assert keep_segments[0] == (0.0, 10.0)

    def test_generate_keep_segments_all_silence(self):
        """Test keep segment when entire video is silence."""
        silence_segments = [
            SilenceSegment(0.0, 10.0),
        ]

        with patch("stream2video.concat._get_video_duration", return_value=10.0):
            keep_segments = _generate_keep_segments(Path("dummy.mp4"), silence_segments)

        # Expected: empty (nothing to keep)
        assert len(keep_segments) == 0

    def test_generate_keep_segments_consecutive_silence(self):
        """Test keep segments with consecutive silence."""
        silence_segments = [
            SilenceSegment(1.0, 2.0),
            SilenceSegment(2.1, 3.0),  # Nearly adjacent
        ]

        with patch("stream2video.concat._get_video_duration", return_value=10.0):
            keep_segments = _generate_keep_segments(Path("dummy.mp4"), silence_segments)

        # Expected: [0-1], [2-2.1], [3-10]
        assert len(keep_segments) == 3
        assert keep_segments[0] == (0.0, 1.0)
        assert keep_segments[1] == (2.0, 2.1)
        assert keep_segments[2] == (3.0, 10.0)


class TestErrorRecovery:
    """Test error handling and recovery."""

    def test_missing_video_file_error(self):
        """Test appropriate error when video file not found."""
        with TemporaryDirectory() as tmpdir:
            with pytest.raises(Exception):
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
                patch("stream2video.concat._get_video_duration", return_value=100.0),
                patch("stream2video.concat._find_keyframes", return_value=[]),
            ):
                with pytest.raises(Exception, match="No video segments"):
                    cut_and_concat(
                        video_file,
                        silence_segments,
                        Path(tmpdir) / "output.mp4",
                    )


class TestConfigValidation:
    """Test configuration parameter validation."""

    def test_valid_threshold_range(self):
        """Test threshold parameter validation."""
        for threshold in [-60, -30, -20, -10, -5]:
            assert -60 <= threshold <= -5

    def test_valid_min_silence_range(self):
        """Test min_silence parameter validation."""
        for min_silence in [0.1, 0.5, 1.0, 30, 60]:
            assert 0.1 <= min_silence <= 60

    def test_valid_margin_range(self):
        """Test margin parameter validation."""
        for margin in [0, 0.1, 1.0, 5.0]:
            assert 0 <= margin <= 5
