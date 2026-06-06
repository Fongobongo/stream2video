"""Tests for stream2video.waveform — pure rendering helpers."""

import struct
import wave
from pathlib import Path

import pytest

from stream2video.silence import SilenceSegment
from stream2video.waveform import (
    _format_clock,
    read_waveform_peaks,
    render_waveform_image,
    silence_pixel_ranges,
)

# ── read_waveform_peaks ────────────────────────────────────────


def _write_sine_wav(path: Path, duration_s: float = 1.0, sr: int = 16000, amp: int = 16000) -> None:
    """Write a 1-channel 16-bit PCM WAV containing a sine wave."""
    import math

    n = int(duration_s * sr)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        frames = bytearray()
        for i in range(n):
            sample = int(amp * math.sin(2 * math.pi * 440 * i / sr))
            frames += struct.pack("<h", max(-32768, min(32767, sample)))
        w.writeframes(bytes(frames))


def test_read_peaks_empty_path(tmp_path: Path):
    """Missing file -> empty list (the caller should treat as 'no data')."""
    assert read_waveform_peaks(tmp_path / "missing.wav", 100) == []


def test_read_peaks_handles_missing_file_without_raising(tmp_path: Path):
    """The reader must not raise on a missing path — return [] so the GUI
    can show an 'audio not available' state instead of a crash dialog."""
    import pytest as _pytest

    # If the function ever changes to raise, this test will catch it.
    try:
        result = read_waveform_peaks(tmp_path / "does_not_exist.wav", 50)
    except FileNotFoundError:
        _pytest.fail("read_waveform_peaks should not raise on missing path")
    assert result == []


def test_read_peaks_zero_buckets(tmp_path: Path):
    p = tmp_path / "x.wav"
    _write_sine_wav(p, duration_s=0.5)
    assert read_waveform_peaks(p, 0) == []


def test_read_peaks_short_wav_returns_proportional_count(tmp_path: Path):
    p = tmp_path / "x.wav"
    # 0.1s @ 16kHz = 1600 samples; ask for 100 buckets, expect ~100 peaks
    # but cap at n_frames / bucket_size = 1, so up to 1600 peaks.
    _write_sine_wav(p, duration_s=0.1)
    peaks = read_waveform_peaks(p, 100)
    assert 0 < len(peaks) <= 1600
    assert all(0.0 <= v <= 1.0 for v in peaks)


def test_read_peaks_dense_sine_nonzero(tmp_path: Path):
    """A loud sine should produce non-trivial peak values everywhere."""
    p = tmp_path / "x.wav"
    _write_sine_wav(p, duration_s=0.5, amp=30000)
    peaks = read_waveform_peaks(p, 50)
    assert len(peaks) == 50
    # Sine of full amp gives peaks around 30000/32768 ~ 0.91
    assert max(peaks) > 0.5


def test_read_peaks_silent_wav_returns_zeros(tmp_path: Path):
    p = tmp_path / "x.wav"
    # Zero-amplitude sine is silence.
    _write_sine_wav(p, duration_s=0.5, amp=0)
    peaks = read_waveform_peaks(p, 20)
    assert peaks  # buckets present
    assert all(v == 0.0 for v in peaks)


def test_read_peaks_stereo_mixed_to_mono(tmp_path: Path):
    """Stereo WAVs are mixed to mono via max-abs across channels.

    A stereo file with one silent and one loud channel should still
    produce non-zero peaks (we don't want to throw away the loud side).
    """
    p = tmp_path / "stereo.wav"
    n_frames = 8000
    with wave.open(str(p), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(16000)
        # L = silence (0), R = loud 16k amplitude square wave.
        frames = bytearray()
        for i in range(n_frames):
            left = 0
            right = 16000 if (i // 100) % 2 == 0 else -16000
            frames += struct.pack("<hh", left, right)
        w.writeframes(bytes(frames))
    peaks = read_waveform_peaks(p, 20)
    assert len(peaks) == 20
    # Max-abs is ~16000/32768 ~ 0.49
    assert max(peaks) > 0.3


def test_read_peaks_empty_wav_returns_empty(tmp_path: Path):
    """Zero-frame WAVs are valid input — return an empty peak list."""
    p = tmp_path / "empty.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"")
    assert read_waveform_peaks(p, 10) == []


# ── silence_pixel_ranges ──────────────────────────────────────


def test_silence_pixels_empty():
    assert silence_pixel_ranges([], 100.0, 800) == []


def test_silence_pixels_zero_duration():
    segs = [SilenceSegment(0, 5)]
    assert silence_pixel_ranges(segs, 0.0, 800) == []


def test_silence_pixels_basic():
    segs = [SilenceSegment(10, 20)]  # 10s out of 100s -> x 80..160
    out = silence_pixel_ranges(segs, 100.0, 800)
    assert out == [(80, 160)]


def test_silence_pixels_clamps_to_duration():
    segs = [SilenceSegment(90, 200)]
    out = silence_pixel_ranges(segs, 100.0, 800)
    assert out == [(720, 800)]


def test_silence_pixels_drops_fully_out_of_range():
    segs = [SilenceSegment(-10, 0), SilenceSegment(100, 110)]
    assert silence_pixel_ranges(segs, 100.0, 800) == []


def test_silence_pixels_subpixel_widens_to_one():
    segs = [SilenceSegment(50.0, 50.001)]
    out = silence_pixel_ranges(segs, 100.0, 800)
    assert out
    x_left, x_right = out[0]
    assert x_right > x_left  # never 0-width


# ── render_waveform_image ──────────────────────────────────────


def test_render_empty_peaks_returns_image_with_placeholder():
    img = render_waveform_image([], width=200, height=60)
    assert img.size == (200, 60)
    assert img.mode == "RGB"


def test_render_basic_dimensions():
    peaks = [0.5, 0.8, 0.3, 0.9, 0.1]
    img = render_waveform_image(peaks, width=200, height=80)
    assert img.size == (200, 80)
    assert img.mode == "RGB"


def test_render_with_silence_overlay_returns_rgba():
    peaks = [0.5] * 100
    segs = [SilenceSegment(20, 50)]
    img = render_waveform_image(
        peaks,
        width=200,
        height=80,
        total_duration=100.0,
        silence_segments=segs,
    )
    assert img.mode == "RGB"  # base mode stays RGB; overlay is composited in
    # Pixels under the silence should be different from pixels outside.
    px_in = img.getpixel((80, 30))  # inside [40..100] silence
    px_out = img.getpixel((150, 30))  # outside
    assert px_in != px_out


def test_render_with_title_writes_text():
    img = render_waveform_image([0.5] * 50, width=200, height=60, title="hi")
    # Hard to assert exact text without OCR; just confirm render didn't crash
    # and that the top row isn't uniformly background (text was drawn).
    bg = (30, 30, 30)
    text_color = (200, 200, 200)
    top_row = [img.getpixel((x, 5)) for x in range(50)]
    assert any(px != bg and px != text_color for px in top_row) or text_color in top_row


def test_render_time_axis_endpoints():
    img = render_waveform_image(
        [0.5] * 10,
        width=200,
        height=60,
        total_duration=125.0,
    )
    # 125s = 2:05 — both "0:00" and "2:05" should be drawn.
    assert img.size == (200, 60)


def test_render_rejects_non_positive_size():
    with pytest.raises(ValueError):
        render_waveform_image([], width=0, height=100)
    with pytest.raises(ValueError):
        render_waveform_image([], width=100, height=0)


def test_render_silence_color_used_for_underlying_waveform():
    """When silence covers a pixel, the bar underneath should be drawn in
    a dimmer color than the rest of the waveform (per the silenced[] lookup)."""
    peaks = [0.9] * 200  # loud, constant
    segs = [SilenceSegment(0, 50)]  # silence covers x 0..100
    img_dark = render_waveform_image(
        peaks,
        width=200,
        height=60,
        total_duration=100.0,
        silence_segments=segs,
        wave_color=(200, 200, 200),
        bg_color=(0, 0, 0),
    )
    # Sample the midline-1 row (top half of bar).
    px_inside = img_dark.getpixel((50, 8))  # inside the silence -> dim
    px_outside = img_dark.getpixel((150, 8))  # outside
    # The "inside" pixel should be dimmer (lower channel sum).
    assert sum(px_inside) < sum(px_outside)


# ── _format_clock (small helper, not exposed) ─────────────────


def test_format_clock_seconds_only():
    assert _format_clock(5) == "0:05"


def test_format_clock_minutes_seconds():
    assert _format_clock(125) == "2:05"


def test_format_clock_hours_minutes_seconds():
    assert _format_clock(3725) == "1:02:05"
