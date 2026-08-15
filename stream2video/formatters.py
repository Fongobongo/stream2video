"""Pure formatting helpers used by the GUI (and its tests).

These are module-level functions instead of staticmethods on
Stream2VideoGUI so the tests can import them without instantiating a
Tk root (the GUI transitively imports Pillow and customtkinter). Pure
functions are also easier to reason about and unit-test.
"""

import math
import re


def fmt_size(bytez: int | float) -> str:
    """Human-readable byte size. Negative values are clamped to 0 — a
    caller that subtracts two file sizes (e.g. "reduction") shouldn't
    be able to print ``-1.0 B`` through here."""
    size = max(0.0, float(bytez))
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} EB"


def fmt_speed(bytes_per_sec: float | None) -> str:
    """Format a download/upload speed as ``"<size>/s"``.

    Returns ``"?"`` for None (yt-dlp reports NA during the initial
    ramp-up before a steady speed estimate stabilises). Reuses
    ``fmt_size`` for the magnitude so the units stay consistent.
    """
    if bytes_per_sec is None or bytes_per_sec < 0:
        return "?"
    return f"{fmt_size(int(bytes_per_sec))}/s"


def fmt_time(secs: float) -> str:
    # divmod() on negative seconds spreads the minus across every unit
    # (fmt_time(-5) used to render "-1d 23h 59m 59s"); clamp negatives and
    # non-finite values to 0 — callers pass elapsed durations.
    total = int(secs)
    if total < 0:
        total = 0
    d, r = divmod(total, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    if d:
        return f"{d}d {h}h {m}m {s}s"
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def fmt_clock_time(secs: float | None) -> str:
    """Format a duration as HH:MM:SS (or D:HH:MM:SS if >= 24h),
    zero-padded. Used in the final summary so '06:04:12 -> 00:34:11'
    is scannable. Returns '?' for None (e.g. ffprobe failed)."""
    if secs is None or secs < 0:
        return "?"
    total = int(secs)
    d, r = divmod(total, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    if d:
        return f"{d}:{h:02d}:{m:02d}:{s:02d}"
    return f"{h:02d}:{m:02d}:{s:02d}"


def fmt_completion_summary(
    src_duration: float | None,
    src_size_bytes: int,
    output_path: str,
    dst_size_bytes: int,
    keep_duration: float,
    pipeline_seconds: float,
) -> str:
    """Format the final success block shown after a pipeline completes.

    Returns a Rich-markup string like::

        [bold green]+ Compression complete![/bold green]
           Input:  6h 23m (2.4 GB)
           Output: 4h 12m (1.1 GB)  [34% reduction, 8.2x realtime]
           Time:   12m 34s

    ``src_duration`` / ``keep_duration`` being None shows ``?`` for the
    time columns; the realtime factor is omitted when duration is unknown
    (e.g. ffprobe failed during a dry-run)."""
    lines = [
        "[bold green]+ Compression complete![/bold green]",
        f"   Input:  {fmt_clock_time(src_duration)} ({fmt_size(src_size_bytes)})"
        if src_duration
        else f"   Input:  ? ({fmt_size(src_size_bytes)})",
    ]

    dst_time_str = fmt_clock_time(keep_duration)
    dst_size_str = fmt_size(dst_size_bytes)

    # Reduction percent: (src - dst) / src * 100. A negative percentage
    # (larger output than source — e.g. an aggressive preset upscaled a
    # small input) used to render as "[-12% reduction]" which reads as a
    # *decrease*; show it as an increase instead so the summary tells
    # the truth either way. Only show when src size is non-zero — a
    # zero-byte source (empty file) would divide by zero.
    if src_size_bytes > 0:
        delta_pct = 100.0 * (src_size_bytes - dst_size_bytes) / src_size_bytes
        if delta_pct >= 0:
            label = f"{delta_pct:.0f}% reduction"
        else:
            label = f"{-delta_pct:.0f}% increase"
        lines.append(f"   Output: {dst_time_str} ({dst_size_str})  [{label}]")
    else:
        lines.append(f"   Output: {dst_time_str} ({dst_size_str})")

    # Realtime factor: source_seconds / pipeline_seconds. >1 means faster
    # than realtime (good). Only compute when both are known and pipeline
    # took measurable time (avoid div-by-zero on a sub-second run).
    if src_duration is not None and src_duration > 0 and pipeline_seconds > 0.1:
        rt_factor = src_duration / pipeline_seconds
        lines.append(f"   Time:   {fmt_time(pipeline_seconds)} ({rt_factor:.1f}x realtime)")
    else:
        lines.append(f"   Time:   {fmt_time(pipeline_seconds)}")

    lines.append(f"Output file: [cyan]{output_path}[/cyan]")
    return "\n".join(lines)


def fmt_dry_run_summary(
    src_duration: float | None,
    src_size_bytes: int,
    silence_segments: list,
    keep_segments: list,
    *,
    markup: bool = True,
) -> str:
    """Format the --dry-run output block.

    Returns a multi-line Rich-markup string (``markup=True``):

        [bold cyan]═══ Dry-run: silence detection only ═══[/bold cyan]
        Source: 6h 23m 12s (2.4 GB)
        Silence segments: 42 (total 1h 23m 45s, 21.9%)
        Keep segments: 15 (total 5h 0m 0s, 78.1%)
        Estimated output: ~2h 5m 30s (assuming 40% bitrate reduction)
        [dim]Use --force to re-run with encode (or remove --dry-run).[/dim]

    ``markup=False`` strips the Rich tags for consumers that render raw
    text (the GUI log panel shows Rich markup literally). The dim
    caveats degrade to plain words: "[dim]? ...[/dim]" →
    "? (duration unknown — ffprobe unavailable)".

    ``src_duration`` None is handled gracefully (shows ``?`` for times).
    ``silence_segments`` / ``keep_segments`` are lists of
    ``SilenceSegment`` / ``(start, end)`` tuples; the caller passes
    whatever ``detect_silence`` and ``generate_keep_segments`` returned.
    """

    def _sum_durations(segs: list) -> float:
        # silence_segments is list[SilenceSegment] (has .start/.end attrs);
        # keep_segments is list[tuple[float, float]]. Handle both by duck-typing.
        total = 0.0
        for s in segs:
            if hasattr(s, "end") and hasattr(s, "start"):
                total += max(0.0, s.end - s.start)
            else:
                # tuple (start, end)
                total += max(0.0, s[1] - s[0])
        return total

    sil_dur = _sum_durations(silence_segments)
    keep_dur = _sum_durations(keep_segments)

    lines = [
        "[bold cyan]═══ Dry-run: silence detection only ═══[/bold cyan]",
    ]

    if src_duration is not None:
        lines.append(f"Source: {fmt_clock_time(src_duration)}")
    else:
        lines.append("Source: [dim]? (duration unknown — ffprobe unavailable)[/dim]")
    lines.append(f"Size: {fmt_size(src_size_bytes)}")

    if silence_segments:
        pct_sil = f"{100.0 * sil_dur / src_duration:.1f}%" if src_duration else "?"
        lines.append(
            f"Silence: {len(silence_segments)} segment{'s' if len(silence_segments) != 1 else ''} "
            f"({fmt_clock_time(sil_dur)}, {pct_sil})"
        )
    else:
        lines.append("Silence: [dim]none detected[/dim]")

    if keep_segments:
        pct_keep = f"{100.0 * keep_dur / src_duration:.1f}%" if src_duration else "?"
        lines.append(
            f"Keep: {len(keep_segments)} segment{'s' if len(keep_segments) != 1 else ''} "
            f"({fmt_clock_time(keep_dur)}, {pct_keep})"
        )
    else:
        lines.append("Keep: [dim]none (would produce empty output)[/dim]")

    # Rough size estimate. Encoding at video_quality=medium (7000k) from a
    # typical 2-4 GB/h stream source usually yields ~35-50% size reduction.
    # We avoid false precision by showing a range and a caveat.
    if src_duration and src_duration > 0 and keep_dur > 0:
        keep_ratio = keep_dur / src_duration
        est_low = src_size_bytes * keep_ratio * 0.4
        est_high = src_size_bytes * keep_ratio * 0.6
        lines.append(
            f"Est. output: {fmt_size(int(est_low))} … {fmt_size(int(est_high))} "
            f"[dim](rough guess; actual depends on content/encoder)[/dim]"
        )

    lines.append("[dim]Use --force to re-run detection; remove --dry-run to encode.[/dim]")
    text = "\n".join(lines)
    if not markup:
        return _strip_rich_markup(text)
    return text


def _strip_rich_markup(text: str) -> str:
    """Strip Rich console tags ([bold], [/dim], [cyan]...) for consumers
    that render raw text. The dry-run summary's only markup is
    ``[bold cyan]…[/bold cyan]`` and ``[dim]…[/dim]``."""
    return re.sub(r"\[/?[a-z][a-z ]*\]", "", text)


def fmt_zoom_text(zoom_level: float) -> str:
    """Format a zoom multiplier (duration / view_duration) for
    the controls and status line. Under 10x uses 1 decimal
    ('1.5x'), at 10x or above rounds to int ('15x').

    The threshold is based on the ROUNDED value, not the raw one —
    ``f"{9.99:.1f}"`` = "10.0" (banker's rounding), so a raw 9.95+
    would render "10.0x" *below* the int branch while 10.0 renders
    "10x" above it (a discontinuity at the boundary). Rounding first
    and comparing the rounded value against 10 moves the switch
    point to where the user actually sees it.
    """
    # Non-finite guard: a NaN zoom (0/0 view math on a
    # degenerate duration) must render as "?" — formatting NaN directly
    # produces "nanx" in the status line, and ``round(nan)`` raises
    # ValueError on some Python builds.
    if not math.isfinite(zoom_level):
        return "?"
    # Round to 1 decimal (the precision of the sub-10 branch) and
    # compare against 10. 9.94 → 9.9 (sub-10, decimal branch).
    # 9.96 → 10.0 (≥10, int branch). The boundary now matches what
    # the rendered labels look like.
    rounded_1dp = round(zoom_level, 1)
    if rounded_1dp < 10:
        return f"{zoom_level:.1f}x"
    return f"{round(zoom_level)}x"
