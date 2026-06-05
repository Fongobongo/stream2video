"""Tests for stream2video.gui formatting helpers.

These are static methods on Stream2VideoGUI (or module-level helpers) that
build the human-readable strings used in the status line, log, and popup
on completion. They are pure functions and don't require a Tk root.
"""
from stream2video.gui import Stream2VideoGUI


_fmt_size = staticmethod(Stream2VideoGUI._fmt_size).__func__
_fmt_time = staticmethod(Stream2VideoGUI._fmt_time).__func__
_fmt_clock_time = staticmethod(Stream2VideoGUI._fmt_clock_time).__func__


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
        assert _fmt_size(int(1.5 * 1024 ** 3)) == "1.5 GB"

    def test_terabytes(self):
        # >= 1024 GB rolls over to TB
        assert _fmt_size(2 * 1024 ** 4) == "2.0 TB"


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
        src = 6 * 3600 + 4 * 60 + 12      # 06:04:12
        dst = 34 * 60 + 11                 # 00:34:11
        assert f"{_fmt_clock_time(src)} -> {_fmt_clock_time(dst)}" == \
               "06:04:12 -> 00:34:11"
