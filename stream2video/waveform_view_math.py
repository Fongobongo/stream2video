"""Pure view math for the waveform popup — extracted from ``gui.py``
(Этап 10 incremental refactor).

These functions take primitive args (no ``self``, no Tk widgets) so they
can be unit-tested without the GUI main loop. The GUI class delegates
the zoom/pan/render-size math here; the GUI side keeps the widget refs
and the bookkeeping that needs them.

Currently exported:
  * ``compute_zoom_view`` — zoom by a multiplicative factor anchored on
    a cursor fraction (or view center if the cursor is unknown). Clamps
    the new view to ``[0, duration]`` and the new view duration to
    ``[0.5, duration]``. Identity if the factor wouldn't move anything.
  * ``compute_pan_view`` — shift the view by a fraction of its own
    duration (positive = right, negative = left). Clamps to ``[0, duration]``.
    Identity if the current view already covers the full timeline.
  * ``compute_render_size`` — image width/height to use for the next
    render, given a popup window size. Padding/reservation constants are
    exposed as module-level so tests can pin them. Falls back to
    ``(800, 200)`` when the window has not been laid out yet (size < 100
    or ``None``).
"""

from __future__ import annotations

# Minimum new view duration after a zoom. Prevents the zoom-in path from
# collapsing the view to a single instant (and the slider math from
# dividing by zero in the GUI).
MIN_ZOOM_VIEW_DURATION = 0.5

# Vertical reservation inside the waveform popup: status row (~30 px)
# + zoom/pan controls (~30 px) + inter-row spacing + image pady (2 top +
# 8 bottom) ≈ 80 px. Kept here so tests can pin the math.
RESERVED_HEIGHT_PX = 80

# Horizontal reservation inside the waveform popup: padx 8 each side.
RESERVED_WIDTH_PX = 16

# Fallback render size when the popup has not been laid out yet (first
# render immediately after the popup is created, or ``<Configure>`` has
# not fired). Large enough to look like a real waveform preview; small
# enough to fit on a 720p screen with the controls visible.
FALLBACK_RENDER_SIZE: tuple[int, int] = (800, 200)

# Minimum win width/height to treat as "real" for render sizing. Below
# this the popup is assumed to be unmapped / just constructed and the
# fallback size is used instead (avoids rendering a 1x1 image while the
# layout pass is still pending).
MIN_REAL_WINDOW_PX = 100

# Minimum clamped render dimensions — even if the window shrinks we keep
# at least this many pixels so the image doesn't collapse to nothing.
MIN_RENDER_WIDTH = 200
MIN_RENDER_HEIGHT = 80


def cursor_plot_frac(
    event_x: int,
    image_width: int,
    axis_width: int,
) -> float | None:
    """Map a mouse ``event.x`` to a fraction along the *plot* area.

    Returns ``None`` when the cursor is outside the plot (over the dB
    axis strip or past the right edge of the rendered image — CTkLabel
    centers its image, so the label can be wider than the bitmap and
    ``event.x`` can exceed ``image_width``).

    Every cursor→time consumer (hover fraction, tooltip, drag pan) must
    use this same mapping so zoom anchors and readouts agree.
    """
    plot_w = image_width - axis_width
    if plot_w <= 0:
        return None
    plot_x = event_x - axis_width
    if plot_x < 0 or plot_x >= plot_w:
        return None
    return plot_x / plot_w


def compute_zoom_view(
    duration: float,
    view_start: float,
    view_end: float,
    cursor_frac: float,
    cursor_known: bool,
    factor: float,
) -> tuple[float, float]:
    """Pure zoom math: zoom by ``factor`` (< 1 in, > 1 out) anchored on
    the cursor or view center. Returns ``(new_start, new_end)`` clamped
    to ``[0, duration]`` with ``new_duration`` in
    ``[MIN_ZOOM_VIEW_DURATION, duration]``. Identity (no change) if the
    requested factor would not change the duration.

    ``cursor_frac`` is the cursor position as a fraction of the current
    view's pixel width (0.0 = left edge, 1.0 = right edge). When
    ``cursor_known`` is False, the zoom anchors on the view center
    (cursor_frac is ignored — pass 0.5 for cleanliness).
    """
    if duration <= 0:
        return (0.0, 0.0)
    view_duration = view_end - view_start
    new_duration = view_duration * factor
    new_duration = max(MIN_ZOOM_VIEW_DURATION, min(duration, new_duration))
    if new_duration == view_duration:
        return (view_start, view_end)
    if cursor_known:
        anchor = view_start + cursor_frac * view_duration
    else:
        anchor = (view_start + view_end) / 2.0
        # When anchoring on the center, use 0.5 so the new view is
        # centered on the anchor rather than shifted by the stale
        # cursor_frac (which may be from an old mousemove and would
        # produce an asymmetric zoom).
        cursor_frac = 0.5
    new_start = anchor - cursor_frac * new_duration
    new_start = max(0.0, min(duration - new_duration, new_start))
    return (new_start, new_start + new_duration)


def compute_pan_view(
    duration: float,
    view_start: float,
    view_end: float,
    frac: float,
) -> tuple[float, float]:
    """Pure pan math: shift view by ``frac * view_duration``
    (positive = right, negative = left). Returns ``(new_start,
    new_end)`` clamped to ``[0, duration]``. Identity if the current
    view is the full timeline (no room to pan)."""
    if duration <= 0:
        return (0.0, 0.0)
    view_duration = view_end - view_start
    if view_duration >= duration:
        return (view_start, view_end)
    shift = view_duration * frac
    new_start = view_start + shift
    new_start = max(0.0, min(duration - view_duration, new_start))
    return (new_start, new_start + view_duration)


def compute_render_size(win_w: int | None, win_h: int | None) -> tuple[int, int]:
    """Image size for the next render given the popup window's pixel
    size. Returns ``FALLBACK_RENDER_SIZE`` if the window has not been
    laid out yet (``None`` or < ``MIN_REAL_WINDOW_PX`` — typical for
    the very first render before ``<Configure>`` has fired).

    Pure: takes the window size, returns the image size. The GUI passes
    ``self._wave_window.winfo_width()`` / ``winfo_height()`` (or None if
    the popup is closed); the function subtracts the reserved regions and
    clamps to the minimum render dimensions.
    """
    if win_w is None or win_h is None or win_w < MIN_REAL_WINDOW_PX or win_h < MIN_REAL_WINDOW_PX:
        return FALLBACK_RENDER_SIZE
    w = max(MIN_RENDER_WIDTH, win_w - RESERVED_WIDTH_PX)
    h = max(MIN_RENDER_HEIGHT, win_h - RESERVED_HEIGHT_PX)
    return (w, h)
