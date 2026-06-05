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


class TestBuildCompletionSummary:
    """_build_completion_summary — pure function returning the four
    user-facing strings emitted at pipeline completion. Tested without
    instantiating the GUI (Tk) — that's the whole point of the refactor."""

    def _summary(self, **overrides):
        """Default-args helper for readability."""
        defaults = dict(
            src_size_bytes=20 * 1024 ** 3,        # 20.0 GB
            src_duration=6 * 3600 + 4 * 60 + 12,  # 06:04:12
            dst_size_bytes=int(1.1 * 1024 ** 3),  # 1.1 GB
            dst_duration=34 * 60 + 11,            # 00:34:11
            pipeline_seconds=23 * 60 + 5,         # 23m 5s
            output_path="D:/vids/out.mp4",
        )
        defaults.update(overrides)
        from stream2video.gui import _build_completion_summary
        return _build_completion_summary(**defaults)

    def test_returns_dict_with_four_keys(self):
        s = self._summary()
        assert set(s.keys()) == {"status", "log_lines", "popup", "overall_label"}

    def test_status_line_is_one_liner(self):
        """Status line must be a single line (no newlines) so it fits
        in the GUI's status bar."""
        s = self._summary()
        assert "\n" not in s["status"]
        assert s["status"].startswith("Complete!")

    def test_status_contains_all_four_metrics(self):
        s = self._summary()
        # 20.0 GB -> 1.1 GB
        assert "20.0 GB -> 1.1 GB" in s["status"]
        # 06:04:12 -> 00:34:11
        assert "06:04:12 -> 00:34:11" in s["status"]
        # (completed in 23m 5s)
        assert "(completed in 23m 5s)" in s["status"]

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

    def test_overall_label_format(self):
        s = self._summary()
        assert s["overall_label"] == "Total: 23m 5s"

    def test_none_source_duration_renders_as_question_mark(self):
        """ffprobe can fail; the summary must not crash and must show '?'
        in the source position. The format is 'src -> dst', so a None
        src_duration produces '? -> 00:34:11'."""
        s = self._summary(src_duration=None)
        assert "? -> 00:34:11" in s["status"]
        # dst is always known (it's computed from keep-segments locally)
        assert "06:04:12 -> ?" not in s["status"]
        joined = "\n".join(s["log_lines"])
        assert "? -> 00:34:11" in joined

    def test_negative_source_duration_renders_as_question_mark(self):
        s = self._summary(src_duration=-5)
        assert "? -> 00:34:11" in s["status"]

    def test_zero_duration_renders_as_zero_clock(self):
        """00:00:00 is a valid value (corrupted 0-byte file) — must not
        be replaced with '?'."""
        s = self._summary(src_duration=0, dst_duration=0)
        assert "00:00:00 -> 00:00:00" in s["status"]

    def test_subsecond_pipeline_renders_as_zero_seconds(self):
        """Pipeline that finishes in < 1s — must show '0s', not crash."""
        s = self._summary(pipeline_seconds=0.4)
        assert "completed in 0s)" in s["status"]
        assert s["overall_label"] == "Total: 0s"

    def test_size_uses_bytes_to_bytes_conversion(self):
        """1 GB = 1024^3 bytes exactly (no rounding issues)."""
        s = self._summary(
            src_size_bytes=1024 ** 3,  # exactly 1.0 GB
            dst_size_bytes=512 * 1024 ** 2,  # exactly 512.0 MB
        )
        assert "1.0 GB -> 512.0 MB" in s["status"]

    def test_days_in_duration_uses_day_field(self):
        """A 30+ hour source must still render cleanly. 30h 4m 12s =
        1d 6h 4m 12s = '1:06:04:12' (the day field kicks in at >= 24h
        so the value is never ambiguous)."""
        s = self._summary(
            src_duration=30 * 3600 + 4 * 60 + 12,  # 1d 6h 4m 12s
        )
        assert "1:06:04:12 -> 00:34:11" in s["status"]

    def test_long_pipeline_uses_d_h_m_s(self):
        """Pipeline wall-clock >= 24h uses _fmt_time's 'Xd Yh Zm Ws' format."""
        s = self._summary(pipeline_seconds=86400 + 3600 + 60)  # 1d 1h 1m
        assert "completed in 1d 1h 1m 0s)" in s["status"]
        assert s["overall_label"] == "Total: 1d 1h 1m 0s"


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

