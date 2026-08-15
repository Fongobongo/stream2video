"""Tests for stream2video.gui_helpers (pure functions extracted from gui.py).

These tests cover the formatting / decision logic that previously lived
inline in the GUI class methods and couldn't be unit-tested without
driving the Tk main loop. Each helper here is pure: no Tk, no side
effects, no I/O.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

from stream2video.gui_helpers import (
    STATUS_UPDATE_INTERVAL,
    TOTAL_ETA_MIN_PROGRESS,
    EtaSmoother,
    build_cli_command,
    build_compact_done_line,
    build_completion_summary,
    build_download_status,
    build_eta_tail,
    build_overall_line,
    build_phase_line,
    build_progress_meta_line,
    build_silence_info_line,
    build_total_line,
    mask_proxy,
    redact_proxy_in_cli_command,
    should_update_status,
)


# Windows cmdline splitter with CommandLineToArgvW / MSVCRT semantics
# (backslash-run doubling before quotes, "" escaping, trailing-run
# doubling) — the inverse of ``_quote_cli_arg``. Used to verify
# round-trips on win32, where shlex.split would apply POSIX rules to a
# command the user will paste into cmd.exe / PowerShell.
def _split_win_cmdline(line: str) -> list[str]:
    args: list[str] = []
    cur: list[str] = []
    in_quotes = False
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        if c == "\\":
            run = 0
            while i < n and line[i] == "\\":
                run += 1
                i += 1
            if i < n and line[i] == '"':
                cur.append("\\" * (run // 2))
                if run % 2 == 1:
                    cur.append('"')  # escaped quote — literal
                else:
                    in_quotes = not in_quotes
                i += 1
            else:
                cur.append("\\" * run)
        elif c == '"':
            if in_quotes and i + 1 < n and line[i + 1] == '"':
                cur.append('"')  # "" inside quotes — literal quote
                i += 2
            else:
                in_quotes = not in_quotes
                i += 1
        elif c in " \t" and not in_quotes:
            if cur:
                args.append("".join(cur))
                cur = []
            i += 1
        else:
            cur.append(c)
            i += 1
    if cur:
        args.append("".join(cur))
    return args


def _split_cmd(cmd: str) -> list[str]:
    """Split a built CLI command with the quoting rules of the current platform."""
    return _split_win_cmdline(cmd) if sys.platform == "win32" else shlex.split(cmd)


class TestQuoteCliArg:
    """MSVCRT quoting rules for Windows; shlex.quote on POSIX."""

    def test_bare_token_unquoted(self):
        from stream2video.gui_helpers import _quote_cli_arg

        assert _quote_cli_arg("C:\\Users\\John\\video.mp4") == "C:\\Users\\John\\video.mp4"

    def test_space_token_double_quoted(self):
        from stream2video.gui_helpers import _quote_cli_arg

        if sys.platform == "win32":
            assert _quote_cli_arg("a b") == '"a b"'
        else:
            assert _quote_cli_arg("a b") == "'a b'"

    def test_embedded_quote_doubled(self):
        from stream2video.gui_helpers import _quote_cli_arg

        if sys.platform == "win32":
            assert _quote_cli_arg('a"b') == '"a\\"b"'
        else:
            assert _quote_cli_arg('a"b') == "'a\"b'"

    def test_trailing_backslash_run_doubled(self):
        from stream2video.gui_helpers import _quote_cli_arg

        if sys.platform == "win32":
            # A bare trailing-backslash token needs no quoting at all
            # (no metachars); when the token IS quoted (space below),
            # the trailing run must be doubled so it can't escape the
            # closing quote (MSVCRT: \ before " = literal quote).
            assert _quote_cli_arg("C:\\dir\\") == "C:\\dir\\"
            assert _quote_cli_arg("C:\\dir name\\") == '"C:\\dir name\\\\"'
        else:
            assert _quote_cli_arg("C:\\dir\\") == "'C:\\dir\\'"

    def test_backslash_before_quote_odd_run(self):
        from stream2video.gui_helpers import _quote_cli_arg

        if sys.platform == "win32":
            # k=1 backslash before a quote → 2k+1 = 3 backslashes + quote
            # (the odd one escapes the quote on the way back).
            assert _quote_cli_arg('a\\"b') == '"a\\\\\\"b"'
        else:
            assert _quote_cli_arg('a\\"b') == "'a\\\"b'"

    def test_win_roundtrip_with_splitter(self):
        # Property check: _quote_cli_arg → _split_win_cmdline is the
        # identity on a set of nasty tokens.
        if sys.platform != "win32":
            return
        from stream2video.gui_helpers import _quote_cli_arg

        for tok in (
            "plain",
            "a b",
            'a"b',
            "C:\\dir\\",
            "trail\\\\",
            'quote at end"',
            "socks5://user:p;ss&$(touch pwned)@proxy:1080",
            'mix\\" of\\ everything',
        ):
            parsed = _split_win_cmdline(_quote_cli_arg(tok))
            assert parsed == [tok], f"{tok!r} → {_quote_cli_arg(tok)!r} → {parsed!r}"


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
        assert "--use-crf" not in cmd

    def test_proxy_flag_omitted_by_default(self):
        # No proxy set → the copied command stays compact (no --proxy).
        cmd = build_cli_command(
            "x",
            Path("./o"),
            method="segment",
            encoder="libx264",
            video_quality="medium",
            download_quality="best",
        )
        assert "--proxy" not in cmd

    def test_proxy_flag_appended_when_set(self):
        # A proxy set via the GUI's proxy button must land in the
        # copied CLI command so a paste runs through the same proxy.
        cmd = build_cli_command(
            "x",
            Path("./o"),
            method="segment",
            encoder="libx264",
            video_quality="medium",
            download_quality="best",
            proxy="http://127.0.0.1:8080",
        )
        assert "--proxy http://127.0.0.1:8080" in cmd

    def test_proxy_with_space_is_shell_quoted(self):
        # Regression: a proxy containing a space (e.g. a password with a
        # space) used to be injected as a raw token, so pasting the
        # copied command would split it into multiple shell words and
        # mangle the argument. It must be quoted for the target shell
        # (double-quoted MSVCRT form on Windows — shlex.quote's POSIX
        # single quotes are not understood by cmd.exe), and the raw
        # unquoted token must not appear in the command string.
        proxy = "socks5://user:pa ss@proxy:1080"
        cmd = build_cli_command(
            "x",
            Path("./o"),
            method="segment",
            encoder="libx264",
            video_quality="medium",
            download_quality="best",
            proxy=proxy,
        )
        if sys.platform == "win32":
            assert '--proxy "socks5://user:pa ss@proxy:1080"' in cmd
        else:
            assert f"--proxy {shlex.quote(proxy)}" in cmd
        assert "--proxy socks5://user:pa ss@proxy:1080" not in cmd
        tokens = _split_cmd(cmd)
        assert tokens[tokens.index("--proxy") + 1] == proxy

    def test_proxy_with_shell_metacharacters_is_quoted_and_roundtrips(self):
        # Regression: ``;``, ``&``, ``$(...)`` etc. inside the proxy
        # string must stay one argument when pasted into a shell — a raw
        # token would terminate the command or execute the rest.
        proxy = "socks5://user:p;ss&$(touch pwned)@proxy:1080"
        cmd = build_cli_command(
            "x",
            Path("./o"),
            method="segment",
            encoder="libx264",
            video_quality="medium",
            download_quality="best",
            proxy=proxy,
        )
        # Round-trip: re-splitting the command must yield the proxy
        # exactly as one argument (no injection, no mangling).
        tokens = _split_cmd(cmd)
        assert tokens[tokens.index("--proxy") + 1] == proxy
        # The command must contain no unquoted metacharacters adjacent
        # to the --proxy flag.
        assert "--proxy " + proxy + " " not in cmd

    def test_proxy_with_credentials_quoted_in_command(self):
        proxy = "socks5://user:secret@host:1080"
        cmd = build_cli_command(
            "x",
            Path("./o"),
            method="segment",
            encoder="libx264",
            video_quality="medium",
            download_quality="best",
            proxy=proxy,
        )
        assert "--proxy socks5://user:secret@host:1080" in cmd
        tokens = _split_cmd(cmd)
        assert tokens[tokens.index("--proxy") + 1] == proxy

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

    def test_x264_low_memory_appended_when_true(self):
        cmd = build_cli_command(
            "x",
            Path("./o"),
            method="segment",
            encoder="libx264",
            video_quality="medium",
            download_quality="best",
            x264_low_memory=True,
        )
        assert "--x264-low-memory" in cmd

    def test_x264_low_memory_omitted_when_false(self):
        cmd = build_cli_command(
            "x",
            Path("./o"),
            method="segment",
            encoder="libx264",
            video_quality="medium",
            download_quality="best",
            x264_low_memory=False,
        )
        assert "--x264-low-memory" not in cmd

    def test_use_crf_appended_when_true(self):
        cmd = build_cli_command(
            "x",
            Path("./o"),
            method="segment",
            encoder="libx264",
            video_quality="medium",
            download_quality="best",
            use_crf=True,
        )
        assert "--use-crf" in cmd

    def test_use_crf_omitted_when_false(self):
        cmd = build_cli_command(
            "x",
            Path("./o"),
            method="segment",
            encoder="libx264",
            video_quality="medium",
            download_quality="best",
            use_crf=False,
        )
        assert "--use-crf" not in cmd

    def test_memory_limit_flags_omitted_at_defaults(self):
        cmd = build_cli_command(
            "x",
            Path("./o"),
            method="segment",
            encoder="libx264",
            video_quality="medium",
            download_quality="best",
        )
        assert "--memory-limit-mb" not in cmd
        assert "--memory-reserve-mb" not in cmd

    def test_boolean_defaults_match_config_defaults(self):
        # Parity: the GUI's copied command must agree with
        # CONFIG_DEFAULTS (gapless_concat=True, per_video_dir=True,
        # audio_quality="source"). At the defaults no --no-* flag is
        # emitted; flipping the config value to False must emit it.
        defaults = build_cli_command(
            "x",
            Path("./o"),
            method="segment",
            encoder="libx264",
            video_quality="medium",
            download_quality="best",
        )
        assert "--no-gapless-concat" not in defaults
        assert "--no-per-video-dir" not in defaults
        assert "--audio-quality" not in defaults

        flipped = build_cli_command(
            "x",
            Path("./o"),
            method="segment",
            encoder="libx264",
            video_quality="medium",
            download_quality="best",
            gapless_concat=False,
            per_video_dir=False,
        )
        assert "--no-gapless-concat" in flipped
        assert "--no-per-video-dir" in flipped

    def test_memory_limit_flags_appended_when_non_default(self):
        cmd = build_cli_command(
            "x",
            Path("./o"),
            method="segment",
            encoder="libx264",
            video_quality="medium",
            download_quality="best",
            memory_limit_mb=4096,
            memory_reserve_mb=1024,
        )
        assert "--memory-limit-mb 4096" in cmd
        assert "--memory-reserve-mb 1024" in cmd

    def test_phase_timeout_flags_omitted_at_defaults(self):
        # All phase-timeout flags default to their historical
        # values; when nothing is customised, the copied command stays
        # compact (no --segment-timeout 600 etc. noise).
        cmd = build_cli_command(
            "x",
            Path("./o"),
            method="segment",
            encoder="libx264",
            video_quality="medium",
            download_quality="best",
        )
        assert "--segment-timeout" not in cmd
        assert "--final-concat-timeout" not in cmd
        assert "--silence-timeout" not in cmd
        assert "--stall-timeout" not in cmd
        assert "--waveform-timeout" not in cmd
        assert "--batch-chunk-size" not in cmd
        assert "--min-part-bytes" not in cmd

    def test_phase_timeout_flags_appended_when_non_default(self):
        cmd = build_cli_command(
            "x",
            Path("./o"),
            method="batch",
            encoder="libx264",
            video_quality="medium",
            download_quality="best",
            segment_encode_timeout=1200,
            final_concat_timeout=172800,
            silence_timeout=72000,
            stall_kill_timeout=600,
            waveform_timeout=900,
            batch_chunk_size=20,
            min_part_bytes=2048,
        )
        assert "--segment-timeout 1200" in cmd
        assert "--final-concat-timeout 172800" in cmd
        assert "--silence-timeout 72000" in cmd
        assert "--stall-timeout 600" in cmd
        assert "--waveform-timeout 900" in cmd
        assert "--batch-chunk-size 20" in cmd
        assert "--min-part-bytes 2048" in cmd


class TestMaskProxy:
    def test_no_credentials_unchanged(self):
        # A proxy without user:pass has nothing to hide — shown as-is.
        assert mask_proxy("http://127.0.0.1:8080") == "http://127.0.0.1:8080"

    def test_user_pass_masked(self):
        # Regression: the GUI log must not contain the proxy password
        # in plain text; only the scheme/host survive, credentials
        # become ***:***.
        masked = mask_proxy("socks5://user:super-secret@host:1080")
        assert masked == "socks5://***:***@host:1080"
        assert "super-secret" not in masked
        assert "user" not in masked

    def test_password_with_at_sign_fully_masked(self):
        # An ``@`` inside the password must not leak the tail of the
        # credentials into the "host" part.
        masked = mask_proxy("socks5://user:pa@ss@host:1080")
        assert masked == "socks5://***:***@host:1080"
        assert "pa@ss" not in masked

    def test_empty_returns_empty(self):
        assert mask_proxy("") == ""

    def test_password_with_space_and_metacharacters_masked(self):
        # The report's repro: a password containing a space and shell
        # metacharacters must be fully hidden, host survives for
        # troubleshooting.
        masked = mask_proxy("socks5://user:pa ss;echo PWNED@proxy:1080")
        assert masked == "socks5://***:***@proxy:1080"
        assert "pa ss" not in masked
        assert "PWNED" not in masked


class TestRedactProxyInCliCommand:
    def test_password_not_present_in_redacted_command(self):
        proxy = "socks5://user:super-secret@host:1080"
        cmd = build_cli_command(
            "x",
            Path("./o"),
            method="segment",
            encoder="libx264",
            video_quality="medium",
            download_quality="best",
            proxy=proxy,
        )
        redacted = redact_proxy_in_cli_command(cmd, proxy)
        assert "super-secret" not in redacted
        assert "socks5://***:***@host:1080" in redacted
        # The rest of the command survives untouched.
        assert redacted.startswith("stream2video ")
        assert "--method segment" in redacted

    def test_empty_proxy_returns_command_unchanged(self):
        cmd = "stream2video -o out"
        assert redact_proxy_in_cli_command(cmd, "") == cmd
        assert redact_proxy_in_cli_command(cmd, None) == cmd

    def test_quoted_proxy_in_command_redacted(self):
        # Space-containing proxy gets quoted in the command; the
        # redacted log line must swap that quoted token, not the raw
        # password.
        proxy = "socks5://user:pa ss@proxy:1080"
        cmd = build_cli_command(
            "x",
            Path("./o"),
            method="segment",
            encoder="libx264",
            video_quality="medium",
            download_quality="best",
            proxy=proxy,
        )
        redacted = redact_proxy_in_cli_command(cmd, proxy)
        assert "pa ss" not in redacted
        assert "socks5://***:***@proxy:1080" in redacted

    def test_metacharacter_proxy_redacted(self):
        proxy = "socks5://user:p;ss&$(touch pwned)@proxy:1080"
        cmd = build_cli_command(
            "x",
            Path("./o"),
            method="segment",
            encoder="libx264",
            video_quality="medium",
            download_quality="best",
            proxy=proxy,
        )
        redacted = redact_proxy_in_cli_command(cmd, proxy)
        assert "p;ss" not in redacted
        assert "touch pwned" not in redacted
        assert "socks5://***:***@proxy:1080" in redacted


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

    def test_over_100_percent_clamped(self):
        # A server that under-reports Content-Length (chunked transfer,
        # unknown initial size) can give downloaded > total — the UI used
        # to render "250.0%".
        s = build_download_status(
            downloaded_bytes=250.0,
            total_bytes=100.0,
            speed=1024.0,
            eta=0.0,
        )
        assert "100.0%" in s
        assert "250.0%" not in s


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


class TestBuildProgressMetaLine:
    def test_overall_and_total_combined(self):
        line = build_progress_meta_line(125.0, "~2m", 300.0)
        assert "Elapsed:" in line
        assert "Remaining: ~2m" in line
        assert "Total: 2m 5s / ~5m 0s" in line

    def test_no_total_when_estimate_missing(self):
        line = build_progress_meta_line(75.0, "?", None)
        assert "Total" not in line
        assert "Elapsed:" in line

    def test_no_total_when_estimate_below_elapsed(self):
        line = build_progress_meta_line(300.0, "~1m", 100.0)
        assert "Total" not in line


class TestEtaSmoother:
    def test_first_sample_is_raw(self):
        s = EtaSmoother(alpha=0.25)
        assert s.update(100.0) == 100.0

    def test_smooths_jittery_samples_towards_mean(self):
        s = EtaSmoother(alpha=0.25)
        # Alternating raw samples should converge near their mean, not
        # bounce between extremes.
        values = [s.update(v) for v in (60, 140, 60, 140, 60, 140)]
        assert 60 <= values[-1] <= 140
        # Second half of the series is strictly less jittery than raw
        # (each new smoothed value moves by at most alpha * range).
        from itertools import pairwise

        raw_jump = 140 - 60
        deltas = [abs(b - a) for a, b in pairwise(values)]
        assert max(deltas[-2:]) <= 0.25 * raw_jump

    def test_none_pauses_and_replays_last_value(self):
        s = EtaSmoother(alpha=0.25)
        s.update(50.0)
        assert s.update(None) == 50.0
        assert s.update(None) == 50.0

    def test_none_before_any_sample_returns_none(self):
        s = EtaSmoother()
        assert s.update(None) is None

    def test_reset_clears_state(self):
        s = EtaSmoother(alpha=0.25)
        s.update(100.0)
        s.reset()
        # Next sample after a phase switch starts from raw again
        # (no bleed-through of the old phase's estimate).
        assert s.update(10.0) == 10.0

    def test_negative_raw_is_clamped_to_zero(self):
        s = EtaSmoother()
        assert s.update(-5.0) == 0.0


class TestBuildTotalLine:
    def test_elapsed_only_when_no_estimate(self):
        assert build_total_line(75.0, None) == "Total: 1m 15s"

    def test_estimate_appended_when_above_elapsed(self):
        line = build_total_line(75.0, 300.0)
        assert line == "Total: 1m 15s / ~5m 0s"

    def test_estimate_below_elapsed_is_hidden(self):
        # Can happen on a spiky progress estimate (progress > the real
        # fraction would imply the pipeline "already finished").
        assert build_total_line(300.0, 100.0) == "Total: 5m 0s"

    def test_min_progress_threshold_constant(self):
        # Pinned: the GUI hides the overall ETA until the pipeline's
        # progress reaches 2 %. Bump deliberately if the UX changes.
        assert TOTAL_ETA_MIN_PROGRESS == 0.02


class TestBuildCompactDoneLine:
    def test_typical_compression(self):
        line = build_compact_done_line(2530.0, 750.0, 495.0)
        assert line.startswith("Done: 00:42:10")
        assert "00:12:30" in line
        assert "70%" in line  # 750/2530 ≈ 30 % kept → 70 % cut
        assert "8m 15s" in line

    def test_unknown_source_duration_drops_percent(self):
        line = build_compact_done_line(None, 750.0, 495.0)
        assert "Done:" in line
        assert "%" not in line

    def test_zero_source_duration_drops_percent(self):
        assert "%" not in build_compact_done_line(0.0, 0.0, 5.0)


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


class TestBuildCompletionSummary:
    def test_status_has_complete_and_pipeline_time(self):
        s = build_completion_summary(
            src_size_bytes=100_000_000,
            src_duration=3600.0,
            dst_size_bytes=20_000_000,
            dst_duration=2700.0,
            pipeline_seconds=600.0,
            output_path="/tmp/out.mp4",
        )
        assert s["status"].startswith("Complete!")
        assert "10m" in s["status"]  # 600s = 10m

    def test_log_lines_have_separator_and_output_path(self):
        s = build_completion_summary(
            src_size_bytes=100,
            src_duration=10.0,
            dst_size_bytes=50,
            dst_duration=8.0,
            pipeline_seconds=2.0,
            output_path="/tmp/myfile.mp4",
        )
        lines = s["log_lines"]
        # First and last lines are the '======' separator.
        assert lines[0] == "=" * 60
        assert lines[-1] == "=" * 60
        # Output path is mentioned in the SUCCESS line.
        success_lines = [ln for ln in lines if "[SUCCESS]" in ln]
        assert len(success_lines) == 1
        assert "/tmp/myfile.mp4" in success_lines[0]

    def test_popup_contains_size_duration_and_path(self):
        s = build_completion_summary(
            src_size_bytes=100,
            src_duration=10.0,
            dst_size_bytes=50,
            dst_duration=8.0,
            pipeline_seconds=2.0,
            output_path="/tmp/myfile.mp4",
        )
        popup = s["popup"]
        assert "/tmp/myfile.mp4" in popup
        assert "Source:" in popup
        assert "Output:" in popup
        assert "Pipeline:" in popup

    def test_handles_none_src_duration(self):
        # src_duration=None can happen when the source video couldn't
        # be probed; fmt_clock_time handles None.
        s = build_completion_summary(
            src_size_bytes=100,
            src_duration=None,
            dst_size_bytes=50,
            dst_duration=8.0,
            pipeline_seconds=2.0,
            output_path="/tmp/out.mp4",
        )
        # Should not crash; the popup just shows "—" or similar for
        # the source duration.
        assert "Source:" in s["popup"]


class TestBuildPhaseLine:
    def test_known_step_with_percent(self):
        assert build_phase_line("2", 35) == "Step 2/4 · Silence (35%)"

    def test_known_step_without_percent(self):
        assert build_phase_line("3") == "Step 3/4 · Cutting"

    def test_none_step_is_empty(self):
        assert build_phase_line(None) == ""

    def test_unknown_step_is_empty(self):
        assert build_phase_line("7") == ""

    def test_all_labels_present(self):
        assert build_phase_line("1", 5) == "Step 1/4 · Download (5%)"
        assert build_phase_line("4", 6) == "Step 4/4 · Concat (6%)"
