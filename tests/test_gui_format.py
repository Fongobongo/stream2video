"""Tests for stream2video.formatters — pure formatting helpers.

These build the human-readable strings used in the GUI's status line,
log, and popup on completion. They don't require a Tk root or Pillow,
so the test module is import-safe in a minimal env.
"""

from stream2video.formatters import (
    fmt_clock_time,
    fmt_size,
    fmt_speed,
    fmt_time,
    fmt_total_label,
    fmt_zoom_text,
)


class TestFmtSize:
    """fmt_size — bytes -> human-readable string."""

    def test_bytes(self):
        assert fmt_size(500) == "500.0 B"

    def test_kilobytes(self):
        assert fmt_size(2048) == "2.0 KB"

    def test_megabytes(self):
        assert fmt_size(5 * 1024 * 1024) == "5.0 MB"

    def test_gigabytes(self):
        # Realistic: 1.5GB Twitch VOD
        assert fmt_size(int(1.5 * 1024**3)) == "1.5 GB"

    def test_terabytes(self):
        assert fmt_size(2 * 1024**4) == "2.0 TB"

    def test_zero(self):
        assert fmt_size(0) == "0.0 B"

    def test_below_kb_uses_bytes(self):
        assert fmt_size(1023) == "1023.0 B"


class TestFmtSpeed:
    """fmt_speed — bytes/sec -> '<size>/s' for the download status line."""

    def test_bytes_per_sec(self):
        assert fmt_speed(500) == "500.0 B/s"

    def test_kibibytes_per_sec(self):
        assert fmt_speed(5 * 1024 * 1024) == "5.0 MB/s"

    def test_none_returns_question_mark(self):
        # yt-dlp emits NA for speed during the initial ramp-up.
        assert fmt_speed(None) == "?"

    def test_negative_returns_question_mark(self):
        assert fmt_speed(-1) == "?"

    def test_zero(self):
        assert fmt_speed(0) == "0.0 B/s"

    def test_gigabits_per_sec(self):
        # Realistic upper bound for a fast cable / fibre download.
        assert fmt_speed(100 * 1024 * 1024) == "100.0 MB/s"


class TestFmtTime:
    """fmt_time — seconds -> 'Xh Ym Zs' / 'Xm Ys' / 'Xs'."""

    def test_seconds_only(self):
        assert fmt_time(42) == "42s"

    def test_minutes_and_seconds(self):
        assert fmt_time(125) == "2m 5s"

    def test_hours_minutes_seconds(self):
        assert fmt_time(3661) == "1h 1m 1s"

    def test_days_hours_minutes_seconds(self):
        assert fmt_time(93784) == "1d 2h 3m 4s"

    def test_zero(self):
        assert fmt_time(0) == "0s"

    def test_subsecond_truncates(self):
        assert fmt_time(0.9) == "0s"


class TestFmtClockTime:
    """fmt_clock_time — seconds -> 'HH:MM:SS' (or 'D:HH:MM:SS' if >= 24h),
    zero-padded. Returns '?' for None or negative input."""

    def test_hours_minutes_seconds_zero_padded(self):
        assert fmt_clock_time(2051) == "00:34:11"

    def test_six_hours_four_minutes_twelve_seconds(self):
        assert fmt_clock_time(21852) == "06:04:12"

    def test_zero(self):
        assert fmt_clock_time(0) == "00:00:00"

    def test_sub_minute(self):
        assert fmt_clock_time(45) == "00:00:45"

    def test_under_hour(self):
        assert fmt_clock_time(303) == "00:05:03"

    def test_over_24_hours_uses_d_prefix(self):
        assert fmt_clock_time(93784) == "1:02:03:04"

    def test_exactly_24_hours(self):
        assert fmt_clock_time(91800) == "1:01:30:00"

    def test_none_returns_question_mark(self):
        assert fmt_clock_time(None) == "?"

    def test_negative_returns_question_mark(self):
        assert fmt_clock_time(-1) == "?"

    def test_subsecond_truncates(self):
        assert fmt_clock_time(1.9) == "00:00:01"

    def test_composes_into_summary_format(self):
        """The completion summary uses `f"{src} -> {dst}"` to make the
        size/duration deltas scannable. Lock the format string."""
        src = 6 * 3600 + 4 * 60 + 12
        dst = 34 * 60 + 11
        assert f"{fmt_clock_time(src)} -> {fmt_clock_time(dst)}" == "06:04:12 -> 00:34:11"


class TestFmtTotalLabel:
    """fmt_total_label — formats the Total wall-clock label that lives
    below the progress bar."""

    def test_short_pipeline(self):
        assert fmt_total_label(23 * 60 + 5) == "Total: 23m 5s"

    def test_zero_seconds(self):
        assert fmt_total_label(0) == "Total: 0s"

    def test_subsecond_rounds_down(self):
        assert fmt_total_label(0.4) == "Total: 0s"

    def test_multi_day(self):
        assert fmt_total_label(86400 + 3600 + 60) == "Total: 1d 1h 1m 0s"

    def test_seconds_only(self):
        assert fmt_total_label(42) == "Total: 42s"


class TestFmtZoomText:
    """`fmt_zoom_text` formats the zoom multiplier (duration /
    view_duration) for the controls and status line."""

    def test_one_x(self):
        assert fmt_zoom_text(1.0) == "1.0x"

    def test_one_point_five_x(self):
        assert fmt_zoom_text(1.5) == "1.5x"

    def test_two_x(self):
        assert fmt_zoom_text(2.0) == "2.0x"

    def test_just_below_10x_keeps_decimal(self):
        assert fmt_zoom_text(9.94) == "9.9x"

    def test_exactly_10x_drops_decimal(self):
        assert fmt_zoom_text(10.0) == "10x"

    def test_large_rounds_to_int(self):
        assert fmt_zoom_text(14.7) == "15x"

    def test_large_rounds_down_when_below_half(self):
        assert fmt_zoom_text(14.4) == "14x"

    def test_just_below_10x_boundary_keeps_decimal(self):
        """Edge: 9.94 must still print '9.9x', not jump to int formatting.

        The threshold for the int branch is based on the 1-dp-rounded
        value, so anything that rounds to <10.0 keeps the decimal
        format. ``9.94`` rounds to ``9.9`` and prints '9.9x'."""
        assert fmt_zoom_text(9.94) == "9.9x"

    def test_just_above_10x_boundary_drops_decimal(self):
        """Edge: 9.96 rounds to 10.0 at 1 dp, so it switches to int
        formatting ('10x') — matching what '10.0x' would have shown
        in the decimal branch, eliminating the discontinuity."""
        assert fmt_zoom_text(9.96) == "10x"
