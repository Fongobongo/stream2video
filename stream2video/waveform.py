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

from PIL import Image, ImageDraw

from stream2video.silence import SilenceSegment
from stream2video.utils import no_window_kwargs

# Canvas sizing constants. Exposed as module-level so tests can pin them.
_DEFAULT_WIDTH = 800
_DEFAULT_HEIGHT = 200
_AXIS_HEIGHT = 16
_SILENCE_FILL = (220, 50, 47, 70)  # red, ~28% alpha
_SILENCE_EDGE = (220, 50, 47, 220)  # solid red for outline


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
) -> list[tuple[int, int]]:
    """Map silence ``(start, end)`` ranges to ``(x_left, x_right)`` pixels.

    Out-of-range segments are clamped; zero/negative-width segments
    after clamping are dropped. ``total_duration`` <= 0 returns [].
    """
    if total_duration <= 0 or plot_width <= 0:
        return []
    result: list[tuple[int, int]] = []
    for seg in segments:
        if seg.end <= 0 or seg.start >= total_duration:
            continue
        start = max(0.0, seg.start)
        end = min(total_duration, seg.end)
        if end <= start:
            continue
        x_left = int(start / total_duration * plot_width)
        x_right = int(end / total_duration * plot_width)
        # Ensure the right edge advances at least 1px past the left so
        # sub-pixel-wide silences still render as a thin line.
        if x_right == x_left:
            x_right = min(plot_width, x_left + 1)
        x_left = max(0, min(plot_width, x_left))
        x_right = max(0, min(plot_width, x_right))
        result.append((x_left, x_right))
    return result


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
) -> Image.Image:
    """Render a waveform image with optional silence overlay.

    Returns an ``Image`` in mode ``"RGB"`` (or ``"RGBA"`` if overlays are
    drawn). ``peaks`` is one amplitude value per horizontal pixel-bucket;
    the waveform is drawn symmetrically around the vertical midline.

    ``width`` and ``height`` set the canvas size. ``bg_color`` /
    ``wave_color`` / ``midline_color`` / ``text_color`` are RGB tuples.
    ``silence_fill`` and ``silence_edge`` are RGBA tuples — alpha is
    honoured for the fill (semi-transparent overlay) and ignored for
    the edge (solid 1-px outline).

    ``total_duration`` and ``silence_segments`` are both required to
    render the overlay; if either is None the silence layer is skipped.
    ``title`` is drawn at the top-left, truncated to canvas width.
    """
    if width <= 0 or height <= 0:
        raise ValueError(f"width and height must be positive, got {width}x{height}")

    plot_h = max(1, height - _AXIS_HEIGHT)
    midline_y = plot_h // 2

    img = Image.new("RGB", (width, height), bg_color)
    draw = ImageDraw.Draw(img)

    if not peaks:
        # Empty waveform — still draw the axis below, and an 'empty' hint.
        if title:
            draw.text((4, 2), title, fill=text_color)
        draw.text((4, midline_y - 6), "(no audio)", fill=text_color)
        _draw_time_axis(draw, width, plot_h, height, text_color, total_duration)
        return img

    # Pre-compute which x-pixels are silenced, so the waveform bars
    # under a silence are drawn in a slightly different (dimmer) color.
    silenced = bytearray(width)
    if total_duration and total_duration > 0 and silence_segments:
        for x_left, x_right in silence_pixel_ranges(silence_segments, total_duration, width):
            for x in range(x_left, x_right):
                if 0 <= x < width:
                    silenced[x] = 1

    # Map N peaks into W pixels. If peaks > W, take max-pool; if peaks
    # < W, stretch linearly.
    n_peaks = len(peaks)
    bar_color_silenced: tuple[int, int, int] = tuple(  # type: ignore[assignment]
        max(0, c - 60) for c in wave_color
    )

    def _bar_color(px: int) -> tuple[int, int, int]:
        return bar_color_silenced if bool(silenced[px]) else wave_color

    for px in range(width):
        if n_peaks == width:
            p = peaks[px]
        elif n_peaks > width:
            # Downsample — take max over the slice.
            start = px * n_peaks // width
            end = max(start + 1, (px + 1) * n_peaks // width)
            p = max(peaks[start:end])
        else:
            # Upsample — nearest.
            p = peaks[px * n_peaks // width]
        # Skip near-silent buckets to avoid a stripe of nothing.
        if p < 1e-4:
            continue
        h = max(1, int(p * (plot_h // 2 - 1)))
        color = _bar_color(px)
        draw.line([(px, midline_y - h), (px, midline_y + h)], fill=color)

    # Midline
    draw.line([(0, midline_y), (width, midline_y)], fill=midline_color)

    # Silence overlay on top of the waveform.
    if total_duration and total_duration > 0 and silence_segments:
        overlay = Image.new("RGBA", (width, plot_h), (0, 0, 0, 0))
        ov_draw = ImageDraw.Draw(overlay)
        for x_left, x_right in silence_pixel_ranges(silence_segments, total_duration, width):
            ov_draw.rectangle(
                [(x_left, 0), (x_right - 1, plot_h - 1)],
                fill=silence_fill,
                outline=silence_edge[:3],
            )
        img.paste(overlay, (0, 0), overlay)

    if title:
        draw.text((4, 2), title, fill=text_color)

    _draw_time_axis(draw, width, plot_h, height, text_color, total_duration)
    return img


def _draw_time_axis(
    draw: ImageDraw.ImageDraw,
    width: int,
    plot_h: int,
    total_h: int,
    text_color: tuple[int, int, int],
    total_duration: float | None,
) -> None:
    """Draw the start / mid / end timestamps below the plot area."""
    y0 = plot_h
    draw.line([(0, y0), (width, y0)], fill=text_color)
    if not total_duration or total_duration <= 0:
        return
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        x = int(frac * (width - 1))
        label = _format_clock(frac * total_duration)
        # Right-align end labels, left-align start, center the rest.
        if frac == 0.0:
            tx = x + 2
        elif frac == 1.0:
            tx = max(0, x - len(label) * 6 - 2)
        else:
            tx = max(0, min(width - len(label) * 6, x - len(label) * 3))
        draw.text((tx, y0 + 1), label, fill=text_color)
