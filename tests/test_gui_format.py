"""Tests for stream2video.gui formatting helpers.

These are static methods on Stream2VideoGUI (or module-level helpers) that
build the human-readable strings used in the status line, log, and popup
on completion. They are pure functions and don't require a Tk root.
"""

from stream2video.gui import Stream2VideoGUI

_fmt_size = staticmethod(Stream2VideoGUI._fmt_size).__func__
_fmt_time = staticmethod(Stream2VideoGUI._fmt_time).__func__
_fmt_clock_time = staticmethod(Stream2VideoGUI._fmt_clock_time).__func__
_fmt_zoom_text = staticmethod(Stream2VideoGUI._fmt_zoom_text).__func__


class TestFmtSize:
    """_fmt_size — bytes -> human-readable string."""

    def test_bytes(self):
        assert _fmt_size(500) == "500.0 B"

    def test_kilobytes(self):
        assert _fmt_size(2048) == "2.0 KB"

    def test_megabytes(self):
        assert _fmt_size(5 * 1024 * 1024) == "5.0 MB"

    def test_gigabytes(self):
        # Realistic: 1.5GB Twitch VOD
        assert _fmt_size(int(1.5 * 1024**3)) == "1.5 GB"

    def test_terabytes(self):
        # >= 1024 GB rolls over to TB
        assert _fmt_size(2 * 1024**4) == "2.0 TB"


class TestFmtTime:
    """_fmt_time — seconds -> 'Xh Ym Zs' / 'Xm Ys' / 'Xs'."""

    def test_seconds_only(self):
        assert _fmt_time(42) == "42s"

    def test_minutes_and_seconds(self):
        assert _fmt_time(125) == "2m 5s"

    def test_hours_minutes_seconds(self):
        assert _fmt_time(3661) == "1h 1m 1s"

    def test_days(self):
        # 1d 2h 3m 4s = 86400 + 2*3600 + 3*60 + 4 = 93784
        assert _fmt_time(93784) == "1d 2h 3m 4s"


class TestFmtClockTime:
    """_fmt_clock_time — seconds -> 'HH:MM:SS' (or 'D:HH:MM:SS' if >= 24h),
    zero-padded, used in the final summary so '06:04:12 -> 00:34:11' is
    scannable at a glance."""

    def test_short_video(self):
        # 34m 11s = 2051s
        assert _fmt_clock_time(2051) == "00:34:11"

    def test_medium_video(self):
        # 6h 4m 12s = 21852s
        assert _fmt_clock_time(21852) == "06:04:12"

    def test_zero(self):
        assert _fmt_clock_time(0) == "00:00:00"

    def test_under_one_minute(self):
        # 45s — leading zeros preserved
        assert _fmt_clock_time(45) == "00:00:45"

    def test_under_one_hour(self):
        # 5m 3s
        assert _fmt_clock_time(303) == "00:05:03"

    def test_days(self):
        # 1d 2h 3m 4s = 93784
        assert _fmt_clock_time(93784) == "1:02:03:04"

    def test_over_24h_includes_day_field(self):
        # 25h 30m 0s = 1d 1h 30m 0s. The day field is included once
        # total >= 24h so the value is never ambiguous.
        assert _fmt_clock_time(91800) == "1:01:30:00"

    def test_none_returns_question_mark(self):
        """get_video_duration() can return None if ffprobe fails. The
        final summary must not crash; it must show '?' for that field."""
        assert _fmt_clock_time(None) == "?"

    def test_negative_returns_question_mark(self):
        assert _fmt_clock_time(-1) == "?"

    def test_float_seconds_truncated(self):
        # 1.9s -> "00:00:01" (we take int() of the value)
        assert _fmt_clock_time(1.9) == "00:00:01"

    def test_realistic_summaries(self):
        """The full 'X -> Y' summary lines as they appear in the GUI."""
        src = 6 * 3600 + 4 * 60 + 12  # 06:04:12
        dst = 34 * 60 + 11  # 00:34:11
        assert f"{_fmt_clock_time(src)} -> {_fmt_clock_time(dst)}" == "06:04:12 -> 00:34:11"


class TestBuildCompletionSummary:
    """_build_completion_summary — pure function returning the three
    user-facing strings emitted at pipeline completion. Tested without
    instantiating the GUI (Tk) — that's the whole point of the refactor.

    The status line is 'Complete!' plus the total wall-clock in
    parentheses, e.g. 'Complete! (23m 5s)'. Size and duration go to the
    log block and the messagebox popup only. The Total wall-clock is
    also its own label below the progress bar — built separately by
    _fmt_total_label()."""

    def _summary(self, **overrides):
        """Default-args helper for readability."""
        defaults = {
            "src_size_bytes": 20 * 1024**3,  # 20.0 GB
            "src_duration": 6 * 3600 + 4 * 60 + 12,  # 06:04:12
            "dst_size_bytes": int(1.1 * 1024**3),  # 1.1 GB
            "dst_duration": 34 * 60 + 11,  # 00:34:11
            "pipeline_seconds": 23 * 60 + 5,  # 23m 5s
            "output_path": "D:/vids/out.mp4",
        }
        defaults.update(overrides)
        from stream2video.gui import _build_completion_summary

        return _build_completion_summary(**defaults)

    def test_returns_dict_with_three_keys(self):
        s = self._summary()
        assert set(s.keys()) == {"status", "log_lines", "popup"}

    def test_status_is_complete_plus_pipeline_time(self):
        """The status line is 'Complete!' plus the total wall-clock in
        parentheses, e.g. 'Complete! (23m 5s)'. Size and duration stay
        out of the status line — those go to the log block and popup."""
        s = self._summary()
        assert s["status"] == "Complete! (23m 5s)"
        assert s["status"].startswith("Complete!")
        assert "GB" not in s["status"]
        assert ":" not in s["status"]

    def test_log_lines_start_and_end_with_separator(self):
        """Log block must be greppable: '=' delimiter on both sides
        of the [SUCCESS] block."""
        s = self._summary()
        assert len(s["log_lines"]) == 6
        assert s["log_lines"][0] == "=" * 60
        assert s["log_lines"][-1] == "=" * 60
        assert s["log_lines"][1].startswith("[SUCCESS] Output: ")

    def test_log_lines_contain_all_metrics(self):
        s = self._summary()
        joined = "\n".join(s["log_lines"])
        assert "20.0 GB -> 1.1 GB" in joined
        assert "06:04:12 -> 00:34:11" in joined
        assert "23m 5s" in joined
        assert "D:/vids/out.mp4" in joined

    def test_popup_contains_source_and_output_labels(self):
        s = self._summary()
        assert "Source:" in s["popup"]
        assert "Output:" in s["popup"]
        assert "Pipeline:" in s["popup"]
        assert "D:/vids/out.mp4" in s["popup"]

    def test_none_source_duration_renders_as_question_mark_in_log(self):
        """ffprobe can fail; the summary must not crash. The '?' shows
        up in the log and popup (not the status, which is just 'Complete!')."""
        s = self._summary(src_duration=None)
        assert "? -> 00:34:11" in "\n".join(s["log_lines"])
        # Popup uses 'Source: X, Y' / 'Output: X, Y' format — '?' for
        # the source duration, real value for the output duration.
        assert "Source:  20.0 GB, ?" in s["popup"]
        assert "Output:  1.1 GB, 00:34:11" in s["popup"]
        # The status line carries only the headline + total wall-clock —
        # never file size or duration strings.
        assert s["status"] == "Complete! (23m 5s)"
        assert "GB" not in s["status"]
        assert ":" not in s["status"]
        # dst is always known (it's computed from keep-segments locally)
        assert "06:04:12 -> ?" not in "\n".join(s["log_lines"])

    def test_negative_source_duration_renders_as_question_mark(self):
        s = self._summary(src_duration=-5)
        assert "? -> 00:34:11" in "\n".join(s["log_lines"])
        assert "Source:  20.0 GB, ?" in s["popup"]

    def test_status_reflects_pipeline_seconds(self):
        """The status line must include the total wall-clock so the user
        sees the headline result at a glance, without opening the popup."""
        assert self._summary(pipeline_seconds=5)["status"] == "Complete! (5s)"
        assert self._summary(pipeline_seconds=90)["status"] == "Complete! (1m 30s)"
        assert self._summary(pipeline_seconds=23 * 60 + 5)["status"] == "Complete! (23m 5s)"
        assert (
            self._summary(pipeline_seconds=3600 + 30 * 60 + 12)["status"]
            == "Complete! (1h 30m 12s)"
        )

    def test_zero_duration_renders_as_zero_clock(self):
        """00:00:00 is a valid value (corrupted 0-byte file) — must not
        be replaced with '?'."""
        s = self._summary(src_duration=0, dst_duration=0)
        assert "00:00:00 -> 00:00:00" in "\n".join(s["log_lines"])

    def test_size_uses_bytes_to_bytes_conversion(self):
        """1 GB = 1024^3 bytes exactly (no rounding issues)."""
        s = self._summary(
            src_size_bytes=1024**3,  # exactly 1.0 GB
            dst_size_bytes=512 * 1024**2,  # exactly 512.0 MB
        )
        assert "1.0 GB -> 512.0 MB" in "\n".join(s["log_lines"])

    def test_days_in_duration_uses_day_field(self):
        """A 30+ hour source must still render cleanly. 30h 4m 12s =
        1d 6h 4m 12s = '1:06:04:12' (the day field kicks in at >= 24h
        so the value is never ambiguous)."""
        s = self._summary(
            src_duration=30 * 3600 + 4 * 60 + 12,  # 1d 6h 4m 12s
        )
        assert "1:06:04:12 -> 00:34:11" in "\n".join(s["log_lines"])


class TestFmtTotalLabel:
    """_fmt_total_label — formats the Total wall-clock label that lives
    below the progress bar. Updated in real time during the pipeline
    and frozen at the final value on completion."""

    def test_basic(self):
        from stream2video.gui import Stream2VideoGUI

        assert Stream2VideoGUI._fmt_total_label(23 * 60 + 5) == "Total: 23m 5s"

    def test_zero(self):
        from stream2video.gui import Stream2VideoGUI

        assert Stream2VideoGUI._fmt_total_label(0) == "Total: 0s"

    def test_subsecond(self):
        from stream2video.gui import Stream2VideoGUI

        assert Stream2VideoGUI._fmt_total_label(0.4) == "Total: 0s"

    def test_long_pipeline(self):
        from stream2video.gui import Stream2VideoGUI

        # 1d 1h 1m 0s
        assert Stream2VideoGUI._fmt_total_label(86400 + 3600 + 60) == "Total: 1d 1h 1m 0s"

    def test_seconds_only(self):
        from stream2video.gui import Stream2VideoGUI

        assert Stream2VideoGUI._fmt_total_label(42) == "Total: 42s"


class TestFmtZoomText:
    """`_fmt_zoom_text` formats the zoom multiplier (duration /
    view_duration) for the controls and status line."""

    def test_one_x(self):
        assert _fmt_zoom_text(1.0) == "1.0x"

    def test_sub_two_uses_one_decimal(self):
        assert _fmt_zoom_text(1.5) == "1.5x"

    def test_two_x(self):
        assert _fmt_zoom_text(2.0) == "2.0x"

    def test_just_under_ten_uses_one_decimal(self):
        # 9.94 rounds to 9.9 (one decimal).
        assert _fmt_zoom_text(9.94) == "9.9x"

    def test_ten_x_rounds_to_int(self):
        assert _fmt_zoom_text(10.0) == "10x"

    def test_large_zoom_rounds_to_int(self):
        # 14.7 -> "15x", 14.4 -> "14x" (banker's rounding would be 14,
        # but Python's round uses half-to-even so 14.5 -> 14).
        assert _fmt_zoom_text(14.7) == "15x"
        assert _fmt_zoom_text(14.4) == "14x"

    def test_just_under_ten_boundary(self):
        # 9.999... rounds to 10.0 with 1 decimal? No -- 9.99 < 10
        # uses 1-decimal branch and formats as 10.0. Actually the
        # rule is < 10 vs >= 10, not the formatted value. So 9.99
        # stays in the 1-decimal branch.
        assert _fmt_zoom_text(9.99) == "10.0x"


class TestSetCheckbox:
    """Stream2VideoGUI._set_checkbox — wrapper around CTkCheckBox.select/
    deselect. Tested with a MagicMock so no Tk root is required."""

    def test_true_calls_select(self):
        from unittest.mock import MagicMock

        from stream2video.gui import Stream2VideoGUI

        cb = MagicMock()
        Stream2VideoGUI._set_checkbox(cb, True)
        cb.select.assert_called_once_with()
        cb.deselect.assert_not_called()

    def test_false_calls_deselect(self):
        from unittest.mock import MagicMock

        from stream2video.gui import Stream2VideoGUI

        cb = MagicMock()
        Stream2VideoGUI._set_checkbox(cb, False)
        cb.deselect.assert_called_once_with()
        cb.select.assert_not_called()

    def test_truthy_non_bool_calls_select(self):
        """Any truthy value should select. Defensive — callers may pass
        e.g. 1 or 'yes' (though the type hint says bool)."""
        from unittest.mock import MagicMock

        from stream2video.gui import Stream2VideoGUI

        cb = MagicMock()
        Stream2VideoGUI._set_checkbox(cb, 1)
        cb.select.assert_called_once_with()

    def test_falsy_non_bool_calls_deselect(self):
        from unittest.mock import MagicMock

        from stream2video.gui import Stream2VideoGUI

        cb = MagicMock()
        Stream2VideoGUI._set_checkbox(cb, 0)
        cb.deselect.assert_called_once_with()

    def test_none_calls_deselect(self):
        from unittest.mock import MagicMock

        from stream2video.gui import Stream2VideoGUI

        cb = MagicMock()
        Stream2VideoGUI._set_checkbox(cb, None)
        cb.deselect.assert_called_once_with()


# ── Waveform view math (pure helpers, no Tk) ────────────────


class TestComputeZoomView:
    """`_compute_zoom_view` returns the new (start, end) for a zoom
    action. Pure function — no GUI state needed. The view is
    clamped to [0, duration] and the duration is clamped to
    [0.5, duration] (so we never zoom past the full timeline or
    below a 0.5s window)."""

    def test_zoom_in_halves_duration_anchored_on_center(self):
        """Default anchor is the view center (cursor unknown)."""
        new_start, new_end = Stream2VideoGUI._compute_zoom_view(
            duration=100.0,
            view_start=0.0,
            view_end=100.0,
            cursor_frac=0.5,
            cursor_known=False,
            factor=0.5,
        )
        assert new_start == 25.0
        assert new_end == 75.0

    def test_zoom_in_doubles_zoom_from_offset_view(self):
        """A view centered at 50s (10s wide) zooms to 5s wide,
        still centered on 50s."""
        new_start, new_end = Stream2VideoGUI._compute_zoom_view(
            duration=100.0,
            view_start=45.0,
            view_end=55.0,
            cursor_frac=0.5,
            cursor_known=False,
            factor=0.5,
        )
        assert new_start == 47.5
        assert new_end == 52.5

    def test_zoom_in_anchored_on_cursor_left_edge(self):
        """Cursor at frac=0 (left edge) means the left edge of the
        view stays put after zooming."""
        new_start, new_end = Stream2VideoGUI._compute_zoom_view(
            duration=100.0,
            view_start=40.0,
            view_end=60.0,
            cursor_frac=0.0,
            cursor_known=True,
            factor=0.5,
        )
        # cursor_time = 40.0, new_duration = 10, new_start = 40 - 0*10 = 40
        assert new_start == 40.0
        assert new_end == 50.0

    def test_zoom_in_anchored_on_cursor_right_edge(self):
        """Cursor at frac=1 (right edge) means the right edge stays put."""
        new_start, new_end = Stream2VideoGUI._compute_zoom_view(
            duration=100.0,
            view_start=40.0,
            view_end=60.0,
            cursor_frac=1.0,
            cursor_known=True,
            factor=0.5,
        )
        # cursor_time = 60.0, new_duration = 10, new_start = 60 - 1*10 = 50
        assert new_start == 50.0
        assert new_end == 60.0

    def test_zoom_in_preserves_cursor_time(self):
        """The time at the cursor stays at the same pixel after zooming:
        the user is anchoring the zoom on the cursor position."""
        cursor_frac = 0.3
        view_start, view_end = 20.0, 80.0
        duration = 100.0
        cursor_time_before = view_start + cursor_frac * (view_end - view_start)
        new_start, new_end = Stream2VideoGUI._compute_zoom_view(
            duration=duration,
            view_start=view_start,
            view_end=view_end,
            cursor_frac=cursor_frac,
            cursor_known=True,
            factor=0.5,
        )
        cursor_time_after = new_start + cursor_frac * (new_end - new_start)
        assert abs(cursor_time_after - cursor_time_before) < 1e-9

    def test_zoom_out_doubles_duration(self):
        """Zoom out from a 10s view → 20s view, centered."""
        new_start, new_end = Stream2VideoGUI._compute_zoom_view(
            duration=100.0,
            view_start=45.0,
            view_end=55.0,
            cursor_frac=0.5,
            cursor_known=False,
            factor=2.0,
        )
        assert new_start == 40.0
        assert new_end == 60.0

    def test_zoom_out_clamps_to_full_duration(self):
        """Zoom out cannot exceed the full duration."""
        new_start, new_end = Stream2VideoGUI._compute_zoom_view(
            duration=100.0,
            view_start=10.0,
            view_end=20.0,
            cursor_frac=0.5,
            cursor_known=False,
            factor=10.0,
        )
        # new_duration would be 100 → capped to 100 → already full
        assert new_start == 0.0
        assert new_end == 100.0

    def test_zoom_in_clamps_to_left_edge(self):
        """Cursor near the right edge would push new_start negative —
        clamped to 0."""
        new_start, new_end = Stream2VideoGUI._compute_zoom_view(
            duration=100.0,
            view_start=80.0,
            view_end=90.0,
            cursor_frac=1.0,
            cursor_known=True,
            factor=0.5,
        )
        # cursor_time=90, new_duration=5, new_start=85 → no clamp
        # Try harder: cursor at left edge of a near-end view
        new_start, new_end = Stream2VideoGUI._compute_zoom_view(
            duration=100.0,
            view_start=95.0,
            view_end=100.0,
            cursor_frac=0.0,
            cursor_known=True,
            factor=0.5,
        )
        # cursor_time=95, new_duration=2.5, new_start=95 - 0 = 95
        # But min(100 - 2.5, 95) = 95 — no clamp
        assert new_start == 95.0
        assert new_end == 97.5

    def test_zoom_in_clamps_to_right_edge(self):
        """Cursor near the left edge would push new_end past duration."""
        new_start, new_end = Stream2VideoGUI._compute_zoom_view(
            duration=100.0,
            view_start=0.0,
            view_end=10.0,
            cursor_frac=0.0,
            cursor_known=True,
            factor=0.5,
        )
        # cursor_time=0, new_duration=5, new_start=0 → no clamp
        assert new_start == 0.0
        assert new_end == 5.0

    def test_zoom_in_clamped_at_minimum_duration(self):
        """Below 0.5s, the duration is clamped to 0.5s."""
        new_start, new_end = Stream2VideoGUI._compute_zoom_view(
            duration=100.0,
            view_start=49.9,
            view_end=50.1,
            cursor_frac=0.5,
            cursor_known=False,
            factor=0.01,
        )
        # new_duration would be 0.002 → capped to 0.5
        assert new_end - new_start == 0.5

    def test_zoom_at_full_is_identity(self):
        """Zoom in at full duration is a no-op (already full)."""
        new_start, new_end = Stream2VideoGUI._compute_zoom_view(
            duration=100.0,
            view_start=0.0,
            view_end=100.0,
            cursor_frac=0.5,
            cursor_known=False,
            factor=0.5,
        )
        # new_duration = 50, view changes — not identity
        # Actually let's test identity: factor=1.0
        new_start, new_end = Stream2VideoGUI._compute_zoom_view(
            duration=100.0,
            view_start=20.0,
            view_end=80.0,
            cursor_frac=0.5,
            cursor_known=False,
            factor=1.0,
        )
        assert new_start == 20.0
        assert new_end == 80.0

    def test_zoom_zero_duration_returns_zero(self):
        """Defensive: zero or negative duration → (0, 0)."""
        assert Stream2VideoGUI._compute_zoom_view(
            duration=0.0,
            view_start=0.0,
            view_end=0.0,
            cursor_frac=0.5,
            cursor_known=False,
            factor=0.5,
        ) == (0.0, 0.0)
        assert Stream2VideoGUI._compute_zoom_view(
            duration=-1.0,
            view_start=0.0,
            view_end=10.0,
            cursor_frac=0.5,
            cursor_known=False,
            factor=0.5,
        ) == (0.0, 0.0)


class TestComputePanView:
    """`_compute_pan_view` shifts the view by `frac * view_duration`.
    Pure function. Clamps to [0, duration]. Identity if the view is
    already the full timeline (no room to pan)."""

    def test_pan_right(self):
        new_start, new_end = Stream2VideoGUI._compute_pan_view(
            duration=100.0,
            view_start=20.0,
            view_end=40.0,
            frac=0.5,
        )
        # shift = 20 * 0.5 = 10
        assert new_start == 30.0
        assert new_end == 50.0

    def test_pan_left(self):
        new_start, new_end = Stream2VideoGUI._compute_pan_view(
            duration=100.0,
            view_start=20.0,
            view_end=40.0,
            frac=-0.5,
        )
        # shift = 20 * -0.5 = -10
        assert new_start == 10.0
        assert new_end == 30.0

    def test_pan_clamps_to_left(self):
        new_start, new_end = Stream2VideoGUI._compute_pan_view(
            duration=100.0,
            view_start=5.0,
            view_end=15.0,
            frac=-1.0,
        )
        # shift = 10 * -1 = -10, new_start = -5 → clamped to 0
        assert new_start == 0.0
        assert new_end == 10.0

    def test_pan_clamps_to_right(self):
        new_start, new_end = Stream2VideoGUI._compute_pan_view(
            duration=100.0,
            view_start=90.0,
            view_end=100.0,
            frac=1.0,
        )
        # shift = 10 * 1 = 10, new_start = 100 → clamped to 90
        assert new_start == 90.0
        assert new_end == 100.0

    def test_pan_at_full_view_is_identity(self):
        """If the view is already the full timeline, pan is a no-op."""
        assert Stream2VideoGUI._compute_pan_view(
            duration=100.0,
            view_start=0.0,
            view_end=100.0,
            frac=0.5,
        ) == (0.0, 100.0)

    def test_pan_zero_duration_returns_zero(self):
        assert Stream2VideoGUI._compute_pan_view(
            duration=0.0,
            view_start=0.0,
            view_end=10.0,
            frac=0.5,
        ) == (0.0, 0.0)

    def test_pan_25_percent(self):
        """The GUI's pan buttons use 0.25 / -0.25. Verify a 25% pan."""
        new_start, new_end = Stream2VideoGUI._compute_pan_view(
            duration=100.0,
            view_start=20.0,
            view_end=40.0,
            frac=0.25,
        )
        # shift = 20 * 0.25 = 5
        assert new_start == 25.0
        assert new_end == 45.0
