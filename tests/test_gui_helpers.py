"""Tests for stream2video.gui_helpers (pure functions extracted from gui.py).

These tests cover the formatting / decision logic that previously lived
inline in the GUI class methods and couldn't be unit-tested without
driving the Tk main loop. Each helper here is pure: no Tk, no side
effects, no I/O.
"""

from __future__ import annotations

from pathlib import Path

from stream2video.gui_helpers import (
    STATUS_MAX,
    STATUS_UPDATE_INTERVAL,
    build_cli_command,
    build_download_status,
    build_eta_tail,
    build_overall_line,
    build_silence_info_line,
    build_waveform_view_label,
    should_update_status,
    truncate_status,
)


class TestBuildCliCommand:
    def test_minimal_command_has_required_flags(self):
        cmd = build_cli_command(
            "video.mp4",
            Path("./out"),
            method="segment",
            encoder="libx264",
            video_quality="medium",
            download_quality="best",
        )
        assert cmd.startswith("stream2video ")
        assert "video.mp4" in cmd
        assert "-o" in cmd
        assert "--method segment" in cmd
        assert "--encoder libx264" in cmd
        assert "--video-quality medium" in cmd
        assert "--download-quality best" in cmd

    def test_force_and_delete_after_add_short_flags(self):
        cmd = build_cli_command(
            "x",
            Path("./o"),
            method="batch",
            encoder="libx264",
            video_quality="medium",
            download_quality="best",
            force=True,
            delete_after=True,
        )
        assert " -f" in cmd
        assert "--delete-after" in cmd

    def test_default_advanced_flags_omitted(self):
        # audio_quality / software_fallback / x264_preset /
        # encoder_threads / output_fps are at their defaults — the
        # copied command stays compact.
        cmd = build_cli_command(
            "x",
            Path("./o"),
            method="segment",
            encoder="libx264",
            video_quality="medium",
            download_quality="best",
        )
        assert "--audio-quality" not in cmd
        assert "--software-fallback" not in cmd
        assert "--x264-preset" not in cmd
        assert "--encoder-threads" not in cmd
        assert "--output-fps" not in cmd

    def test_non_default_advanced_flags_appended(self):
        cmd = build_cli_command(
            "x",
            Path("./o"),
            method="segment",
            encoder="libx264",
            video_quality="medium",
            download_quality="best",
            audio_quality="high",
            software_fallback="enabled",
            x264_preset="ultrafast",
            encoder_threads=4,
            output_fps="60",
        )
        assert "--audio-quality high" in cmd
        assert "--software-fallback enabled" in cmd
        assert "--x264-preset ultrafast" in cmd
        assert "--encoder-threads 4" in cmd
        assert "--output-fps 60" in cmd

    def test_config_path_appended_as_c_flag(self):
        cmd = build_cli_command(
            "x",
            Path("./o"),
            method="segment",
            encoder="libx264",
            video_quality="medium",
            download_quality="best",
            config_path=Path("/tmp/cfg.yaml"),
        )
        assert "-c " in cmd
        assert "cfg.yaml" in cmd

    def test_empty_input_omits_argument(self):
        cmd = build_cli_command(
            "",
            Path("./o"),
            method="segment",
            encoder="libx264",
            video_quality="medium",
            download_quality="best",
        )
        # No quoted empty string between 'stream2video' and '-o'
        assert "stream2video  -o" not in cmd
        assert "stream2video -o" in cmd


class TestTruncateStatus:
    def test_short_string_unchanged(self):
        assert truncate_status("hello", 50) == "hello"

    def test_exact_length_unchanged(self):
        s = "x" * STATUS_MAX
        assert truncate_status(s) == s

    def test_long_string_truncated_with_ellipsis(self):
        s = "x" * (STATUS_MAX + 10)
        out = truncate_status(s)
        assert len(out) == STATUS_MAX
        assert out.endswith("…")

    def test_custom_max_len(self):
        assert truncate_status("abcdefgh", 5) == "abcd…"


class TestBuildDownloadStatus:
    def test_with_total_bytes_shows_percent(self):
        s = build_download_status(
            downloaded_bytes=500.0,
            total_bytes=1000.0,
            speed=1024.0,
            eta=5.0,
        )
        assert "50.0%" in s
        assert "ETA" in s

    def test_without_total_bytes_omits_percent(self):
        s = build_download_status(
            downloaded_bytes=500.0,
            total_bytes=None,
            speed=1024.0,
            eta=None,
        )
        assert "%" not in s
        assert "ETA" not in s

    def test_unknown_fields_render_question_mark(self):
        s = build_download_status(
            downloaded_bytes=None,
            total_bytes=None,
            speed=None,
            eta=None,
        )
        # All fields unknown — line still readable.
        assert "?" in s

    def test_explicit_pct_overrides_computed(self):
        s = build_download_status(
            downloaded_bytes=200.0,
            total_bytes=1000.0,
            speed=0.0,
            eta=10.0,
            pct=99.9,
        )
        assert "99.9%" in s


class TestBuildEtaTail:
    def test_known_remaining_last_phase(self):
        assert build_eta_tail(120.0, more_phases=False) == "~2m 0s"

    def test_known_remaining_more_phases_appends_question_mark(self):
        tail = build_eta_tail(60.0, more_phases=True)
        assert tail.startswith("~")
        assert tail.endswith("+ ?")

    def test_none_remaining_more_phases_is_question(self):
        assert build_eta_tail(None, more_phases=True) == "?"

    def test_none_remaining_last_phase_is_dash(self):
        assert build_eta_tail(None, more_phases=False) == "—"

    def test_zero_remaining_last_phase_is_dash(self):
        assert build_eta_tail(0.0, more_phases=False) == "—"

    def test_negative_remaining_treated_as_unknown(self):
        assert build_eta_tail(-5.0, more_phases=True) == "?"


class TestBuildOverallLine:
    def test_format(self):
        line = build_overall_line(125.0, "~2m")
        assert "Elapsed:" in line
        assert "Remaining:" in line
        assert "~2m" in line


class TestBuildSilenceInfoLine:
    def test_with_duration(self):
        line = build_silence_info_line(num_silence=5, num_keep=6, keep_duration=120.0)
        assert "5 segments" in line
        assert "6 segments" in line
        assert "2m" in line

    def test_without_duration(self):
        line = build_silence_info_line(num_silence=3, num_keep=4, keep_duration=None)
        assert "3 segments" in line
        assert "4 segments" in line
        # No parenthetical duration when unknown.
        assert "(" not in line


class TestBuildWaveformViewLabel:
    def test_format(self):
        label = build_waveform_view_label(view_start=0.0, view_end=90.0, zoom=1.5)
        assert "00:00:00" in label
        assert "00:01:30" in label
        assert "1.5x" in label


class TestShouldUpdateStatus:
    def test_force_always_true(self):
        assert should_update_status(100.0, 100.0, force=True) is True

    def test_within_interval_dropped(self):
        now = 100.0
        last = 100.0 + STATUS_UPDATE_INTERVAL / 2
        assert should_update_status(last, now) is False

    def test_after_interval_passes(self):
        now = 100.0
        last = 100.0 - STATUS_UPDATE_INTERVAL - 0.01
        assert should_update_status(last, now) is True

    def test_custom_interval(self):
        # 10s interval — a 5s gap shouldn't pass.
        assert should_update_status(100.0, 105.0, interval=10.0) is False
        assert should_update_status(100.0, 111.0, interval=10.0) is True
