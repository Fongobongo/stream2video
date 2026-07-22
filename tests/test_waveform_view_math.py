"""Tests for stream2video.waveform_view_math (pure zoom / pan / render
math extracted from gui.py — Этап 10 incremental refactor).

These functions take primitive numeric args and return tuples. No Tk,
no side effects, no I/O — every behavior the GUI relies on for view
navigation is captured here.
"""

from __future__ import annotations

import math

from stream2video.waveform_view_math import (
    FALLBACK_RENDER_SIZE,
    MIN_RENDER_HEIGHT,
    MIN_RENDER_WIDTH,
    MIN_ZOOM_VIEW_DURATION,
    RESERVED_HEIGHT_PX,
    RESERVED_WIDTH_PX,
    compute_pan_view,
    compute_render_size,
    compute_zoom_view,
)


class TestComputeZoomView:
    def test_zero_duration_returns_zero_view(self):
        # A 0-duration source has no zoom semantics; matching gui behaviour.
        assert compute_zoom_view(0.0, 0.0, 0.0, 0.5, True, 0.5) == (0.0, 0.0)

    def test_zoom_in_halves_view_around_cursor(self):
        # 60s source, full view, cursor at right edge (frac=1.0) — zooming
        # in 2x should keep the right edge fixed and the new view should
        # span the last 30s of the timeline.
        new_start, new_end = compute_zoom_view(60.0, 0.0, 60.0, 1.0, True, 0.5)
        assert math.isclose(new_end, 60.0, abs_tol=1e-6)
        assert math.isclose(new_start, 30.0, abs_tol=1e-6)

    def test_zoom_in_around_cursor_left_edge(self):
        # Cursor at left edge — zoom should anchor the left side.
        new_start, new_end = compute_zoom_view(60.0, 0.0, 60.0, 0.0, True, 0.5)
        assert math.isclose(new_start, 0.0, abs_tol=1e-6)
        assert math.isclose(new_end, 30.0, abs_tol=1e-6)

    def test_zoom_in_without_cursor_anchors_on_center(self):
        # Cursor unknown → anchor on the geometric center of the current
        # view (cursor_frac ignored — pass 0.5 for cleanliness).
        new_start, new_end = compute_zoom_view(60.0, 10.0, 50.0, 0.5, False, 0.5)
        # Center = 30.0; the new view spans [30 - 0.5*20, 30 + 0.5*20] = [20, 40].
        assert math.isclose(new_start, 20.0, abs_tol=1e-6)
        assert math.isclose(new_end, 40.0, abs_tol=1e-6)

    def test_zoom_out_doubles_view_clamped_to_duration(self):
        # Already a 30s window in a 60s source — zooming out 2x should
        # produce a 60s window (clamped at the source duration).
        new_start, new_end = compute_zoom_view(60.0, 15.0, 45.0, 0.5, True, 2.0)
        assert math.isclose(new_end - new_start, 60.0, abs_tol=1e-6)

    def test_zoom_in_at_minimum_clamps_to_min_duration(self):
        # Repeatedly zooming in should never collapse below MIN_ZOOM_VIEW_DURATION.
        new_start, new_end = compute_zoom_view(120.0, 50.0, 50.5, 0.5, True, 0.001)
        assert math.isclose(new_end - new_start, MIN_ZOOM_VIEW_DURATION, abs_tol=1e-6)

    def test_factor_one_returns_same_view_identity(self):
        # Factor that wouldn't change the duration → identity return.
        result = compute_zoom_view(60.0, 10.0, 40.0, 0.5, True, 1.0)
        assert result == (10.0, 40.0)

    def test_new_start_clamped_to_zero_when_anchor_near_left(self):
        # Cursor at the right edge of a small view near the left edge of
        # the source — zooming out (factor > 1) wants the new view's
        # left edge to go negative; the clamp pins it to 0.0. Without
        # the clamp this would lose a chunk of the timeline on the left.
        # view_duration = 2; factor = 2 → new_duration = 4; anchor at
        # cursor_frac=1.0 is view_start + 1.0 * 2 = 3.0; new_start before
        # clamp = 3.0 - 1.0 * 4 = -1.0; clamped to 0.0.
        new_start, new_end = compute_zoom_view(60.0, 1.0, 3.0, 1.0, True, 2.0)
        assert math.isclose(new_start, 0.0, abs_tol=1e-6)
        assert math.isclose(new_end, 4.0, abs_tol=1e-6)

    def test_new_start_clamped_when_anchor_near_right(self):
        # Cursor at right edge, view near end of source — new_start
        # clamps to ``duration - new_duration`` so the view doesn't
        # overshoot the right edge.
        new_start, new_end = compute_zoom_view(60.0, 55.0, 60.0, 1.0, True, 0.5)
        # New duration = 2.5; new_start clamps to 60 - 2.5 = 57.5.
        assert math.isclose(new_start, 57.5, abs_tol=1e-6)
        assert math.isclose(new_end, 60.0, abs_tol=1e-6)


class TestComputePanView:
    def test_zero_duration_returns_zero_view(self):
        assert compute_pan_view(0.0, 0.0, 0.0, 0.25) == (0.0, 0.0)

    def test_full_timeline_pan_is_identity(self):
        # When the view already covers the whole timeline there is no
        # room to pan — the helper returns the same view unchanged.
        assert compute_pan_view(60.0, 0.0, 60.0, 0.25) == (0.0, 60.0)

    def test_pan_right_by_quarter_view(self):
        # 30s window in a 60s source; pan right by 0.25 of the view
        # (=7.5s) starting from [10, 40].
        new_start, new_end = compute_pan_view(60.0, 10.0, 40.0, 0.25)
        assert math.isclose(new_start, 17.5, abs_tol=1e-6)
        assert math.isclose(new_end, 47.5, abs_tol=1e-6)

    def test_pan_left_by_quarter_view_clamps_to_zero(self):
        # Pan left from a view already at the left edge — clamps to 0.0
        # and returns a view starting at 0.
        new_start, new_end = compute_pan_view(60.0, 1.0, 31.0, -0.5)
        assert math.isclose(new_start, 0.0, abs_tol=1e-6)
        assert math.isclose(new_end, 30.0, abs_tol=1e-6)

    def test_pan_right_clamps_to_duration_minus_view(self):
        # Pan right that would overshoot the right edge — clamps so the
        # view's right edge is at the source duration.
        new_start, new_end = compute_pan_view(60.0, 50.0, 55.0, 5.0)
        assert math.isclose(new_end, 60.0, abs_tol=1e-6)
        assert math.isclose(new_start, 55.0, abs_tol=1e-6)

    def test_negative_fraction_pans_left(self):
        new_start, _ = compute_pan_view(60.0, 30.0, 50.0, -0.5)
        # View duration = 20; shift = -10; new_start = 30 + (-10) = 20.
        assert math.isclose(new_start, 20.0, abs_tol=1e-6)


class TestComputeRenderSize:
    def test_none_window_returns_fallback(self):
        assert compute_render_size(None, None) == FALLBACK_RENDER_SIZE

    def test_tiny_window_returns_fallback(self):
        # Below MIN_REAL_WINDOW_PX the popup is considered not laid out
        # yet (typical during first render).
        assert compute_render_size(50, 50) == FALLBACK_RENDER_SIZE

    def test_normal_window_subtracts_reserved_regions(self):
        win_w, win_h = 900, 380
        expect_w = max(MIN_RENDER_WIDTH, win_w - RESERVED_WIDTH_PX)
        expect_h = max(MIN_RENDER_HEIGHT, win_h - RESERVED_HEIGHT_PX)
        assert compute_render_size(win_w, win_h) == (expect_w, expect_h)

    def test_just_above_minimum_real_window_uses_actual_size(self):
        # Barely-real window still subtracts the reserved regions.
        win_w, win_h = 101, 101
        expect_w = max(MIN_RENDER_WIDTH, win_w - RESERVED_WIDTH_PX)
        expect_h = max(MIN_RENDER_HEIGHT, win_h - RESERVED_HEIGHT_PX)
        assert compute_render_size(win_w, win_h) == (expect_w, expect_h)

    def test_small_window_clamps_render_to_minimum(self):
        # A valid (>=100px) window but small enough that without the
        # clamp would underflow — the function still returns at least
        # MIN_RENDER_WIDTH x MIN_RENDER_HEIGHT.
        result = compute_render_size(110, 110)
        assert result[0] >= MIN_RENDER_WIDTH
        assert result[1] >= MIN_RENDER_HEIGHT

    def test_large_window_grows_with_window(self):
        # A larger window should give a larger render (the reserved
        # region is fixed, not a fraction).
        w1, h1 = compute_render_size(900, 380)
        w2, h2 = compute_render_size(1200, 600)
        assert w2 > w1
        assert h2 > h1
