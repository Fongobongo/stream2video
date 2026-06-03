"""Tests for silence detection module."""

import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from stream2video.silence import (
    SilenceCancelledError,
    SilenceDetectionError,
    SilenceSegment,
    _apply_margin,
    detect_silence,
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

    def test_inverted_segment_has_zero_duration(self):
        seg = SilenceSegment(5.0, 3.0)
        assert seg.duration == 0.0


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

    def test_positive_margin_shrinks(self):
        """Positive margin shrinks silence (keeps more audio)."""
        seg = SilenceSegment(2.0, 4.0)
        result = _apply_margin([seg], 0.5)
        assert len(result) == 1
        assert result[0].start == 2.5  # 2.0 + 0.5
        assert result[0].end == 3.5    # 4.0 - 0.5

    def test_negative_margin_expands(self):
        """Negative margin expands silence (removes more audio)."""
        seg = SilenceSegment(2.0, 4.0)
        result = _apply_margin([seg], -0.5)
        assert len(result) == 1
        assert result[0].start == 1.5  # 2.0 - 0.5
        assert result[0].end == 4.5    # 4.0 + 0.5

    def test_margin_clamps_to_zero(self):
        """Shrinking must not produce negative start time."""
        seg = SilenceSegment(0.3, 1.0)
        result = _apply_margin([seg], 0.5)
        # After shrink: start=0.8, end=0.5 → start>end → filtered out
        assert len(result) == 0

    def test_overlapping_segments_merge_after_expand(self):
        """Negative margin expands segments that then overlap and merge."""
        seg1 = SilenceSegment(1.0, 2.5)
        seg2 = SilenceSegment(2.0, 3.5)
        result = _apply_margin([seg1, seg2], -0.3)
        # seg1: 0.7-2.8, seg2: 1.7-3.8 → overlap → merge to 0.7-3.8
        assert len(result) == 1
        assert result[0].start == 0.7
        assert result[0].end == 3.8

    def test_non_overlapping_segments_preserved(self):
        seg1 = SilenceSegment(1.0, 2.0)
        seg2 = SilenceSegment(4.0, 5.0)
        result = _apply_margin([seg1, seg2], 0.2)
        assert len(result) == 2
        assert result[0].start == 1.2
        assert result[0].end == 1.8
        assert result[1].start == 4.2
        assert result[1].end == 4.8


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


class TestSilenceCancellation:
    """Cancellation must raise SilenceCancelledError, not a real failure."""

    def test_cancel_callback_aborts(self):
        """cancel_callback=True must kill the subprocess and raise SilenceCancelledError."""
        _real_popen = subprocess.Popen

        def fake_popen(cmd, **kwargs):
            proc = _real_popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            time.sleep(0.3)
            return proc

        with TemporaryDirectory() as tmpdir:
            video_file = Path(tmpdir) / "video.mp4"
            video_file.write_text("dummy")

            with patch("stream2video.silence._probe_duration", return_value=100.0), \
                 patch("stream2video.silence.subprocess.Popen", side_effect=fake_popen):
                with pytest.raises(SilenceCancelledError, match="cancelled"):
                    detect_silence(
                        video_file,
                        cancel_callback=lambda: True,
                    )

    def test_cancelled_is_subclass_of_silence_error(self):
        """SilenceCancelledError must remain a SilenceDetectionError for backwards-compat catches."""
        assert issubclass(SilenceCancelledError, SilenceDetectionError)
