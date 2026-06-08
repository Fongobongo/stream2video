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


def fmt_time(secs: float) -> str:
    total = int(secs)
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
    ('1.5x'), at 10x or above rounds to int ('15x')."""
    if zoom_level < 10:
        return f"{zoom_level:.1f}x"
    return f"{round(zoom_level)}x"


def fmt_total_label(total_elapsed: float) -> str:
    """Format the Total label — 'Total: X' where X is the wall-clock
    pipeline duration. Pure helper, easy to unit-test."""
    return f"Total: {fmt_time(total_elapsed)}"
