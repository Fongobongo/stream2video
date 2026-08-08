"""Pure formatting helpers used by the GUI (and its tests).

These are module-level functions instead of staticmethods on
Stream2VideoGUI so the tests can import them without instantiating a
Tk root (the GUI transitively imports Pillow and customtkinter). Pure
functions are also easier to reason about and unit-test.
"""


def fmt_size(bytez: int) -> str:
    size = float(bytez)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


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
    # Round to 1 decimal (the precision of the sub-10 branch) and
    # compare against 10. 9.94 → 9.9 (sub-10, decimal branch).
    # 9.96 → 10.0 (≥10, int branch). The boundary now matches what
    # the rendered labels look like.
    rounded_1dp = round(zoom_level, 1)
    if rounded_1dp < 10:
        return f"{zoom_level:.1f}x"
    return f"{round(zoom_level)}x"
