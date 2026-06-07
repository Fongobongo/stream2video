"""Tests for silence detection module."""

import os
import shutil
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
    _sample_segments_match,
    _segments_match,
    detect_silence,
    detect_silence_stream,
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
        assert result[0].end == 3.5  # 4.0 - 0.5

    def test_negative_margin_expands(self):
        """Negative margin expands silence (removes more audio)."""
        seg = SilenceSegment(2.0, 4.0)
        result = _apply_margin([seg], -0.5)
        assert len(result) == 1
        assert result[0].start == 1.5  # 2.0 - 0.5
        assert result[0].end == 4.5  # 4.0 + 0.5

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

            with (
                patch("stream2video.silence._probe_duration", return_value=100.0),
                patch("stream2video.silence.subprocess.Popen", side_effect=fake_popen),
                pytest.raises(SilenceCancelledError, match="cancelled"),
            ):
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


class TestSampleSegmentsMatch:
    """_sample_segments_match is the sample-verify gate.

    It compares only START times (and counts), not ENDs, because A-sample's
    ends are clipped by the -t flag (e.g., a real (50, 80) becomes (50, 60)).
    START comparison still catches constant itsoffset broken-PTS.
    """

    def test_identical_segments_match(self):
        seg = [SilenceSegment(1.0, 2.0), SilenceSegment(5.0, 6.0)]
        assert _sample_segments_match(seg, seg[:]) is True

    def test_boundary_clipped_a_segment_matches_full_d_segment(self):
        """The case that broke the original sample-verify: A-sample has
        (50, 60) (clipped at -t boundary) while D has the real (50, 80).
        Same start (50), same count → match, trust D's full end."""
        a = [SilenceSegment(50.0, 60.0)]  # clipped by -t
        b = [SilenceSegment(50.0, 80.0)]  # real end
        assert _sample_segments_match(a, b, tolerance=0.05) is True

    def test_itsoffset_caught_by_start_shift(self):
        """Constant 2s itsoffset must trigger mismatch (shifts all starts)."""
        a = [SilenceSegment(5.0, 8.0), SilenceSegment(30.0, 35.0)]
        b = [SilenceSegment(3.0, 8.0), SilenceSegment(28.0, 35.0)]  # shifted -2
        assert _sample_segments_match(a, b, tolerance=0.05) is False

    def test_count_mismatch(self):
        a = [SilenceSegment(5.0, 10.0)]
        b = [SilenceSegment(5.0, 10.0), SilenceSegment(20.0, 25.0)]
        assert _sample_segments_match(a, b) is False

    def test_empty_lists_match(self):
        assert _sample_segments_match([], []) is True

    def test_different_ends_same_starts_match(self):
        """ENDS can differ wildly (A's are clipped) but same STARTS → match."""
        a = [SilenceSegment(5.0, 60.0), SilenceSegment(20.0, 25.0)]
        b = [SilenceSegment(5.0, 300.0), SilenceSegment(20.0, 25.0)]
        assert _sample_segments_match(a, b, tolerance=0.05) is True

    def test_start_within_tolerance_matches(self):
        """Sub-50ms drift on starts is tolerated (resampling precision)."""
        a = [SilenceSegment(5.000, 10.0), SilenceSegment(30.000, 35.0)]
        b = [SilenceSegment(5.030, 10.0), SilenceSegment(30.010, 35.0)]
        assert _sample_segments_match(a, b, tolerance=0.05) is True


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

    def _fake_popen_factory(self, stderr_outputs: list, extract_wav_to: Path | None = None):
        """Return a fake Popen whose .stderr.readline yields the i-th canned
        output then EOF, .stdout.readline yields 'progress=end' then EOF.

        Each Popen() call advances through `stderr_outputs`. If
        `extract_wav_to` is set, the first Popen call (assumed to be the WAV
        extract) writes a placeholder file at that path so subsequent WAV
        cache validity checks pass.
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
                    # Mimic ffmpeg: when called as the extract step, write the
                    # WAV file so WAV cache validity checks succeed later.
                    if extract_wav_to is not None and idx == 0:
                        extract_wav_to.write_text("fake-wav")

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

            with (
                patch("stream2video.silence._probe_duration", return_value=100.0),
                patch("stream2video.silence.subprocess.Popen", side_effect=factory),
            ):
                segs = detect_silence(
                    video, output_dir=out, threshold=-20, min_silence=0.5, margin=0
                )

            assert len(segs) == 1
            assert segs[0].start == 1.0
            assert segs[0].end == 2.5
            assert wav.exists()  # cache kept

    def test_d_mismatch_a_invalidates_wav_and_uses_a(self):
        """No WAV cache → extract + D + A-sample. On sample mismatch, the WAV
        is invalidated and a full A detection is run. The full A result is
        used."""
        with TemporaryDirectory() as tmp:
            video = Path(tmp) / "video.mp4"
            video.write_text("dummy")
            out = Path(tmp)
            wav = out / "video_audio.wav"
            # No pre-existing WAV → extract step will run.

            # 4 ffmpeg calls: extract, D (returns shifted segment simulating
            # broken PTS), A-sample (canonical), full A (after mismatch).
            stderr_extract = ""
            stderr_D = (
                "[silencedetect @ 0x0] silence_start: 0.000\n"
                "[silencedetect @ 0x0] silence_end: 1.500\n"
            )
            stderr_A_sample = (
                "[silencedetect @ 0x0] silence_start: 2.000\n"
                "[silencedetect @ 0x0] silence_end: 3.500\n"
            )
            stderr_A_full = (
                "[silencedetect @ 0x0] silence_start: 4.000\n"
                "[silencedetect @ 0x0] silence_end: 5.500\n"
            )
            factory = self._fake_popen_factory(
                [stderr_extract, stderr_D, stderr_A_sample, stderr_A_full]
            )

            with (
                patch("stream2video.silence._probe_duration", return_value=100.0),
                patch("stream2video.silence.subprocess.Popen", side_effect=factory),
            ):
                segs = detect_silence(
                    video, output_dir=out, threshold=-20, min_silence=0.5, margin=0
                )

            # Full A's result must be used (canonical, unshifted, all-time)
            assert len(segs) == 1
            assert segs[0].start == 4.0
            assert segs[0].end == 5.5
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

            with (
                patch("stream2video.silence._probe_duration", return_value=100.0),
                patch("stream2video.silence.subprocess.Popen", side_effect=factory),
            ):
                segs = detect_silence(video, threshold=-20, min_silence=0.5, margin=0)

            assert len(segs) == 1
            assert segs[0].start == 1.0
            assert segs[0].end == 2.0

    def test_sample_verify_pass_keeps_d_and_wav(self):
        """On cache miss with matching D-sample and A-sample, D's full result
        is used and the WAV cache is kept (no full A run needed)."""
        with TemporaryDirectory() as tmp:
            video = Path(tmp) / "video.mp4"
            video.write_text("dummy")
            out = Path(tmp)
            wav = out / "video_audio.wav"

            # 3 ffmpeg calls: extract, D (full), A-sample. If sample matches
            # D, no full A is run.
            stderr_extract = ""
            # D returns 2 segments within the first 60s + 1 outside
            stderr_D = (
                "[silencedetect @ 0x0] silence_start: 5.000\n"
                "[silencedetect @ 0x0] silence_end: 10.000\n"
                "[silencedetect @ 0x0] silence_start: 30.000\n"
                "[silencedetect @ 0x0] silence_end: 35.000\n"
                "[silencedetect @ 0x0] silence_start: 80.000\n"
                "[silencedetect @ 0x0] silence_end: 85.000\n"
            )
            # A-sample (first 60s) sees the same 2 segments as D-sample
            stderr_A_sample = (
                "[silencedetect @ 0x0] silence_start: 5.000\n"
                "[silencedetect @ 0x0] silence_end: 10.000\n"
                "[silencedetect @ 0x0] silence_start: 30.000\n"
                "[silencedetect @ 0x0] silence_end: 35.000\n"
            )
            factory = self._fake_popen_factory(
                [stderr_extract, stderr_D, stderr_A_sample],
                extract_wav_to=wav,
            )

            with (
                patch("stream2video.silence._probe_duration", return_value=100.0),
                patch("stream2video.silence.subprocess.Popen", side_effect=factory),
            ):
                segs = detect_silence(
                    video, output_dir=out, threshold=-20, min_silence=0.5, margin=0
                )

            # D's full result (3 segments) is used; 2 of those are within
            # the sample window and matched A-sample.
            assert len(segs) == 3
            assert segs[0].start == 5.0
            assert segs[0].end == 10.0
            assert segs[1].start == 30.0
            assert segs[1].end == 35.0
            assert segs[2].start == 80.0
            assert segs[2].end == 85.0
            # WAV cache must be kept on sample-verify pass
            assert wav.exists()


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
            ffmpeg,
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-f",
            "lavfi",
            "-i",
            ("sine=frequency=1000:sample_rate=48000:duration=2"),
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x240:r=10",
            "-filter_complex",
            "[1:a]apad[w];[0:a][w]concat=n=2:v=0:a=1[a];[2:v]trim=duration=6[v]",
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-t",
            "6",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(out_path),
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
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
                video,
                threshold=-30,
                min_silence=0.5,
                margin=0,
            )

            # D path — with output_dir, runs extract + D + A verification
            segs_D = detect_silence(
                video,
                threshold=-30,
                min_silence=0.5,
                margin=0,
                output_dir=out,
            )

            # Same number of segments and matching timestamps (within tolerance)
            assert len(segs_A) == len(segs_D), f"A={len(segs_A)} segments, D={len(segs_D)} segments"
            for a, d in zip(segs_A, segs_D, strict=True):
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
                video,
                threshold=-30,
                min_silence=0.5,
                margin=0,
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
                video,
                threshold=-30,
                min_silence=1.0,
                margin=0,
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

    def test_long_silence_crossing_sample_boundary_passes_sample_verify(self, has_ffmpeg):
        """Regression: a long silence that crosses the 60s sample window must
        not cause a false-positive sample-verify mismatch (previously the
        test would fail because A-sample's end (60) differed from D's real
        end by tens of seconds). The fix compares START times only, so
        the long silence is correctly identified as healthy."""
        if not has_ffmpeg:
            pytest.skip("ffmpeg/ffmpeg not available")

        with TemporaryDirectory() as tmp:
            video = Path(tmp) / "long_silence.mp4"
            out = Path(tmp)

            # 5min video, all silence — produces ONE segment (0, 300)
            # that crosses the 60s sample-verify boundary.
            cmd = [
                shutil_which("ffmpeg"),
                "-y",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=stereo:duration=300",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=320x240:r=10",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-t",
                "300",
                str(video),
            ]
            subprocess.run(
                cmd,
                check=True,
                timeout=60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

            # 1st run: should pass sample-verify, keep WAV
            segs = detect_silence(
                video,
                threshold=-30,
                min_silence=0.5,
                margin=0,
                output_dir=out,
            )
            wav = out / f"{video.stem}_audio.wav"
            assert wav.exists(), "WAV must be kept on sample-verify pass"

            # 2nd run with different threshold: should hit cache, no verify
            wav_mtime_before = wav.stat().st_mtime
            segs2 = detect_silence(
                video,
                threshold=-40,
                min_silence=0.5,
                margin=0,
                output_dir=out,
            )
            assert wav.stat().st_mtime == wav_mtime_before, (
                "WAV must not be re-extracted on cache hit"
            )

            # Both runs should find the single all-video silence
            assert len(segs) == 1
            assert len(segs2) == 1


def shutil_which(name: str) -> str:
    import shutil

    path = shutil.which(name)
    if not path:
        pytest.skip(f"{name} not available")
    return path


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
            line = self._payload[self._pos :]
            self._pos = len(self._payload)
            return line
        line = self._payload[self._pos : nl + 1]
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


# ── detect_silence_stream (ffmpeg pipe, no WAV) ───────────────


def test_detect_silence_stream_silent_wav(tmp_path):
    """All-zero WAV is one continuous silence (single segment from 0 to end)."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    import wave

    wav = tmp_path / "silent.wav"
    n = 16000 * 2  # 2 seconds at 16kHz
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * n)

    segments = detect_silence_stream(wav, threshold=-30.0, min_silence=0.5)
    assert len(segments) >= 1
    assert segments[0].start < 0.1
    assert segments[0].end > 1.5


def test_detect_silence_stream_sine_wav_no_segments(tmp_path):
    """Loud sine wave — no silence detected."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    import math
    import struct
    import wave

    wav = tmp_path / "sine.wav"
    n = 16000  # 1 second at 16kHz
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        frames = bytearray()
        for i in range(n):
            s = int(20000 * math.sin(2 * math.pi * 440 * i / 16000))
            frames += struct.pack("<h", max(-32768, min(32767, s)))
        w.writeframes(bytes(frames))

    segments = detect_silence_stream(wav, threshold=-30.0, min_silence=0.5)
    assert segments == []


def test_detect_silence_stream_progressive_callback(tmp_path):
    """on_segment fires with a running list of segments as silence_end lines arrive."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    import wave

    wav = tmp_path / "two_silences.wav"
    # 0.5s loud tone, 1s silence, 0.5s loud tone, 1s silence (16kHz mono s16le).
    sr = 16000
    n_tone = int(0.5 * sr)
    n_silence = int(1.0 * sr)
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        # 0x7fff = max amplitude (0 dB) — well above the -30 dB threshold.
        w.writeframes(b"\xff\x7f" * n_tone + b"\x00\x00" * n_silence)
        w.writeframes(b"\xff\x7f" * n_tone + b"\x00\x00" * n_silence)

    seen: list[list] = []
    segments = detect_silence_stream(
        wav,
        threshold=-30.0,
        min_silence=0.3,
        on_segment=lambda s: seen.append(len(s)),
    )
    # Final list has 2 silences.
    assert len(segments) == 2
    # Callback was invoked at least twice with growing list sizes.
    assert seen, "on_segment was never called"
    assert seen[-1] == 2
    # The first callback must have been a prefix of the final list.
    assert seen[0] >= 1
    # Sizes are non-decreasing.
    assert seen == sorted(seen)


# ── detect_silence(on_segment=...) — progressive callback ─


class TestDetectSilenceOnSegment:
    """`detect_silence(..., on_segment=...)` invokes the callback with a
    growing list of raw (pre-margin) segments as `silence_end` lines
    arrive on ffmpeg's stderr. The callback runs on the stderr drain
    thread; the GUI is responsible for any main-thread dispatch.

    No file is written — the in-process GUI stores the snapshot in
    memory and the popup polls it. The final cache (if any) is still
    written by the caller via `save_silence_cache`.
    """

    def _fake_popen_factory(self, stderr_payload: str):
        """Single-call Popen factory — A-path (no output_dir) makes one
        ffmpeg call, so we only need to mock one."""

        def fake_popen(cmd, **kwargs):
            class _FakeProcess:
                def __init__(self):
                    self.stderr = _FakeStderr(stderr_payload.encode("utf-8"))
                    self.stdout = _FakeStdout()
                    self.returncode = 0

                def poll(self):
                    return self.returncode

                def wait(self, timeout=None):
                    return self.returncode

                def kill(self):
                    self.returncode = -9

            return _FakeProcess()

        return fake_popen

    def test_on_segment_fires_with_growing_snapshot(self):
        """The callback must be invoked with a snapshot list whose length
        grows monotonically as new segments are detected. The final
        snapshot must match the returned list of segments."""
        with TemporaryDirectory() as tmp:
            video = Path(tmp) / "video.mp4"
            video.write_text("dummy")

            stderr_payload = (
                "[silencedetect @ 0x0] silence_start: 1.000\n"
                "[silencedetect @ 0x0] silence_end: 2.000\n"
                "[silencedetect @ 0x0] silence_start: 10.000\n"
                "[silencedetect @ 0x0] silence_end: 12.000\n"
                "[silencedetect @ 0x0] silence_start: 20.000\n"
                "[silencedetect @ 0x0] silence_end: 25.000\n"
            )
            factory = self._fake_popen_factory(stderr_payload)

            seen_sizes: list[int] = []
            last_snapshot: list[SilenceSegment] = []

            def on_segment(seg_list: list[SilenceSegment]) -> None:
                seen_sizes.append(len(seg_list))
                # Stash the latest snapshot — the GUI's polling code does
                # effectively the same to read the most recent state.
                last_snapshot.clear()
                last_snapshot.extend(seg_list)

            with (
                patch("stream2video.silence._probe_duration", return_value=100.0),
                patch("stream2video.silence.subprocess.Popen", side_effect=factory),
            ):
                segs = detect_silence(
                    video,
                    threshold=-20,
                    min_silence=0.5,
                    margin=0,
                    on_segment=on_segment,
                )

            # Callback fired exactly once per new segment.
            assert seen_sizes == [1, 2, 3]
            # Final snapshot matches the returned list (raw, pre-margin).
            assert [(s.start, s.end) for s in last_snapshot] == [(s.start, s.end) for s in segs]
            # The returned list is also margin'd (margin=0 here, so no change).
            assert [(s.start, s.end) for s in segs] == [
                (1.0, 2.0),
                (10.0, 12.0),
                (20.0, 25.0),
            ]

    def test_on_segment_not_fired_when_none(self):
        """Backward compat: when `on_segment` is None, detection still
        works (batch path), and no callback is registered."""
        with TemporaryDirectory() as tmp:
            video = Path(tmp) / "video.mp4"
            video.write_text("dummy")

            stderr_payload = (
                "[silencedetect @ 0x0] silence_start: 5.000\n"
                "[silencedetect @ 0x0] silence_end: 6.000\n"
            )
            factory = self._fake_popen_factory(stderr_payload)

            fired: list[bool] = []

            def on_segment(seg_list: list[SilenceSegment]) -> None:
                fired.append(True)

            with (
                patch("stream2video.silence._probe_duration", return_value=100.0),
                patch("stream2video.silence.subprocess.Popen", side_effect=factory),
            ):
                segs = detect_silence(video, threshold=-20, min_silence=0.5, margin=0)

            assert len(segs) == 1
            assert segs[0].start == 5.0
            # No callback fired because none was passed.
            assert fired == []

    def test_on_segment_receives_pre_margin_segments(self):
        """The callback sees raw (pre-margin) segments — margin is applied
        ONLY in the final return value, not in the snapshots the
        callback observes. The GUI applies margin at render time
        so the same logic works for both live and post-detect rendering."""
        with TemporaryDirectory() as tmp:
            video = Path(tmp) / "video.mp4"
            video.write_text("dummy")

            stderr_payload = (
                "[silencedetect @ 0x0] silence_start: 1.000\n"
                "[silencedetect @ 0x0] silence_end: 4.000\n"
            )
            factory = self._fake_popen_factory(stderr_payload)

            last_snapshot: list[SilenceSegment] = []

            def on_segment(seg_list: list[SilenceSegment]) -> None:
                last_snapshot.clear()
                last_snapshot.extend(seg_list)

            with (
                patch("stream2video.silence._probe_duration", return_value=100.0),
                patch("stream2video.silence.subprocess.Popen", side_effect=factory),
            ):
                segs = detect_silence(
                    video,
                    threshold=-20,
                    min_silence=0.5,
                    margin=0.5,  # shrink by 0.5s on each side
                    on_segment=on_segment,
                )

            # Return value is margin'd (1.5, 3.5).
            assert len(segs) == 1
            assert segs[0].start == 1.5
            assert segs[0].end == 3.5
            # Callback saw the raw (1.0, 4.0) — margin is the caller's job.
            assert len(last_snapshot) == 1
            assert last_snapshot[0].start == 1.0
            assert last_snapshot[0].end == 4.0
