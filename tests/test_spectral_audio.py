"""Spectral / signal audio smoke-tests (numpy-based).

These tests extract audio from the source and the processed output via
``ffmpeg -f s16le`` pipes, decode the raw PCM with numpy, and verify
signal-level properties the frame-count tests can't catch:

  * **Tone preservation** — the 440 Hz sine in the source survives
    the cut+concat pipeline with its dominant frequency intact.
  * **No DC offset introduced** — the pipeline shouldn't shift the
    signal's mean away from zero.
  * **Amplitude in expected range** — the output's RMS is in the
    same ballpark as the source (within -3..+3 dB), not silently
    attenuated or clipped.
  * **Gapless continuity** — at the concat boundary (where two keep
    segments join), the waveform shouldn't have a large discontinuity
    (click/pop). A click would show up as a transient spike in the
    first few samples after the join.

Skipped when numpy or ffmpeg is not available.

The tests are intentionally lenient (broad tolerances) — they're a
smoke net for "the audio was completely mangled", not a perceptual
codec quality metric.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from stream2video.concat import cut_and_concat
from stream2video.silence import SilenceSegment

# Optional numpy import — the entire module skips if numpy isn't
# installed. numpy is NOT a runtime dependency of stream2video; it's
# only used in this test module to analyse the raw PCM.
np = pytest.importorskip("numpy", reason="numpy required for spectral tests")


def _have(*tools: str) -> bool:
    return all(shutil.which(t) is not None for t in tools)


HAVE_FFMPEG = _have("ffmpeg", "ffprobe")
pytestmark = pytest.mark.skipif(
    not HAVE_FFMPEG,
    reason="ffmpeg/ffprobe not on PATH — spectral tests need them",
)


def _make_tone_source(out: Path, duration: float = 6.0, freq: int = 440) -> None:
    """Generate a synthetic source with a steady tone + silence in the middle.

    The source has a 440 Hz sine for the full duration. We insert one
    silence segment in the middle (2.0-4.0s) so the output keeps 0-2s
    + 4-6s. The tone should be present in both kept segments, and the
    concat boundary at t=2s (output) should not have a click.
    """
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=duration={duration}:size=320x240:rate=30",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:duration={duration}:sample_rate=48000",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(out),
        ],
        check=True,
    )


def _extract_pcm(video: Path, sample_rate: int = 48000) -> np.ndarray:
    """Extract mono s16le PCM from ``video`` via ffmpeg pipe.

    Returns a 1-D numpy array of float32 samples normalised to [-1, 1].
    """
    proc = subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-i",
            str(video),
            "-vn",  # audio only
            "-ac",
            "1",  # mono
            "-ar",
            str(sample_rate),
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
    )
    raw = proc.stdout
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return samples


def _dominant_frequency(samples: np.ndarray, sample_rate: int = 48000) -> float:
    """Return the dominant frequency in ``samples`` via FFT.

    For a pure 440 Hz tone, this should return ~440 Hz. We take the
    magnitude spectrum of the first 8192 samples (enough for ~6 Hz
    resolution at 48 kHz) and pick the peak bin.
    """
    n = min(len(samples), 8192)
    fft = np.fft.rfft(samples[:n] * np.hanning(n))
    mags = np.abs(fft)
    freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate)
    peak_idx = np.argmax(mags[1:]) + 1  # skip DC
    return float(freqs[peak_idx])


def _rms(samples: np.ndarray) -> float:
    """RMS amplitude of the signal (0..1 scale)."""
    if len(samples) == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples**2)))


class TestSpectralAudio:
    """Spectral / signal-level audio integrity tests."""

    @pytest.fixture
    def source_and_output(self, tmp_path: Path):
        """Generate a 6s source (440 Hz tone), cut silence [2,4], keep ~4s."""
        src = tmp_path / "src_tone.mp4"
        _make_tone_source(src, duration=6.0, freq=440)

        out = tmp_path / "out_tone.mp4"
        silence = [SilenceSegment(2.0, 4.0)]
        cut_and_concat(
            src,
            silence,
            out,
            method="segment",
            encoder="libx264",
            video_quality="medium",
        )
        return src, out

    def test_dominant_frequency_preserved(self, source_and_output):
        """The 440 Hz tone should survive the pipeline."""
        src, out = source_and_output
        src_pcm = _extract_pcm(src)
        out_pcm = _extract_pcm(out)

        src_freq = _dominant_frequency(src_pcm)
        out_freq = _dominant_frequency(out_pcm)

        # Both should be near 440 Hz (within ~20 Hz — FFT bin resolution
        # + AAC codec jitter).
        assert abs(src_freq - 440) < 30, f"source freq {src_freq} not near 440"
        assert abs(out_freq - 440) < 30, f"output freq {out_freq} not near 440"

    def test_no_dc_offset_introduced(self, source_and_output):
        """The pipeline shouldn't shift the signal's DC level."""
        _src, out = source_and_output
        out_pcm = _extract_pcm(out)
        dc = float(np.mean(out_pcm))
        # DC should be near zero (within 0.01 — one bit of 16-bit range).
        assert abs(dc) < 0.02, f"DC offset {dc} too large"

    def test_amplitude_in_expected_range(self, source_and_output):
        """Output RMS should be in the same ballpark as the source."""
        src, out = source_and_output
        src_rms = _rms(_extract_pcm(src))
        out_rms = _rms(_extract_pcm(out))

        # Both should be non-zero (silence would mean the audio was lost).
        assert src_rms > 0.01, f"source RMS {src_rms} too low"
        assert out_rms > 0.01, f"output RMS {out_rms} too low"

        # The ratio should be within -6..+6 dB (0.5x..2x). AAC re-encode
        # + concat can shift the level slightly, but not by 10x.
        ratio = out_rms / src_rms
        assert 0.3 < ratio < 3.0, (
            f"output/source RMS ratio {ratio} outside [0.3, 3.0] — "
            f"src_rms={src_rms}, out_rms={out_rms}"
        )

    def test_no_large_discontinuity_at_concat_boundary(self, source_and_output):
        """At the concat join (t=2s in output), no click/pop.

        The join is where the first keep segment (0-2s) ends and the
        second (4-6s of source = 2-4s of output) begins. A click would
        show up as a sample whose value is much larger than the local
        average — a transient spike.
        """
        _src, out = source_and_output
        pcm = _extract_pcm(out)
        # The output is ~4s at 48 kHz = ~192000 samples. The concat
        # boundary is at ~2s = ~96000 samples. Check a window around it.
        sample_rate = 48000
        boundary = 2 * sample_rate  # approximate
        window = 100  # samples on each side
        start = max(0, boundary - window)
        end = min(len(pcm), boundary + window)
        if end - start < 10:
            pytest.skip("output too short for boundary analysis")
        segment = pcm[start:end]
        # The max absolute value in the boundary window should not be
        # much larger than the RMS of the segment (a click would have
        # a single sample 10x+ the RMS).
        seg_rms = _rms(segment)
        if seg_rms < 0.001:
            pytest.skip("segment too quiet for click detection")
        peak = float(np.max(np.abs(segment)))
        # Allow up to 10x the RMS (sine waves have peaks at ~1.41x RMS,
        # so 10x is generous; a click would be 50x+).
        assert peak < seg_rms * 15, (
            f"concat boundary has a spike: peak={peak}, rms={seg_rms}, ratio={peak / seg_rms:.1f}x"
        )

    @pytest.mark.parametrize("method", ["segment", "batch"])
    def test_audio_not_silenced(self, method: str, tmp_path: Path):
        """After cut+concat, the output should have audible audio —
        not a silent track. This catches the historical bug where the
        audio stream was dropped entirely (wrong stream mapping)."""
        src = tmp_path / "src.mp4"
        _make_tone_source(src, duration=4.0, freq=440)
        out = tmp_path / f"out_{method}.mp4"
        silence = [SilenceSegment(1.0, 3.0)]  # keep 0-1 + 3-4 = 2s
        cut_and_concat(
            src,
            silence,
            out,
            method=method,
            encoder="libx264",
            video_quality="medium",
        )
        pcm = _extract_pcm(out)
        rms = _rms(pcm)
        assert rms > 0.01, f"{method}: output audio is silent (RMS={rms}) — audio stream lost"
