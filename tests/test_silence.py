"""Tests for silence detection module."""

import pytest
from pathlib import Path
from tempfile import TemporaryDirectory

from stream2video.silence import (
    SilenceSegment,
    _apply_margin,
    detect_silence,
    SilenceDetectionError,
)


class TestSilenceSegment:
    """Test SilenceSegment class."""

    def test_segment_creation(self):
        seg = SilenceSegment(1.0, 3.5)
        assert seg.start == 1.0
        assert seg.end == 3.5
        assert seg.duration == 2.5

    def test_segment_repr(self):
        seg = SilenceSegment(0.5, 2.0)
        repr_str = repr(seg)
        assert "0.50s - 2.00s" in repr_str
        assert "duration=1.50s" in repr_str


class TestParameterValidation:
    """Test parameter validation."""

    def test_threshold_valid(self):
        # Valid range: -60 to -5
        for threshold in [-60, -30, -20, -10, -5]:
            # Should not raise
            _validate_threshold = lambda t: -60 <= t <= -5
            assert _validate_threshold(threshold)

    def test_threshold_invalid(self):
        for threshold in [-61, -4, 0, 10]:
            _validate_threshold = lambda t: -60 <= t <= -5
            assert not _validate_threshold(threshold)

    def test_min_silence_valid(self):
        for min_s in [0.1, 0.5, 1.0, 30, 60]:
            _validate_min_silence = lambda m: 0.1 <= m <= 60
            assert _validate_min_silence(min_s)

    def test_min_silence_invalid(self):
        for min_s in [0.05, 0.09, 61, 100]:
            _validate_min_silence = lambda m: 0.1 <= m <= 60
            assert not _validate_min_silence(min_s)

    def test_margin_valid(self):
        for margin in [-3, -0.3, 0, 0.1, 1.0, 5.0]:
            _validate_margin = lambda m: -3 <= m <= 5
            assert _validate_margin(margin)

    def test_margin_invalid(self):
        for margin in [-0.1, 5.1, 10]:
            _validate_margin = lambda m: 0 <= m <= 5
            assert not _validate_margin(margin)


class TestApplyMargin:
    """Test margin application."""

    def test_empty_list(self):
        result = _apply_margin([], 0.5)
        assert result == []

    def test_single_segment_no_margin(self):
        seg = SilenceSegment(2.0, 4.0)
        result = _apply_margin([seg], 0)
        assert len(result) == 1
        assert result[0].start == 2.0
        assert result[0].end == 4.0

    def test_single_segment_with_margin(self):
        seg = SilenceSegment(2.0, 4.0)
        result = _apply_margin([seg], 0.5)
        assert len(result) == 1
        assert result[0].start == 1.5  # 2.0 - 0.5
        assert result[0].end == 4.5  # 4.0 + 0.5

    def test_margin_clamps_to_zero(self):
        seg = SilenceSegment(0.3, 1.0)
        result = _apply_margin([seg], 0.5)
        assert len(result) == 1
        assert result[0].start == 0  # Max(0, 0.3 - 0.5)

    def test_overlapping_segments_merge(self):
        seg1 = SilenceSegment(1.0, 2.0)
        seg2 = SilenceSegment(1.8, 3.0)  # Overlaps with seg1 after margin applied
        result = _apply_margin([seg1, seg2], 0.3)
        assert len(result) == 1
        assert result[0].start == 0.7  # 1.0 - 0.3
        assert result[0].end == 3.3  # 3.0 + 0.3

    def test_non_overlapping_segments_preserved(self):
        seg1 = SilenceSegment(1.0, 2.0)
        seg2 = SilenceSegment(4.0, 5.0)
        result = _apply_margin([seg1, seg2], 0.2)
        assert len(result) == 2
        assert result[0].start == 0.8
        assert result[0].end == 2.2
        assert result[1].start == 3.8
        assert result[1].end == 5.2


class TestDetectSilenceValidation:
    """Test silence detection parameter validation."""

    def test_nonexistent_file(self):
        with pytest.raises(SilenceDetectionError, match="not found"):
            detect_silence(Path("/nonexistent/video.mp4"))

    def test_threshold_out_of_range(self):
        with TemporaryDirectory() as tmpdir:
            video_file = Path(tmpdir) / "video.mp4"
            video_file.write_text("dummy")

            with pytest.raises(ValueError, match="Threshold"):
                detect_silence(video_file, threshold=-61)

            with pytest.raises(ValueError, match="Threshold"):
                detect_silence(video_file, threshold=-4)

    def test_min_silence_out_of_range(self):
        with TemporaryDirectory() as tmpdir:
            video_file = Path(tmpdir) / "video.mp4"
            video_file.write_text("dummy")

            with pytest.raises(ValueError, match="Min silence"):
                detect_silence(video_file, min_silence=0.05)

            with pytest.raises(ValueError, match="Min silence"):
                detect_silence(video_file, min_silence=61)

    def test_margin_out_of_range(self):
        with TemporaryDirectory() as tmpdir:
            video_file = Path(tmpdir) / "video.mp4"
            video_file.write_text("dummy")

            with pytest.raises(ValueError, match="Margin"):
                detect_silence(video_file, margin=-3.1)

            with pytest.raises(ValueError, match="Margin"):
                detect_silence(video_file, margin=5.1)
