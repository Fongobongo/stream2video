"""Tests for silence detection module."""

import os
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
    _get_wav_cache_path,
    _is_wav_cache_valid,
    _segments_match,
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


class TestSegmentsMatch:
    """_segments_match is the verification gate for the D→A fallback."""

    def test_identical_segments_match(self):
        seg = [SilenceSegment(1.0, 2.0), SilenceSegment(5.0, 6.0)]
        assert _segments_match(seg, seg[:]) is True

    def test_reordered_segments_match(self):
        a = [SilenceSegment(1.0, 2.0), SilenceSegment(5.0, 6.0)]
        b = [SilenceSegment(5.0, 6.0), SilenceSegment(1.0, 2.0)]
        assert _segments_match(a, b) is True

    def test_within_tolerance_matches(self):
        """Sub-100ms timestamp drift is tolerated (resampling precision)."""
        a = [SilenceSegment(1.000, 2.000), SilenceSegment(5.000, 6.000)]
        b = [SilenceSegment(1.030, 2.020), SilenceSegment(5.010, 6.040)]
        assert _segments_match(a, b, tolerance=0.05) is True

    def test_outside_tolerance_mismatches(self):
        a = [SilenceSegment(1.000, 2.000), SilenceSegment(5.000, 6.000)]
        b = [SilenceSegment(1.500, 2.500), SilenceSegment(5.500, 6.500)]
        assert _segments_match(a, b, tolerance=0.05) is False

    def test_different_count_mismatches(self):
        a = [SilenceSegment(1.0, 2.0), SilenceSegment(5.0, 6.0)]
        b = [SilenceSegment(1.0, 2.0)]
        assert _segments_match(a, b) is False

    def test_extra_segment_mismatches(self):
        a = [SilenceSegment(1.0, 2.0)]
        b = [SilenceSegment(1.0, 2.0), SilenceSegment(5.0, 6.0)]
        assert _segments_match(a, b) is False

    def test_empty_lists_match(self):
        assert _segments_match([], []) is True

    def test_broken_pts_shift_mismatches(self):
        """A 2-second itsoffset (the documented failure mode of -copyts-free
        extraction) must register as a mismatch so the fallback path is taken."""
        a = [SilenceSegment(10.0, 12.0), SilenceSegment(20.0, 22.0)]
        b = [SilenceSegment(8.0, 10.0), SilenceSegment(18.0, 20.0)]  # shifted by -2.0
        assert _segments_match(a, b, tolerance=0.05) is False


class TestWavCachePath:
    """_get_wav_cache_path naming convention."""

    def test_path_uses_video_stem(self):
        video = Path("/some/dir/myvideo.mp4")
        out = Path("/output")
        assert _get_wav_cache_path(video, out) == Path("/output/myvideo_audio.wav")


class TestWavCacheValidity:
    """WAV cache is keyed by source mtime — older WAV is invalidated."""

    def test_missing_wav_is_invalid(self):
        with TemporaryDirectory() as tmp:
            video = Path(tmp) / "video.mp4"
            video.write_text("dummy")
            wav = Path(tmp) / "video_audio.wav"
            assert not wav.exists()
            assert _is_wav_cache_valid(wav, video) is False

    def test_newer_wav_is_valid(self):
        with TemporaryDirectory() as tmp:
            video = Path(tmp) / "video.mp4"
            wav = Path(tmp) / "video_audio.wav"
            video.write_text("dummy")
            wav.write_text("dummy")
            os.utime(video, (1000, 1000))
            os.utime(wav, (2000, 2000))
            assert _is_wav_cache_valid(wav, video) is True

    def test_older_wav_is_invalid(self):
        with TemporaryDirectory() as tmp:
            video = Path(tmp) / "video.mp4"
            wav = Path(tmp) / "video_audio.wav"
            video.write_text("dummy")
            wav.write_text("dummy")
            os.utime(video, (2000, 2000))
            os.utime(wav, (1000, 1000))
            assert _is_wav_cache_valid(wav, video) is False

    def test_equal_mtime_is_valid(self):
        """Equal mtime counts as fresh (avoids re-extract on same-second touches)."""
        with TemporaryDirectory() as tmp:
            video = Path(tmp) / "video.mp4"
            wav = Path(tmp) / "video_audio.wav"
            video.write_text("dummy")
            wav.write_text("dummy")
            os.utime(video, (1000, 1000))
            os.utime(wav, (1000, 1000))
            assert _is_wav_cache_valid(wav, video) is True


class TestWavCacheFallback:
    """End-to-end of the D + A verification path with mocked ffmpeg.

    We can't easily mock _run_silencedetect without changing call signatures,
    so we patch subprocess.Popen and feed canned ffmpeg output for each call.
    The mock ffmpeg writes silence_start/silence_end lines to stderr that we
    control per-call, so we can simulate D-match-A and D-mismatch-A scenarios.
    """

    def _fake_popen_factory(self, stderr_outputs: list):
        """Return a fake Popen whose .stderr.readline yields the i-th canned
        output then EOF, .stdout.readline yields 'progress=end' then EOF.

        Each Popen() call advances through `stderr_outputs`.
        """
        call_index = {"i": 0}

        def fake_popen(cmd, **kwargs):
            idx = call_index["i"]
            call_index["i"] += 1
            if idx >= len(stderr_outputs):
                stderr_lines = b""
            else:
                stderr_lines = stderr_outputs[idx].encode("utf-8")

            class _FakeProcess:
                def __init__(self):
                    self.stderr = _FakeStderr(stderr_lines)
                    self.stdout = _FakeStdout()
                    self.returncode = 0
                    self._killed = False

                def poll(self):
                    return self.returncode

                def wait(self, timeout=None):
                    return self.returncode

                def kill(self):
                    self._killed = True
                    self.returncode = -9

            return _FakeProcess()

        return fake_popen

    def test_d_matches_a_keeps_wav_cache(self):
        """When the WAV cache is valid (newer mtime than source), only D runs
        and no verification happens. The WAV is kept."""
        with TemporaryDirectory() as tmp:
            video = Path(tmp) / "video.mp4"
            video.write_text("dummy")
            out = Path(tmp)
            wav = out / "video_audio.wav"
            wav.write_text("placeholder")  # pre-existing → D path is taken
            os.utime(video, (1000, 1000))
            os.utime(wav, (2000, 2000))

            # WAV is already valid (mtime newer), so only 1 ffmpeg call happens.
            stderr_D = (
                "[silencedetect @ 0x0] silence_start: 1.000\n"
                "[silencedetect @ 0x0] silence_end: 2.500\n"
            )
            factory = self._fake_popen_factory([stderr_D])

            with patch("stream2video.silence._probe_duration", return_value=100.0), \
                 patch("stream2video.silence.subprocess.Popen", side_effect=factory):
                segs = detect_silence(video, output_dir=out, threshold=-20, min_silence=0.5, margin=0)

            assert len(segs) == 1
            assert segs[0].start == 1.0
            assert segs[0].end == 2.5
            assert wav.exists()  # cache kept

    def test_d_mismatch_a_invalidates_wav_and_uses_a(self):
        """No WAV cache → full D + A verification. On mismatch, A is used and
        the freshly-extracted WAV is deleted."""
        with TemporaryDirectory() as tmp:
            video = Path(tmp) / "video.mp4"
            video.write_text("dummy")
            out = Path(tmp)
            wav = out / "video_audio.wav"
            # No pre-existing WAV → extract step will run, then D, then A.

            # 3 ffmpeg calls: extract (writes WAV — fake returns success),
            # D (returns one shifted segment simulating broken PTS),
            # A (returns the canonical unshifted segment).
            stderr_extract = ""
            stderr_D = (
                "[silencedetect @ 0x0] silence_start: 0.000\n"
                "[silencedetect @ 0x0] silence_end: 1.500\n"
            )
            stderr_A = (
                "[silencedetect @ 0x0] silence_start: 2.000\n"
                "[silencedetect @ 0x0] silence_end: 3.500\n"
            )
            factory = self._fake_popen_factory([stderr_extract, stderr_D, stderr_A])

            with patch("stream2video.silence._probe_duration", return_value=100.0), \
                 patch("stream2video.silence.subprocess.Popen", side_effect=factory):
                segs = detect_silence(video, output_dir=out, threshold=-20, min_silence=0.5, margin=0)

            # A's result must be used (canonical, unshifted)
            assert len(segs) == 1
            assert segs[0].start == 2.0
            assert segs[0].end == 3.5
            # WAV cache must be invalidated on mismatch
            assert not wav.exists()

    def test_no_output_dir_skips_wav_caching(self):
        """With output_dir=None, behavior is identical to the A-only baseline
        and no WAV file is created."""
        with TemporaryDirectory() as tmp:
            video = Path(tmp) / "video.mp4"
            video.write_text("dummy")

            stderr_A = (
                "[silencedetect @ 0x0] silence_start: 1.000\n"
                "[silencedetect @ 0x0] silence_end: 2.000\n"
            )
            factory = self._fake_popen_factory([stderr_A])

            with patch("stream2video.silence._probe_duration", return_value=100.0), \
                 patch("stream2video.silence.subprocess.Popen", side_effect=factory):
                segs = detect_silence(video, threshold=-20, min_silence=0.5, margin=0)

            assert len(segs) == 1
            assert segs[0].start == 1.0
            assert segs[0].end == 2.0


class TestEndToEndRealFfmpeg:
    """Real-ffmpeg end-to-end test for the D + A verification pipeline.

    Skipped if ffmpeg/ffprobe are not available. Generates a synthetic test
    video with known silences via ffmpeg's lavfi input, then runs detect_silence
    through both paths and asserts they produce matching segments.
    """

    @pytest.fixture
    def has_ffmpeg(self):
        import shutil
        return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None

    def _make_test_video(self, out_path: Path) -> None:
        """Build a 6-second test video: 2s silence + 2s tone + 2s silence."""
        import shutil
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            pytest.skip("ffmpeg not available")
        cmd = [
            ffmpeg, "-y", "-v", "error",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
            "-f", "lavfi", "-i", (
                "sine=frequency=1000:sample_rate=48000:duration=2"
            ),
            "-f", "lavfi", "-i", "color=c=black:s=320x240:r=10",
            "-filter_complex",
            "[1:a]apad[w];[0:a][w]concat=n=2:v=0:a=1[a];[2:v]trim=duration=6[v]",
            "-map", "[v]", "-map", "[a]",
            "-t", "6",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            str(out_path),
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            pytest.skip(f"ffmpeg lavfi test setup failed: {result.stderr[:200]}")

    def test_d_path_matches_a_path_on_real_video(self, has_ffmpeg):
        """Run detect_silence on a real test video via both A (no output_dir)
        and D (output_dir) paths; assert identical results, and that the WAV
        cache is created."""
        if not has_ffmpeg:
            pytest.skip("ffmpeg/ffprobe not available")

        with TemporaryDirectory() as tmp:
            video = Path(tmp) / "test.mp4"
            out = Path(tmp)
            self._make_test_video(video)

            # A path — no output_dir, direct detection on video
            segs_A = detect_silence(
                video, threshold=-30, min_silence=0.5, margin=0,
            )

            # D path — with output_dir, runs extract + D + A verification
            segs_D = detect_silence(
                video, threshold=-30, min_silence=0.5, margin=0,
                output_dir=out,
            )

            # Same number of segments and matching timestamps (within tolerance)
            assert len(segs_A) == len(segs_D), (
                f"A={len(segs_A)} segments, D={len(segs_D)} segments"
            )
            for a, d in zip(segs_A, segs_D):
                assert abs(a.start - d.start) < 0.1, f"start mismatch: {a.start} vs {d.start}"
                assert abs(a.end - d.end) < 0.1, f"end mismatch: {a.end} vs {d.end}"

            # WAV cache must exist after D path
            wav = out / f"{video.stem}_audio.wav"
            assert wav.exists(), "WAV cache should be created on first D run"

    def test_wav_cache_reused_on_second_run(self, has_ffmpeg):
        """After a successful D+A verification, a second call must reuse the
        WAV cache (no second extract step), giving the same result."""
        if not has_ffmpeg:
            pytest.skip("ffmpeg/ffprobe not available")

        with TemporaryDirectory() as tmp:
            video = Path(tmp) / "test.mp4"
            out = Path(tmp)
            self._make_test_video(video)

            # First call: creates WAV cache
            segs_first = detect_silence(
                video, threshold=-30, min_silence=0.5, margin=0,
                output_dir=out,
            )

            # Touch WAV mtime to a known time, then re-run with new params
            # (different params force a re-detect, but WAV is still valid).
            wav = out / f"{video.stem}_audio.wav"
            assert wav.exists()
            first_wav_mtime = wav.stat().st_mtime

            # Second call with different min_silence — forces re-detect but
            # WAV should be reused (mtime is still >= video mtime)
            segs_second = detect_silence(
                video, threshold=-30, min_silence=1.0, margin=0,
                output_dir=out,
            )

            # WAV must not have been re-extracted
            assert wav.stat().st_mtime == first_wav_mtime, (
                "WAV cache should not be re-extracted when still valid"
            )

            # Different min_silence can give different results — just verify
            # both are non-empty lists of segments
            assert len(segs_first) > 0
            assert len(segs_second) > 0


class _FakeStderr:
    """Minimal pipe-like object for drain_stderr_lines to read from."""

    def __init__(self, payload: bytes):
        self._payload = payload
        self._pos = 0
        self._closed = False

    def readline(self):
        if self._closed or self._pos >= len(self._payload):
            return b""
        nl = self._payload.find(b"\n", self._pos)
        if nl < 0:
            line = self._payload[self._pos:]
            self._pos = len(self._payload)
            return line
        line = self._payload[self._pos:nl + 1]
        self._pos = nl + 1
        return line

    def close(self):
        self._closed = True


class _FakeStdout:
    """Minimal stdout pipe: yields one 'progress=end' marker line then EOF."""

    def __init__(self):
        self._sent = False
        self._closed = False

    def readline(self):
        if self._closed or self._sent:
            return b""
        self._sent = True
        return b"progress=end\n"

    def close(self):
        self._closed = True
