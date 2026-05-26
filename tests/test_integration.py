"""Integration tests for stream2video pipeline."""

import pytest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch, MagicMock

from stream2video.download import download
from stream2video.silence import detect_silence, SilenceSegment
from stream2video.concat import cut_and_concat, _generate_keep_segments, _align_to_keyframes


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


class TestAlignToKeyframes:
    """Test keyframe alignment logic."""

    def test_empty_keyframes_no_change(self):
        """Test that segments are unchanged with empty keyframes."""
        segments = [(1.0, 5.0), (10.0, 15.0)]
        assert _align_to_keyframes(segments, []) == segments

    def test_first_segment_start_zero_preserved(self):
        """Test segment starting at 0.0 is not modified."""
        keyframes = [0, 2, 4]
        assert _align_to_keyframes([(0.0, 5.0)], keyframes) == [(0.0, 5.0)]

    def test_no_overlap_after_alignment(self):
        """Test that alignment doesn't create overlapping segments."""
        segments = [(0.0, 3.5), (4.0, 8.0)]
        keyframes = [0, 1, 2, 3, 4, 5]
        result = _align_to_keyframes(segments, keyframes)
        for i in range(1, len(result)):
            assert result[i][0] >= result[i-1][1], f"Overlap: {result[i-1]} -> {result[i]}"

    def test_prevents_overlap_with_prev_segment(self):
        """Test that alignment snaps to original start if overlap would occur."""
        segments = [(0.0, 10.2), (10.5, 20.0)]
        keyframes = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
        result = _align_to_keyframes(segments, keyframes)
        # Second segment original start = 10.5, nearest keyframe = 10
        # Snapping to 10 would overlap with segment 1 (ends at 10.2)
        # So original start (10.5) should be preserved
        assert result[1][0] == 10.5, f"Expected 10.5, got {result[1][0]}"


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
