"""Waveform rendering and silence overlay (pure functions, no Tk).

Renders a downsampled audio waveform to a Pillow Image with detected
silence segments drawn as semi-transparent overlays. Used by the GUI's
"Render preview" button so users can tune ``threshold`` / ``min_silence`` /
``margin`` visually instead of running a full encode to see the result.

WAV format expected: mono PCM s16le (matches the cached ``_audio.wav``
produced by ``silence._extract_audio_wav``). Any 16-bit PCM mono WAV
with a known sample width works — the reader doesn't care about the
sample rate beyond what ``silence`` writes (16 kHz).
"""

from __future__ import annotations

import math
import struct
import subprocess
import wave
from collections.abc import Sequence
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from stream2video.silence import SilenceSegment
from stream2video.utils import no_window_kwargs

# Canvas sizing constants. Exposed as module-level so tests can pin them.
_DEFAULT_WIDTH = 800
_DEFAULT_HEIGHT = 200
_AXIS_HEIGHT = 16
_SILENCE_FILL = (220, 50, 47, 70)  # red, ~28% alpha
_SILENCE_EDGE = (220, 50, 47, 220)  # solid red for outline
# Warm yellow for the user-set silence threshold line. Chosen to be
# distinct from the red silence overlay and the gray bars/text so the
# threshold stays readable on a busy waveform.
_THRESHOLD_LINE = (240, 200, 60)
# Orange for the "below threshold" overlay: shows, column-by-column,
# where the audio would be cut if we applied the threshold directly
# (without the silence detector's min-duration filter). Distinct from
# the red silence overlay (which is the actually-detected cut) and
# the yellow threshold line. The fill is moderately opaque (~40%) so
# it stays visible over the gray bars; the edge is solid orange so
# the rectangle is clearly outlined even where it covers a tall bar.
_BELOW_THRESHOLD_FILL = (255, 100, 0, 100)  # bright orange, ~40% alpha
_BELOW_THRESHOLD_EDGE = (255, 140, 30)  # solid orange (RGB, no alpha)
# Left-margin width reserved for the dB axis (ticks + labels).
# Exposed so the GUI can map cursor pixels into plot-space coordinates.
DB_AXIS_WIDTH = 36
# dB display range: top of plot = 0 dB, bottom = _DB_AXIS_BOTTOM.
# 60 dB matches the silence-detection threshold range ([-60, -5]).
_DB_AXIS_TOP = 0.0
_DB_AXIS_BOTTOM = -60.0
_DB_AXIS_STEP = 10  # tick every 10 dB
# Minimum on-screen amplitude (peaks below this are pinned to the bottom
# of the plot — they're effectively silence anyway).
_DB_FLOOR = 1e-4  # = -80 dB


def read_waveform_peaks(wav_path: Path, target_buckets: int) -> list[float]:
    """Read a 16-bit PCM mono (or stereo) WAV and return ``target_buckets`` peak values.

    Each returned value is the max-abs amplitude (in [-1, 1]) of one
    bucket of samples. Empty WAVs return an empty list. The result is
    suitable for symmetric waveform rendering (a positive and a negative
    bar centered on the canvas midline).

    Reads in fixed-size chunks to keep peak memory at one chunk's worth
    of samples (~64 KB per chunk) regardless of input length. Stereo
    inputs are mixed to mono by taking the max-abs across channels.
    Returns ``[]`` for missing files or non-16-bit formats — the caller
    can show an 'audio not available' state without exception handling.
    """
    if target_buckets <= 0 or not wav_path.is_file():
        return []
    with wave.open(str(wav_path), "rb") as w:
        n_channels = w.getnchannels()
        sampwidth = w.getsampwidth()
        n_frames = w.getnframes()
        if n_frames == 0 or n_channels == 0 or sampwidth != 2:
            return []

        # Bucket size: how many INPUT frames feed into one output peak.
        # Use ceiling so the last bucket may be slightly larger (a tail
        # of silence is fine — the last peak is just less averaged).
        bucket_size = max(1, math.ceil(n_frames / target_buckets))

        # Per channel, the audio WAV from the project is always mono
        # (n_channels == 1). If someone hands us stereo, mix to mono
        # by taking the max-abs across channels for each sample.
        chunk_frames = max(bucket_size, 4096)

        peaks: list[float] = []
        remaining = n_frames
        bucket_acc = 0
        bucket_count = 0

        while remaining > 0:
            to_read = min(chunk_frames, remaining)
            raw = w.readframes(to_read)
            if not raw:
                break
            n_samples = len(raw) // sampwidth
            # Build the format string per-chunk to match the actual count.
            samples = struct.unpack(f"<{n_samples}h", raw)
            for i in range(0, len(samples), n_channels):
                if n_channels == 1:
                    s = samples[i]
                else:
                    s = max(samples[i : i + n_channels], key=abs)
                bucket_acc = max(bucket_acc, abs(s))
                bucket_count += 1
                if bucket_count >= bucket_size:
                    peaks.append(bucket_acc / 32768.0)
                    bucket_acc = 0
                    bucket_count = 0
            remaining -= to_read

        # Tail bucket (partial) — flush whatever was accumulated.
        if bucket_count > 0:
            peaks.append(bucket_acc / 32768.0)

        # If we somehow got fewer buckets than requested, the WAV is
        # shorter than ``target_buckets`` samples — that's fine, callers
        # treat the result as proportional to duration.
        return peaks


def read_peaks_from_stream(
    input_path: Path,
    target_buckets: int,
) -> tuple[list[float], float]:
    """Stream audio from ``input_path`` via ffmpeg and return (peaks, duration).

    Single ffmpeg invocation: decodes the input and resamples to
    s16le / mono / 16 kHz on stdout. No file is written. Duration is
    derived from the total sample count (bytes / 2 / 16000).

    Returns ``([], 0.0)`` on error or empty audio. Peaks are normalised
    to ``[0.0, 1.0]`` (max-abs divided by 32768), matching
    :func:`read_waveform_peaks`. Memory peaks at the raw audio byte
    count — for a 2 h stream at 16 kHz mono that's ~230 MB.

    The function intentionally ignores stderr (silenced via
    ``-loglevel error`` to ``DEVNULL``) so silencedetect noise from
    :func:`stream2video.silence.detect_silence_stream` running in
    parallel is irrelevant. For preview-only use, this is faster and
    simpler than threading stderr parsing.
    """
    if target_buckets <= 0 or not Path(input_path).is_file():
        return [], 0.0

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-loglevel",
        "error",
        "-i",
        str(input_path),
        "-vn",
        "-sn",
        "-dn",
        "-f",
        "s16le",
        "-ac",
        "1",
        "-ar",
        "16000",
        "pipe:1",
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=-1,
            **no_window_kwargs(),
        )
    except FileNotFoundError:
        return [], 0.0

    assert proc.stdout is not None
    raw = proc.stdout.read()
    proc.wait()
    if proc.returncode != 0 or not raw:
        return [], 0.0

    total_samples = len(raw) // 2
    if total_samples == 0:
        return [], 0.0

    duration = total_samples / 16000.0
    bucket_size = max(1, math.ceil(total_samples / target_buckets))

    # Iterate the bytearray in chunks to avoid allocating a full sample
    # list (the unpacked list of 2h-16kHz-mono would be ~460 MB).
    peaks: list[float] = []
    bucket_acc = 0
    bucket_count = 0
    offset = 0
    chunk_samples = 8192
    while offset < total_samples:
        n = min(chunk_samples, total_samples - offset)
        chunk = struct.unpack_from(f"<{n}h", raw, offset * 2)
        for s in chunk:
            bucket_acc = max(bucket_acc, abs(s))
            bucket_count += 1
            if bucket_count >= bucket_size:
                peaks.append(bucket_acc / 32768.0)
                bucket_acc = 0
                bucket_count = 0
        offset += n

    if bucket_count > 0:
        peaks.append(bucket_acc / 32768.0)

    return peaks, duration


def silence_pixel_ranges(
    segments: Sequence[SilenceSegment],
    total_duration: float,
    plot_width: int,
    view_start: float = 0.0,
) -> list[tuple[int, int]]:
    """Map silence ``(start, end)`` ranges to ``(x_left, x_right)`` pixels
    within the visible window ``[view_start, view_start + total_duration)``.

    Segments outside the window are dropped; segments that overlap the
    window are clamped to it. Sub-pixel-wide silences are widened to 1
    px so they still render as a thin line. ``total_duration`` <= 0 or
    ``plot_width`` <= 0 returns [].

    ``view_start`` defaults to ``0.0`` so the function is a drop-in
    replacement for the original ``[0, total_duration)`` mapping — pass
    the actual left edge of the visible window when the renderer is
    showing a panned/zoomed sub-region of the timeline.
    """
    if total_duration <= 0 or plot_width <= 0:
        return []
    view_end = view_start + total_duration
    result: list[tuple[int, int]] = []
    for seg in segments:
        if seg.end <= view_start or seg.start >= view_end:
            continue
        start = max(view_start, seg.start)
        end = min(view_end, seg.end)
        if end <= start:
            continue
        # Map the segment's position within the visible window.
        x_left = int((start - view_start) / total_duration * plot_width)
        x_right = int((end - view_start) / total_duration * plot_width)
        # Ensure the right edge advances at least 1px past the left so
        # sub-pixel-wide silences still render as a thin line.
        if x_right == x_left:
            x_right = min(plot_width, x_left + 1)
        x_left = max(0, min(plot_width, x_left))
        x_right = max(0, min(plot_width, x_right))
        result.append((x_left, x_right))
    return result


def below_threshold_pixel_ranges(
    peaks: Sequence[float],
    threshold_db: float,
    plot_w: int,
) -> list[tuple[int, int]]:
    """Map each plot column to a binary "below threshold" flag and
    return the ``(x_left, x_right)`` pixel ranges where audio would
    be cut if we applied the threshold directly (without the silence
    detector's min-duration / margin filters).

    This is the "what would the threshold cut" view: it complements
    the silence overlay (red, the actually-detected cut) by showing
    the broader region of audio that's below the threshold level.
    Adjacent below-threshold columns are merged into a single range
    so the caller gets a compact list of rectangles to draw.
    """
    if plot_w <= 0 or not peaks or threshold_db is None:
        return []
    # Convert threshold dB to linear amplitude so we can compare
    # directly against the peak values (which are linear amplitudes).
    try:
        threshold_amp = 10 ** (threshold_db / 20)
    except OverflowError:
        # threshold_db is huge — nothing will be below it.
        return []
    n_peaks = len(peaks)
    ranges: list[tuple[int, int]] = []
    in_below = False
    start_x = 0
    for x in range(plot_w):
        # Same peak-to-pixel mapping as the bar renderer so the
        # "below threshold" rectangles align with the bar columns.
        if n_peaks == plot_w:
            p = peaks[x]
        elif n_peaks > plot_w:
            start = x * n_peaks // plot_w
            end = max(start + 1, (x + 1) * n_peaks // plot_w)
            p = max(peaks[start:end])
        else:
            p = peaks[x * n_peaks // plot_w]
        below = p < threshold_amp
        if below and not in_below:
            start_x = x
            in_below = True
        elif not below and in_below:
            ranges.append((start_x, x))
            in_below = False
    if in_below:
        ranges.append((start_x, plot_w))
    return ranges


def slice_peaks_by_time(
    peaks: Sequence[float],
    total_duration: float,
    view_start: float,
    view_end: float,
) -> list[float]:
    """Return the subset of ``peaks`` that covers ``[view_start, view_end)``.

    Used by the GUI's zoom/pan controls to map a visible time window
    onto the pre-bucketed peak array. ``peaks`` is assumed to be
    uniformly distributed across ``[0, total_duration]`` (the contract
    of :func:`read_waveform_peaks` and :func:`read_peaks_from_stream`),
    so slicing is a simple index range plus a max-pool if the slice
    contains more peaks than fit in one output bucket.

    Clamps ``view_start``/``view_end`` to ``[0, total_duration]``.
    Returns ``[]`` for empty peaks, zero/negative duration, or
    inverted windows (view_start >= view_end after clamping).
    """
    if not peaks or total_duration <= 0:
        return []
    # Clamp to valid range.
    vs = max(0.0, min(float(view_start), total_duration))
    ve = max(0.0, min(float(view_end), total_duration))
    if ve <= vs:
        return []
    # Map [vs, ve] to bucket indices [lo, hi] in `peaks`.
    n = len(peaks)
    lo = int(vs / total_duration * n)
    hi = max(lo + 1, int(ve / total_duration * n))
    hi = min(n, hi)
    return list(peaks[lo:hi])


def _format_clock(seconds: float) -> str:
    total = int(seconds)
    h, r = divmod(total, 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def render_waveform_image(
    peaks: Sequence[float],
    width: int = _DEFAULT_WIDTH,
    height: int = _DEFAULT_HEIGHT,
    *,
    bg_color: tuple[int, int, int] = (30, 30, 30),
    wave_color: tuple[int, int, int] = (180, 180, 180),
    midline_color: tuple[int, int, int] = (80, 80, 80),
    text_color: tuple[int, int, int] = (200, 200, 200),
    silence_fill: tuple[int, int, int, int] = _SILENCE_FILL,
    silence_edge: tuple[int, int, int, int] = _SILENCE_EDGE,
    total_duration: float | None = None,
    silence_segments: Sequence[SilenceSegment] | None = None,
    title: str | None = None,
    view_start: float = 0.0,
    threshold_db: float | None = None,
    threshold_color: tuple[int, int, int] = _THRESHOLD_LINE,
    below_threshold_color: tuple[int, int, int, int] = _BELOW_THRESHOLD_FILL,
    below_threshold_edge: tuple[int, int, int] = _BELOW_THRESHOLD_EDGE,
) -> Image.Image:
    """Render a waveform image with optional silence overlay.

    Returns an ``Image`` in mode ``"RGB"`` (or ``"RGBA"`` if overlays are
    drawn). ``peaks`` is one amplitude value per horizontal pixel-bucket;
    the waveform is drawn symmetrically around the vertical midline.

    Layout: a ``DB_AXIS_WIDTH``-pixel strip on the left holds the dB
    axis (ticks every 5 dB from 0 at the top to -60 at the bottom). The
    waveform / silence / time-axis are drawn in the remaining plot
    region. Bar heights are linear in dB so the tick positions match
    the visible peaks (the silence-detection threshold is in dB, so
    visual dB alignment is what users actually want).

    ``width`` and ``height`` set the canvas size. ``bg_color`` /
    ``wave_color`` / ``midline_color`` / ``text_color`` are RGB tuples.
    ``silence_fill`` and ``silence_edge`` are RGBA tuples — alpha is
    honoured for the fill (semi-transparent overlay) and ignored for the
    edge (solid 1-px outline).

    ``total_duration`` and ``silence_segments`` are both required to
    render the overlay; if either is None the silence layer is skipped.
    ``title`` is drawn at the top-left, just inside the plot region.

    ``view_start`` is the real time (seconds) at the left edge of the
    visible window; the time axis labels are ``view_start + frac *
    total_duration``. With the default ``0.0`` the labels run from 0 to
    ``total_duration`` (the original behaviour). When the GUI pans /
    zooms into a sub-window, it passes the actual ``view_start`` so the
    bottom labels stay in sync with the title.

    ``threshold_db`` (optional) draws a horizontal line at the y of the
    given dB value, mirrored above and below the midline, in
    ``threshold_color``. The line is baked into the returned image so
    it cannot "leak" or accumulate across motion / redraw events — the
    GUI only needs to call this function with a fresh ``threshold_db``
    to update it. The threshold at the silence floor (``-60 dB``) is
    drawn on the midline only (the lower mirror is suppressed, matching
    the dB axis labels). ``None`` (the default) skips the line entirely.

    When ``threshold_db`` is set, the renderer also draws a
    "below threshold" overlay (orange, semi-transparent fill with a
    1-px solid edge) covering every column whose peak amplitude is
    below the threshold. This visualises "what would be cut if we
    applied the threshold directly" — a superset of the actual
    silence overlay (red), which only marks detected silences
    (min-duration passes). The ``below_threshold_color`` is an RGBA
    tuple; pass alpha=0 to disable. ``below_threshold_edge`` is the
    solid 1-px outline drawn around each rectangle (RGB tuple, no
    alpha) — the edge is what keeps the overlay visible on top of
    bright bars where the semi-transparent fill would otherwise
    blend in. The overlay is drawn under the silence overlay so the
    red silences still dominate in the actually-cut regions.
    """
    if width <= 0 or height <= 0:
        raise ValueError(f"width and height must be positive, got {width}x{height}")

    plot_h = max(1, height - _AXIS_HEIGHT)
    plot_w = max(1, width - DB_AXIS_WIDTH)
    midline_y = plot_h // 2
    x_left = DB_AXIS_WIDTH

    img = Image.new("RGB", (width, height), bg_color)
    draw = ImageDraw.Draw(img)

    # dB axis first so the waveform overlays it cleanly.
    _draw_db_axis(draw, plot_h, text_color)

    if not peaks:
        # Empty waveform — still draw the axis below, and an 'empty' hint.
        if title:
            draw.text((x_left + 4, 2), title, fill=text_color)
        draw.text((x_left + 4, midline_y - 6), "(no audio)", fill=text_color)
        _draw_time_axis(draw, x_left, plot_w, plot_h, text_color, total_duration, view_start)
        return img

    # Pre-compute which x-pixels are silenced, so the waveform bars
    # under a silence are drawn in a slightly different (dimmer) color.
    # Index is in plot-space (0..plot_w-1); translate to image-x when
    # drawing.
    silenced = bytearray(plot_w)
    if total_duration and total_duration > 0 and silence_segments:
        for x_lp, x_rp in silence_pixel_ranges(
            silence_segments, total_duration, plot_w, view_start
        ):
            for x in range(x_lp, x_rp):
                if 0 <= x < plot_w:
                    silenced[x] = 1

    # Map N peaks into plot_w pixels. If peaks > plot_w, take max-pool;
    # if peaks < plot_w, stretch linearly.
    n_peaks = len(peaks)
    bar_color_silenced: tuple[int, int, int] = tuple(  # type: ignore[assignment]
        max(0, c - 60) for c in wave_color
    )

    def _bar_color(plot_x: int) -> tuple[int, int, int]:
        return bar_color_silenced if bool(silenced[plot_x]) else wave_color

    # Pre-compute the dB scale bar height lookup. Mapping 0 dB -> midline_y,
    # _DB_AXIS_BOTTOM dB -> 1 px (visibility floor). Linear in dB across
    # the range, so a -30 dB peak reaches halfway up.
    db_range = _DB_AXIS_TOP - _DB_AXIS_BOTTOM  # > 0
    max_half_h = max(1, plot_h // 2 - 1)

    def _half_h_for_db(db: float) -> int:
        clamped = max(_DB_AXIS_BOTTOM, min(_DB_AXIS_TOP, db))
        h = (clamped - _DB_AXIS_BOTTOM) / db_range * max_half_h
        return max(1, int(h))

    for px in range(plot_w):
        if n_peaks == plot_w:
            p = peaks[px]
        elif n_peaks > plot_w:
            start = px * n_peaks // plot_w
            end = max(start + 1, (px + 1) * n_peaks // plot_w)
            p = max(peaks[start:end])
        else:
            p = peaks[px * n_peaks // plot_w]
        if p < _DB_FLOOR:
            continue
        db = 20 * math.log10(p)
        h = _half_h_for_db(db)
        color = _bar_color(px)
        draw.line(
            [(x_left + px, midline_y - h), (x_left + px, midline_y + h)],
            fill=color,
        )

    # Midline (in the plot region).
    draw.line([(x_left, midline_y), (x_left + plot_w - 1, midline_y)], fill=midline_color)

    # "Below threshold" overlay: column-by-column view of where the
    # audio would be cut if we applied the threshold directly,
    # independent of the silence detector's min-duration filter. This
    # is a superset of the silence overlay — anything that's red
    # (silence) is also orange (below threshold), but the orange
    # extends to short quiet moments that didn't make the silence
    # cut. Drawn before the silence overlay so the red still wins in
    # actually-cut regions. A 1-px solid edge (drawn via
    # ``outline=``) keeps the rectangle visible even where the fill
    # blends with a tall bar; without it the overlay is easy to miss
    # on bright sections of the waveform.
    if threshold_db is not None and below_threshold_color[3] > 0:
        bt_ranges = below_threshold_pixel_ranges(peaks, threshold_db, plot_w)
        if bt_ranges:
            bt_overlay = Image.new("RGBA", (plot_w, plot_h), (0, 0, 0, 0))
            bt_draw = ImageDraw.Draw(bt_overlay)
            for x_lp, x_rp in bt_ranges:
                bt_draw.rectangle(
                    [(x_lp, 0), (x_rp - 1, plot_h - 1)],
                    fill=below_threshold_color,
                    outline=below_threshold_edge,
                )
            img.paste(bt_overlay, (x_left, 0), bt_overlay)

    # Silence overlay on top of the waveform.
    if total_duration and total_duration > 0 and silence_segments:
        overlay = Image.new("RGBA", (plot_w, plot_h), (0, 0, 0, 0))
        ov_draw = ImageDraw.Draw(overlay)
        for x_lp, x_rp in silence_pixel_ranges(
            silence_segments, total_duration, plot_w, view_start
        ):
            ov_draw.rectangle(
                [(x_lp, 0), (x_rp - 1, plot_h - 1)],
                fill=silence_fill,
                outline=silence_edge[:3],
            )
        img.paste(overlay, (x_left, 0), overlay)

    if title:
        draw.text((x_left + 4, 2), title, fill=text_color)

    if threshold_db is not None:
        _draw_threshold_lines(draw, x_left, plot_w, plot_h, threshold_db, threshold_color)

    _draw_time_axis(draw, x_left, plot_w, plot_h, text_color, total_duration, view_start)
    return img


def _draw_threshold_lines(
    draw: ImageDraw.ImageDraw,
    x_left: int,
    plot_w: int,
    plot_h: int,
    threshold_db: float,
    color: tuple[int, int, int],
) -> None:
    """Draw a horizontal line at the y of ``threshold_db`` in the
    plot, mirrored above and below the midline.

    The y position uses the same dB-to-pixel mapping as the bar
    height and the dB axis labels, so the line lands exactly on the
    top (or bottom) of a bar that crosses the threshold. At the
    silence floor (``_DB_AXIS_BOTTOM``) the lower mirror is skipped:
    ``half_h == 0`` would draw on top of the upper line and merge
    with the midline.

    The line spans the full plot width (left edge ``x_left``,
    right edge ``x_left + plot_w - 1``). The dB-axis strip on the
    left is intentionally not covered — the threshold is a plot-only
    affordance and the axis labels are the source of truth for
    reading dB values.
    """
    if plot_w <= 0 or plot_h <= 0:
        return
    db_range = _DB_AXIS_TOP - _DB_AXIS_BOTTOM
    midline_y = plot_h // 2
    max_half_h = max(1, plot_h // 2 - 1)
    clamped = max(_DB_AXIS_BOTTOM, min(_DB_AXIS_TOP, threshold_db))
    frac = (_DB_AXIS_TOP - clamped) / db_range
    half_h = (1 - frac) * max_half_h
    y_top = round(midline_y - half_h)
    y_top = max(0, min(plot_h - 1, y_top))
    x_right = x_left + plot_w - 1
    draw.line([(x_left, y_top), (x_right, y_top)], fill=color, width=1)
    if half_h > 0:
        y_bot = round(midline_y + half_h)
        y_bot = max(0, min(plot_h - 1, y_bot))
        draw.line([(x_left, y_bot), (x_right, y_bot)], fill=color, width=1)


def _draw_db_axis(
    draw: ImageDraw.ImageDraw,
    plot_h: int,
    text_color: tuple[int, int, int],
) -> None:
    """Draw the left-side dB axis: a vertical strip with tick marks and
    labels every ``_DB_AXIS_STEP`` dB.

    For each dB value two ticks / labels are drawn, mirrored across
    the plot's midline:

    * the **upper** one sits at the y of the top of a bar at that dB
      (so the eye can read dB from the top edge of any bar);
    * the **lower** one sits at the y of the bottom of a bar at that
      dB (so the eye can also read dB from the bottom edge).

    The ``_DB_AXIS_BOTTOM`` (``-60 dB``) mirror is intentionally
    omitted: the bar at the silence floor collapses to the midline
    (``half_h == 0``), so the upper and lower ticks would be drawn on
    top of each other and the labels would visually merge. The single
    upper ``-60`` tick on the midline is enough.

    The strip is ``DB_AXIS_WIDTH`` pixels wide; the plot starts to its
    right. Tick marks point rightward into the boundary so the eye
    can match the label to a peak height.
    """
    if plot_h <= 0:
        return
    db_range = _DB_AXIS_TOP - _DB_AXIS_BOTTOM
    midline_y = plot_h // 2
    max_half_h = max(1, plot_h // 2 - 1)
    tick_x_outer = DB_AXIS_WIDTH - 1  # rightmost pixel of the tick (at the boundary)
    tick_x_inner = DB_AXIS_WIDTH - 5  # 4-px tick length
    label_x = DB_AXIS_WIDTH - 7       # right-aligned just left of the tick
    # Faint vertical guide at the boundary (separates axis from plot).
    draw.line(
        [(tick_x_outer, 0), (tick_x_outer, plot_h - 1)],
        fill=text_color,
    )
    # Try a small font so 5 dB-spaced labels don't overlap; fall back to
    # the default if load_default(size=) isn't available in this Pillow.
    try:
        font = ImageFont.load_default(size=9)
    except TypeError:
        font = ImageFont.load_default()
    db = _DB_AXIS_TOP
    while db >= _DB_AXIS_BOTTOM - 1e-9:
        # Bar half-height for this dB value. Identical formula to
        # ``_half_h_for_db`` in ``render_waveform_image``: the bar is
        # drawn from ``midline_y - half_h`` down to
        # ``midline_y + half_h``; the upper tick is on the top edge,
        # the lower tick on the bottom edge.
        frac = (_DB_AXIS_TOP - db) / db_range  # 0 at top, 1 at bottom
        half_h = (1 - frac) * max_half_h
        y_top = round(midline_y - half_h)
        y_top = max(0, min(plot_h - 1, y_top))
        draw.line([(tick_x_inner, y_top), (tick_x_outer, y_top)], fill=text_color)
        # Format: "0", "-5", "-10", ... up to "-60".
        label = f"{round(db)}"
        draw.text((label_x, y_top), label, fill=text_color, font=font, anchor="rm")
        if half_h > 0:
            y_bot = round(midline_y + half_h)
            y_bot = max(0, min(plot_h - 1, y_bot))
            draw.line([(tick_x_inner, y_bot), (tick_x_outer, y_bot)], fill=text_color)
            draw.text((label_x, y_bot), label, fill=text_color, font=font, anchor="rm")
        db -= _DB_AXIS_STEP


def _draw_time_axis(
    draw: ImageDraw.ImageDraw,
    x_left: int,
    plot_w: int,
    plot_h: int,
    text_color: tuple[int, int, int],
    total_duration: float | None,
    view_start: float = 0.0,
) -> None:
    """Draw the start / mid / end timestamps below the plot area,
    aligned to the plot region ``[x_left, x_left + plot_w)``.

    Labels are formatted as ``view_start + frac * total_duration`` so
    the time axis stays in sync with the title after pan/zoom. The
    default ``view_start=0.0`` preserves the original 0…duration labels
    for top-level calls.
    """
    y0 = plot_h
    draw.line([(x_left, y0), (x_left + plot_w - 1, y0)], fill=text_color)
    if not total_duration or total_duration <= 0:
        return
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        x = x_left + int(frac * (plot_w - 1))
        label = _format_clock(view_start + frac * total_duration)
        # Right-align end labels, left-align start, center the rest.
        if frac == 0.0:
            tx = x + 2
        elif frac == 1.0:
            tx = max(x_left, x - len(label) * 6 - 2)
        else:
            tx = max(x_left, min(x_left + plot_w - len(label) * 6, x - len(label) * 3))
        draw.text((tx, y0 + 1), label, fill=text_color)
