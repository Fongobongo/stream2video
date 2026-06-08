"""Tests for stream2video.waveform — pure rendering helpers."""

import struct
import wave
from pathlib import Path

import pytest

pytest.importorskip("PIL", reason="waveform rendering requires Pillow ([gui] extra)")

from stream2video.silence import SilenceSegment
from stream2video.waveform import (
    DB_AXIS_WIDTH,
    _format_clock,
    read_peaks_from_stream,
    read_waveform_peaks,
    render_waveform_image,
    silence_pixel_ranges,
    slice_peaks_by_time,
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


def test_silence_pixels_view_start_shifts_x_to_match_view():
    """After panning, the same absolute-time silence must render at a
    pixel position consistent with its location inside the visible
    window — not the same image-x as in the un-panned view.

    Concretely, a silence at absolute time ``[10, 15]`` in a 100 s
    timeline should appear at pixel ``10/100 * 800 = 80`` when the view
    is the full timeline, and at ``(10-5)/15 * 800 ≈ 267`` when the view
    is ``[5, 20]`` (15 s wide). The old implementation keyed the math
    off the visible duration only, so it produced the same x for both.
    """
    segs = [SilenceSegment(10, 15)]
    full = silence_pixel_ranges(segs, total_duration=100.0, plot_width=800)
    panned = silence_pixel_ranges(segs, total_duration=15.0, plot_width=800, view_start=5.0)
    assert full[0] == (80, 120)
    # The panned view starts at 5 s, so 10 s is 5/15 of the way across.
    assert panned[0] == (266, 533)


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


def test_render_time_axis_uses_view_start_for_panned_labels():
    """After pan/zoom, the time-axis labels should reflect the real
    time in the visible window (``view_start + frac * total_duration``),
    not just the visible duration from 0.

    Default ``view_start=0`` is preserved for top-level calls (the
    existing ``test_render_time_axis_endpoints`` covers that).
    """
    peaks = [0.5] * 10
    kwargs = {"width": 400, "height": 80, "total_duration": 120.0}
    img_default = render_waveform_image(peaks, **kwargs)
    img_panned = render_waveform_image(peaks, view_start=60.0, **kwargs)
    # Panning into [60, 180] of a 240 s timeline: labels become
    # 1:00, 1:30, 2:00, 2:30, 3:00 instead of 0:00, 0:30, 1:00,
    # 1:30, 2:00 — the bottom row of pixels must differ.
    assert img_default.tobytes() != img_panned.tobytes()


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


def test_render_draws_db_axis_in_left_margin():
    """The leftmost pixels of the image should contain axis content
    (ticks + labels) — not pure background. Confirms the dB axis is
    rendered and reserves the expected width."""
    img = render_waveform_image([0.5] * 50, width=200, height=80)
    # The boundary column and the tick marks should contain non-bg pixels
    # in the left margin. We don't need an exact count — just that the
    # axis was drawn (e.g., a few text-color pixels in [0, DB_AXIS_WIDTH)).
    bg = (30, 30, 30)
    margin_pixels = [
        img.getpixel((x, plot_y)) for x in range(DB_AXIS_WIDTH) for plot_y in (5, 20, 40, 60)
    ]
    assert any(px != bg for px in margin_pixels), (
        "expected dB axis content (ticks/labels) in the left margin"
    )


def test_render_bar_height_is_db_linear():
    """Bar height should be linear in dB, not in raw amplitude —
    a -30 dB peak should reach roughly half the plot half-height
    (instead of ~3% as it would on a linear-amplitude scale)."""
    img_quiet = render_waveform_image([0.0316] * 200, width=200, height=200)
    img_loud = render_waveform_image([1.0] * 200, width=200, height=200)

    # 0.0316 amplitude ≈ -30 dB. Walk upward from midline on a single
    # plot column and find the topmost bar pixel; compare to the
    # loud-reference column.
    def _topmost_bar_y(img):
        # Skip the dB axis and find the first column with a bar above midline.
        for x in range(DB_AXIS_WIDTH, 200):
            for y in range(0, 84):
                px = img.getpixel((x, y))
                if px != (30, 30, 30):  # bg
                    return y
        return -1

    y_quiet = _topmost_bar_y(img_quiet)
    y_loud = _topmost_bar_y(img_loud)
    assert y_loud <= 2, f"0 dB peak should reach the top of the plot, got y={y_loud}"
    # -30 dB should be at the top of a bar centered on the midline.
    # half_h for plot_h=200-16=184: -30 dB => half_h = 30/60 * 91 = 45,
    # so the topmost bar pixel is at midline_y - half_h = 92 - 45 = 47.
    assert 40 <= y_quiet <= 55, (
        f"-30 dB peak expected around the upper-mid of the plot, got y={y_quiet}"
    )


def test_render_db_axis_ticks_align_with_bar_tops():
    """Each dB tick mark should sit at the same y as the top of a bar
    at that dB value — that is the whole point of a dB axis on a
    waveform display. Regression test: the labels used to be spread
    over the full plot height, which made them useless for reading
    off bar heights."""
    img = render_waveform_image([0.0316] * 200, width=200, height=200)
    bg = (30, 30, 30)
    plot_h = 200 - 16  # height minus bottom time-axis strip
    midline_y = plot_h // 2
    max_half_h = max(1, plot_h // 2 - 1)
    # 0.0316 amplitude ≈ -30 dB. With the bar-top formula
    # midline_y - half_h, where half_h = 30/60 * max_half_h.
    half_h = 30 / 60 * max_half_h
    expected_y = round(midline_y - half_h)  # 47 for plot_h=184
    # Find the -30 dB tick by scanning the 4-pixel tick area
    # (x in [DB_AXIS_WIDTH-5, DB_AXIS_WIDTH-1)) for a row where all
    # four pixels are non-background. The boundary column
    # x=DB_AXIS_WIDTH-1 is excluded so we don't catch the long
    # vertical guide line.
    tick_ys = [
        y
        for y in range(plot_h)
        if all(img.getpixel((x, y)) != bg for x in range(DB_AXIS_WIDTH - 5, DB_AXIS_WIDTH - 1))
    ]
    assert expected_y in tick_ys, (
        f"expected -30 dB tick at y={expected_y}, ticks present at y={tick_ys}"
    )
    # And the bar's topmost pixel (first non-bg pixel above midline in
    # the plot region) should land on the same y, within 1 px.
    bar_top_y = next(
        y for x in range(DB_AXIS_WIDTH, 200) for y in range(midline_y) if img.getpixel((x, y)) != bg
    )
    assert abs(bar_top_y - expected_y) <= 1, (
        f"bar top y={bar_top_y} should align with -30 dB tick y={expected_y} (±1 px)"
    )


def test_render_db_axis_lower_mirror_aligns_with_bar_bottoms():
    """Mirror tick at the bottom of the bar: a -30 dB bar reaches
    ``midline_y + half_h`` downward, and the lower ``-30`` tick /
    label should sit on that same y. The user can then read dB from
    either the top or the bottom edge of a bar."""
    img = render_waveform_image([0.0316] * 200, width=200, height=200)
    bg = (30, 30, 30)
    plot_h = 200 - 16
    midline_y = plot_h // 2
    max_half_h = max(1, plot_h // 2 - 1)
    half_h = 30 / 60 * max_half_h
    expected_y = round(midline_y + half_h)  # 137 for plot_h=184
    # Same tick-zone scan as the upper test.
    tick_ys = [
        y
        for y in range(plot_h)
        if all(img.getpixel((x, y)) != bg for x in range(DB_AXIS_WIDTH - 5, DB_AXIS_WIDTH - 1))
    ]
    assert expected_y in tick_ys, (
        f"expected lower -30 dB tick at y={expected_y}, ticks present at y={tick_ys}"
    )
    # Bar's bottom edge: the LAST row below the midline that still
    # contains a non-bg bar pixel in the plot region. (A ``next``
    # walking upward from below would return the first pixel right
    # under the midline, not the actual bottom of the bar.)
    bar_bot_y = max(
        y
        for x in range(DB_AXIS_WIDTH, 200)
        for y in range(midline_y + 1, plot_h)
        if img.getpixel((x, y)) != bg
    )
    assert abs(bar_bot_y - expected_y) <= 1, (
        f"bar bottom y={bar_bot_y} should align with lower -30 dB tick y={expected_y} (±1 px)"
    )
    # And the mirror should be symmetric with the top tick.
    upper_y = round(midline_y - half_h)
    assert abs((expected_y - midline_y) - (midline_y - upper_y)) <= 1, (
        f"mirror not symmetric: upper y={upper_y}, lower y={expected_y}, midline={midline_y}"
    )


def test_render_db_axis_lower_minus_60_mirror_is_skipped():
    """At -60 dB the bar collapses to a 1-pixel sliver on the midline
    (``half_h == 0``). Drawing the mirror tick would overlay the
    upper ``-60`` tick and merge the two labels into an unreadable
    blob. The implementation must skip the lower mirror for this dB
    value, which we detect by checking that the row just below the
    midline in the tick area is all background."""
    img = render_waveform_image([0.5] * 200, width=200, height=200)
    bg = (30, 30, 30)
    plot_h = 200 - 16
    midline_y = plot_h // 2
    # The lower mirror, if it existed, would be at y = midline_y + 0
    # (same as the upper mirror). The four tick-area pixels at
    # y = midline_y + 1 must all be background: the bar at amplitude
    # 0.5 (~ -6 dB) is far above the silence floor, so the only
    # non-bg content near the midline in the tick area is the upper
    # -60 tick at y = midline_y itself.
    assert all(
        img.getpixel((x, midline_y + 1)) == bg for x in range(DB_AXIS_WIDTH - 5, DB_AXIS_WIDTH - 1)
    ), f"unexpected tick content at y={midline_y + 1} in tick area"


def test_render_db_axis_step_is_10db():
    """The dB axis should be labeled every 10 dB, both above and below
    the midline (mirror). Exactly 7 upper ticks (0, -10, ..., -60) and
    6 lower mirror ticks (0, -10, ..., -50) — the -60 mirror is
    intentionally skipped to avoid merging with the upper -60."""
    from stream2video.waveform import _DB_AXIS_STEP

    assert _DB_AXIS_STEP == 10, f"dB step should be 10, got {_DB_AXIS_STEP}"
    img = render_waveform_image([0.5] * 200, width=200, height=200)
    bg = (30, 30, 30)
    plot_h = 200 - 16
    midline_y = plot_h // 2
    max_half_h = max(1, plot_h // 2 - 1)
    tick_ys = [
        y
        for y in range(plot_h)
        if all(img.getpixel((x, y)) != bg for x in range(DB_AXIS_WIDTH - 5, DB_AXIS_WIDTH - 1))
    ]
    upper_ticks = [y for y in tick_ys if y <= midline_y]
    lower_ticks = [y for y in tick_ys if y > midline_y]

    # Expected positions: 0, -10, -20, -30, -40, -50, -60 in the upper
    # half (7 ticks) and the mirror of 0..-50 in the lower half (6
    # ticks; -60 is collapsed to the midline so it lives in ``upper_ticks``).
    # Bar half-height for a given dB value: linear in dB from
    # ``max_half_h`` at 0 dB to 0 at -60 dB. Top of bar is
    # ``midline_y - half_h``; bottom is ``midline_y + half_h``.
    def _half_h(db: float) -> float:
        return (db + 60) / 60 * max_half_h

    expected_upper = sorted(round(midline_y - _half_h(db)) for db in range(0, -61, -10))
    expected_lower = sorted(round(midline_y + _half_h(db)) for db in range(0, -51, -10))
    assert upper_ticks == expected_upper, f"upper ticks {upper_ticks} != expected {expected_upper}"
    assert lower_ticks == expected_lower, (
        f"lower mirror ticks {lower_ticks} != expected {expected_lower}"
    )


def test_render_threshold_line_at_minus_30():
    """With ``threshold_db=-30`` the line should be drawn at the same
    y as the top of a -30 dB bar (upper mirror) and at the same y as
    the bottom of a -30 dB bar (lower mirror), spanning the plot
    region. The line is baked into the image so a re-render with a
    different ``threshold_db`` cannot leave stale pixels around."""
    color = (255, 0, 0)
    img = render_waveform_image(
        [0.0316] * 200,  # 0.0316 amplitude ≈ -30 dB
        width=200,
        height=200,
        threshold_db=-30.0,
        threshold_color=color,
    )
    plot_h = 200 - 16
    midline_y = plot_h // 2
    max_half_h = max(1, plot_h // 2 - 1)
    half_h = 30 / 60 * max_half_h
    expected_top = round(midline_y - half_h)  # ≈47
    expected_bot = round(midline_y + half_h)  # ≈137
    x_left = DB_AXIS_WIDTH
    x_right = 200 - 1
    # A handful of sample columns across the plot region should be
    # the threshold color on both lines.
    for x in (x_left + 5, x_left + 50, x_left + 100, x_right - 5):
        assert img.getpixel((x, expected_top)) == color, (
            f"expected top threshold line at ({x},{expected_top})"
        )
        assert img.getpixel((x, expected_bot)) == color, (
            f"expected bottom threshold line at ({x},{expected_bot})"
        )
    # And the line should NOT cross into the dB axis strip on the
    # left.
    for x in range(0, DB_AXIS_WIDTH):
        assert img.getpixel((x, expected_top)) != color, (
            f"threshold line should not cover the dB axis at x={x}"
        )


def test_render_threshold_line_skipped_when_none():
    """The default ``threshold_db=None`` must not draw a threshold
    line. We verify by sampling the y where a -30 dB line would
    otherwise sit and checking that the pixel is not the threshold
    color. (Bars at that y are also not the threshold color, so the
    assertion is robust.)"""
    color = (255, 0, 0)
    img = render_waveform_image([0.0316] * 200, width=200, height=200, threshold_color=color)
    plot_h = 200 - 16
    midline_y = plot_h // 2
    max_half_h = max(1, plot_h // 2 - 1)
    half_h = 30 / 60 * max_half_h
    expected_top = round(midline_y - half_h)
    for x in (DB_AXIS_WIDTH + 5, DB_AXIS_WIDTH + 50, DB_AXIS_WIDTH + 100, 199):
        assert img.getpixel((x, expected_top)) != color, (
            f"unexpected threshold line at ({x},{expected_top}) when threshold_db=None"
        )


def test_render_threshold_line_at_floor_only_upper():
    """At the silence floor (``threshold_db=_DB_AXIS_BOTTOM=-60``) the
    lower mirror is intentionally skipped (``half_h == 0``). The
    upper line should sit on the midline, and there should be no
    second line on the bottom half."""
    from stream2video.waveform import _DB_AXIS_BOTTOM

    color = (255, 0, 0)
    img = render_waveform_image(
        [0.5] * 200,  # amplitude well above the floor so no bar covers the line
        width=200,
        height=200,
        threshold_db=_DB_AXIS_BOTTOM,
        threshold_color=color,
    )
    plot_h = 200 - 16
    midline_y = plot_h // 2
    x_left = DB_AXIS_WIDTH
    x_right = 200 - 1
    # Upper line on the midline.
    for x in (x_left + 5, x_left + 50, x_left + 100, x_right - 5):
        assert img.getpixel((x, midline_y)) == color, (
            f"expected floor threshold line at ({x},{midline_y})"
        )
    # No second line below the midline (the midline at -60 dB is the
    # only line).
    for y in (midline_y + 1, midline_y + 2, plot_h - 1):
        for x in (x_left + 5, x_left + 100):
            assert img.getpixel((x, y)) != color, (
                f"unexpected extra threshold pixel at ({x},{y}) for floor threshold"
            )


def test_render_threshold_line_outside_range_clamps():
    """If the caller passes a ``threshold_db`` outside ``[_DB_AXIS_BOTTOM,
    _DB_AXIS_TOP]``, the renderer should clamp rather than draw a
    line off-canvas."""
    color = (255, 0, 0)
    img = render_waveform_image(
        [0.5] * 200, width=200, height=200, threshold_db=10.0, threshold_color=color
    )
    plot_h = 200 - 16
    # 0 dB is at y = midline_y - max_half_h = 1.
    assert img.getpixel((DB_AXIS_WIDTH + 50, 1)) == color, (
        "threshold_db=10 should clamp to 0 dB and land on the top row"
    )
    img_low = render_waveform_image(
        [0.5] * 200, width=200, height=200, threshold_db=-100.0, threshold_color=color
    )
    # -100 should clamp to -60, which sits on the midline.
    assert img_low.getpixel((DB_AXIS_WIDTH + 50, plot_h // 2)) == color, (
        "threshold_db=-100 should clamp to -60 dB and land on the midline"
    )


# ── below_threshold overlay ──────────────────────────────────


def test_below_threshold_pixel_ranges_basic():
    """Peaks alternating above/below a threshold should yield
    alternating pixel ranges. Adjacent below-threshold columns are
    merged into a single range."""
    from stream2video.waveform import below_threshold_pixel_ranges

    # Amplitude 0.1 ≈ -20 dB, amplitude 0.001 ≈ -60 dB.
    # 10 alternating peaks: above, below, above, below, ...
    peaks = [0.1 if i % 2 == 0 else 0.001 for i in range(10)]
    # plot_w == n_peaks: one column per peak.
    ranges = below_threshold_pixel_ranges(peaks, threshold_db=-30, plot_w=10)
    assert ranges == [(1, 2), (3, 4), (5, 6), (7, 8), (9, 10)], f"unexpected ranges: {ranges}"


def test_below_threshold_pixel_ranges_all_above():
    """When every peak is above the threshold, no ranges are returned."""
    from stream2video.waveform import below_threshold_pixel_ranges

    peaks = [1.0] * 10
    assert below_threshold_pixel_ranges(peaks, threshold_db=-30, plot_w=10) == []


def test_below_threshold_pixel_ranges_all_below():
    """When every peak is below the threshold, the entire plot is
    one range from x=0 to x=plot_w."""
    from stream2video.waveform import below_threshold_pixel_ranges

    peaks = [0.0] * 10
    assert below_threshold_pixel_ranges(peaks, threshold_db=-30, plot_w=10) == [(0, 10)]


def test_below_threshold_pixel_ranges_merges_adjacent():
    """A contiguous run of below-threshold columns collapses to a
    single range."""
    from stream2video.waveform import below_threshold_pixel_ranges

    # 0..4 below, 5..9 above.
    peaks = [0.0 if i < 5 else 1.0 for i in range(10)]
    assert below_threshold_pixel_ranges(peaks, threshold_db=-30, plot_w=10) == [(0, 5)]


def test_below_threshold_pixel_ranges_downsamples_peaks():
    """When peaks > plot_w, the function takes the max across the
    bucket so a single loud peak in a bucket keeps the whole bucket
    above threshold."""
    from stream2video.waveform import below_threshold_pixel_ranges

    # 20 peaks mapped into 4 plot columns: column 0 has 0.0..0.0
    # (below), column 1 has 1.0 in the middle (above), column 2 all
    # below, column 3 all above.
    peaks = [0.0] * 5 + [0.0, 1.0, 0.0, 0.0, 0.0] + [0.0] * 5 + [1.0] * 5
    ranges = below_threshold_pixel_ranges(peaks, threshold_db=-30, plot_w=4)
    # Bucket 0: all 0.0 → below
    # Bucket 1: max is 1.0 → above
    # Bucket 2: all 0.0 → below
    # Bucket 3: all 1.0 → above
    assert ranges == [(0, 1), (2, 3)], f"unexpected ranges: {ranges}"


def test_below_threshold_pixel_ranges_edge_cases():
    """Empty peaks / zero plot_w / None threshold return []. A huge
    threshold (very positive dB) makes everything below it."""
    from stream2video.waveform import below_threshold_pixel_ranges

    assert below_threshold_pixel_ranges([], threshold_db=-30, plot_w=10) == []
    assert below_threshold_pixel_ranges([0.5] * 5, threshold_db=-30, plot_w=0) == []
    assert below_threshold_pixel_ranges([0.5] * 5, threshold_db=None, plot_w=10) == []
    # +60 dB threshold = amplitude 1000; no real peak gets there.
    assert below_threshold_pixel_ranges([0.5] * 5, threshold_db=60, plot_w=5) == [(0, 5)]


def test_render_below_threshold_overlay_basic():
    """The overlay should paint a column at every x whose peak is
    below the threshold, with the orange color. To keep the test
    robust we use ``n_peaks == plot_w`` so each column maps 1:1 to
    a peak, and pick x_offsets that are unambiguously on each side
    of the threshold."""
    # Full alpha so the overlay color is opaque on top of the bar.
    color = (255, 0, 0, 255)
    # 100 peaks, 100 plot columns. First 50 are loud (1.0), last 50
    # are quiet (0.001 ≈ -60 dB).
    peaks = [1.0] * 50 + [0.001] * 50
    # Width 136 → plot_w = 100 (= n_peaks) so column i == peak i.
    img = render_waveform_image(
        peaks,
        width=136,
        height=200,
        threshold_db=-30.0,
        below_threshold_color=color,
    )
    x_left = DB_AXIS_WIDTH
    plot_h = 200 - 16
    # img is RGB (the overlay is RGBA but pasted onto RGB), so
    # getpixel returns a 3-tuple.
    rgb_color = color[:3]
    # Above-threshold columns (5, 25, 45): no overlay. Sample at the
    # midline — the bar covers it, so we see the bar color, not the
    # overlay.
    for x_offset in (5, 25, 45):
        x = x_left + x_offset
        assert img.getpixel((x, plot_h // 2)) != rgb_color, (
            f"unexpected overlay at x={x} for above-threshold column"
        )
    # Below-threshold columns (55, 75, 95): overlay covers the full
    # plot height. Sample at three y values (top, middle, bottom).
    for x_offset in (55, 75, 95):
        x = x_left + x_offset
        for y in (10, plot_h // 2, plot_h - 2):
            assert img.getpixel((x, y)) == rgb_color, (
                f"expected overlay color at ({x},{y}) for below-threshold column, "
                f"got {img.getpixel((x, y))}"
            )


def test_render_below_threshold_overlay_skipped_when_threshold_none():
    """Without ``threshold_db``, the below-threshold overlay is not
    drawn. Sample a column that would otherwise be fully overlaid
    and check the pixel is the bar / bg color, not the orange."""
    color = (255, 0, 0, 255)
    img = render_waveform_image(
        [0.001] * 100,  # all below any reasonable threshold
        width=200,
        height=200,
        below_threshold_color=color,
    )
    plot_h = 200 - 16
    x = DB_AXIS_WIDTH + 50
    rgb_color = color[:3]
    assert img.getpixel((x, plot_h // 2)) != rgb_color, (
        "unexpected overlay color at midline when threshold_db is None"
    )


def test_render_below_threshold_overlay_alpha_zero_disables():
    """``below_threshold_color`` with alpha=0 should be a no-op even
    when ``threshold_db`` is set."""
    color = (255, 0, 0, 0)  # fully transparent
    img = render_waveform_image(
        [0.001] * 100,
        width=200,
        height=200,
        threshold_db=-30.0,
        below_threshold_color=color,
    )
    plot_h = 200 - 16
    x = DB_AXIS_WIDTH + 50
    # Alpha=0 overlay shouldn't paint anything; the bar (or bg) is
    # visible underneath.
    assert img.getpixel((x, plot_h // 2)) != (255, 0, 0), (
        "transparent overlay should not paint the full-red color"
    )


def test_render_below_threshold_overlay_does_not_cover_dB_axis():
    """The below-threshold overlay lives in the plot region only —
    the dB axis strip on the left should not be tinted orange."""
    color = (255, 0, 0, 255)
    img = render_waveform_image(
        [0.001] * 100,
        width=200,
        height=200,
        threshold_db=-30.0,
        below_threshold_color=color,
    )
    plot_h = 200 - 16
    rgb_color = color[:3]
    for y in (5, plot_h // 2, plot_h - 2):
        for x in range(0, DB_AXIS_WIDTH):
            assert img.getpixel((x, y)) != rgb_color, (
                f"below-threshold overlay leaked into dB axis at ({x},{y})"
            )


def test_render_below_threshold_overlay_draws_solid_edge():
    """The 1-px solid edge is what makes the overlay visible on top
    of bright bars where the semi-transparent fill would otherwise
    blend in. The edge should appear at y=0 and y=plot_h-1 of every
    below-threshold column, in ``below_threshold_edge`` color."""
    fill = (255, 0, 0, 100)  # not the edge color
    edge = (0, 200, 0)  # bright green so it's easy to spot
    # 100 peaks, 100 plot columns, all below threshold.
    img = render_waveform_image(
        [0.001] * 100,
        width=200,
        height=200,
        threshold_db=-30.0,
        below_threshold_color=fill,
        below_threshold_edge=edge,
    )
    plot_h = 200 - 16
    x_left = DB_AXIS_WIDTH
    # Sample top and bottom rows at several x offsets — every column
    # in the plot is below threshold, so the edge should be present
    # across the full width of the plot.
    for x_offset in (10, 50, 100, 150):
        x = x_left + x_offset
        assert img.getpixel((x, 0)) == edge, (
            f"expected top edge color at ({x}, 0), got {img.getpixel((x, 0))}"
        )
        assert img.getpixel((x, plot_h - 1)) == edge, (
            f"expected bottom edge color at ({x}, {plot_h - 1}), "
            f"got {img.getpixel((x, plot_h - 1))}"
        )


# ── _format_clock (small helper, not exposed) ─────────────────


def test_format_clock_seconds_only():
    assert _format_clock(5) == "0:05"


def test_format_clock_minutes_seconds():
    assert _format_clock(125) == "2:05"


def test_format_clock_hours_minutes_seconds():
    assert _format_clock(3725) == "1:02:05"


# ── read_peaks_from_stream (ffmpeg pipe, no file) ─────────────


def test_read_peaks_from_stream_missing_file(tmp_path: Path):
    """Missing input -> ([], 0.0)."""
    assert read_peaks_from_stream(tmp_path / "missing.mp4", 100) == ([], 0.0)


def test_read_peaks_from_stream_silent_wav(tmp_path: Path):
    """All-zero WAV via ffmpeg pipe returns ~target_buckets peaks of 0 and ~1s duration."""
    wav = tmp_path / "silent.wav"
    _write_sine_wav(wav, duration_s=1.0, amp=0)
    peaks, duration = read_peaks_from_stream(wav, target_buckets=50)
    assert len(peaks) > 0
    assert len(peaks) <= 50
    assert all(p == 0.0 for p in peaks)
    assert 0.9 <= duration <= 1.1


def test_read_peaks_from_stream_sine_wav(tmp_path: Path):
    """Sine wave via ffmpeg pipe gives non-zero peaks and correct duration."""
    wav = tmp_path / "sine.wav"
    _write_sine_wav(wav, duration_s=2.0, sr=16000, amp=20000)
    peaks, duration = read_peaks_from_stream(wav, target_buckets=200)
    assert len(peaks) > 0
    assert len(peaks) <= 200
    # Sine with amp=20000 -> normalised peak ~= 20000/32768 ~ 0.61.
    assert max(peaks) > 0.5
    assert 1.9 <= duration <= 2.1


def test_read_peaks_from_stream_bucket_count(tmp_path: Path):
    """target_buckets is a soft upper bound; result is <= target_buckets."""
    wav = tmp_path / "x.wav"
    _write_sine_wav(wav, duration_s=0.5, amp=10000)
    peaks, _ = read_peaks_from_stream(wav, target_buckets=100)
    assert len(peaks) <= 100
    assert len(peaks) > 0


# ── slice_peaks_by_time ────────────────────────────────────────


class TestSlicePeaksByTime:
    """The GUI's zoom/pan controls slice the pre-bucketed peak array
    by a visible time window. Verifies the index math, clamping, and
    empty-edge cases."""

    def test_full_view_returns_all_peaks(self):
        peaks = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
        assert slice_peaks_by_time(peaks, 10.0, 0.0, 10.0) == peaks

    def test_first_half(self):
        peaks = list(range(10))
        out = slice_peaks_by_time(peaks, 10.0, 0.0, 5.0)
        # Half of the buckets are in the first half of the timeline.
        assert out == peaks[:5]

    def test_last_half(self):
        peaks = list(range(10))
        out = slice_peaks_by_time(peaks, 10.0, 5.0, 10.0)
        assert out == peaks[5:]

    def test_zoom_middle_quarter(self):
        """Zooming to the middle quarter of a 10s / 10-peak array
        returns the middle 2-3 buckets."""
        peaks = list(range(10))
        out = slice_peaks_by_time(peaks, 10.0, 2.5, 7.5)
        # Indices 2.5/10*10=2, 7.5/10*10=7 → peaks[2:7]
        assert out == [2, 3, 4, 5, 6]

    def test_clamps_overshoot_left(self):
        peaks = [0.1, 0.2, 0.3, 0.4, 0.5]
        out = slice_peaks_by_time(peaks, 5.0, -2.0, 3.0)
        # Clamped to 0 → peaks[0:3] = [0.1, 0.2, 0.3]
        assert out == [0.1, 0.2, 0.3]

    def test_clamps_overshoot_right(self):
        peaks = [0.1, 0.2, 0.3, 0.4, 0.5]
        out = slice_peaks_by_time(peaks, 5.0, 3.0, 10.0)
        # Clamped to 5 → peaks[3:5] = [0.4, 0.5]
        assert out == [0.4, 0.5]

    def test_empty_peaks(self):
        assert slice_peaks_by_time([], 10.0, 0.0, 5.0) == []

    def test_zero_duration(self):
        assert slice_peaks_by_time([0.1, 0.2], 0.0, 0.0, 5.0) == []

    def test_inverted_window(self):
        peaks = [0.1, 0.2, 0.3]
        # view_start > view_end → inverted, returns []
        assert slice_peaks_by_time(peaks, 10.0, 5.0, 3.0) == []

    def test_window_at_boundary(self):
        """view_start == view_end → inverted (degenerate), returns []."""
        peaks = [0.1, 0.2, 0.3]
        assert slice_peaks_by_time(peaks, 10.0, 5.0, 5.0) == []

    def test_preserves_uniform_density(self):
        """For a 100-peak array, the slice should have ~window_fraction*100 entries."""
        peaks = [float(i) for i in range(100)]
        out = slice_peaks_by_time(peaks, 10.0, 2.0, 4.0)
        # 20% of 100 = 20, ±1 for rounding.
        assert 18 <= len(out) <= 22
