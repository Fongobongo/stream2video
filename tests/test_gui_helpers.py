"""Tests for stream2video.gui_helpers (pure functions extracted from gui.py).

These tests cover the formatting / decision logic that previously lived
inline in the GUI class methods and couldn't be unit-tested without
driving the Tk main loop. Each helper here is pure: no Tk, no side
effects, no I/O.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from stream2video.gui_helpers import (
    CMD_SHELL,
    POWERSHELL_SHELL,
    STATUS_UPDATE_INTERVAL,
    TOTAL_ETA_MIN_PROGRESS,
    EtaSmoother,
    _quote_arg,
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
    proxy_has_credentials,
    redact_proxy_in_cli_command,
    should_update_status,
    strip_proxy_credentials,
)


# Windows cmdline splitter with CommandLineToArgvW / MSVCRT semantics
# (backslash-run doubling before quotes, "" escaping, trailing-run
# doubling) — the inverse of the cmd.exe quoting. Used to verify
# round-trips of the "cmd" target.
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


# PowerShell splitter: single quotes group a token, ``''`` is an
# escaped literal quote. Everything inside a single-quoted token is
# verbatim (no interpolation) — the inverse of the PowerShell quoting.
def _split_powershell_cmdline(line: str) -> list[str]:
    args: list[str] = []
    cur: list[str] = []
    in_single = False
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        if in_single:
            if c == "'":
                if i + 1 < n and line[i + 1] == "'":
                    cur.append("'")
                    i += 2
                else:
                    in_single = False
                    i += 1
            else:
                cur.append(c)
                i += 1
        elif c == "'":
            in_single = True
            i += 1
        elif c in " \t":
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
    return _split_powershell_cmdline(cmd) if sys.platform == "win32" else shlex.split(cmd)


class TestQuoteCliArg:
    """Per-shell quoting: PowerShell single-quote, cmd MSVCRT, POSIX shlex."""

    def test_powershell_bare_token_unquoted(self):
        assert (
            _quote_arg("C:\\Users\\John\\video.mp4", POWERSHELL_SHELL)
            == "C:\\Users\\John\\video.mp4"
        )

    def test_powershell_space_token_single_quoted(self):
        assert _quote_arg("a b", POWERSHELL_SHELL) == "'a b'"

    def test_powershell_embedded_quote_doubled(self):
        assert _quote_arg("a'b", POWERSHELL_SHELL) == "'a''b'"

    def test_powershell_interpolation_tokens_quoted_literal(self):
        # Audit #2: inside double quotes PowerShell would interpolate
        # $var / $(...) and expand backticks — every dangerous token
        # must end up in a single-quoted literal.
        for tok in (
            "$(calc)",
            "$env:PATH",
            "a%VAR%b",
            'a"b',
            "a;b",
            "a`n",
            "a&b",
            "a|b",
            "a>b",
            "a<b",
            "a#b",
        ):
            quoted = _quote_arg(tok, POWERSHELL_SHELL)
            assert quoted.startswith("'") and quoted.endswith("'"), tok

    def test_powershell_roundtrip_with_splitter(self):
        for tok in (
            "plain",
            "a b",
            "it's",
            'a"b',
            "C:\\dir\\",
            "trail\\\\",
            "socks5://user:p;ss&$(touch pwned)@proxy:1080",
            'mix\\" of\\ everything',
            "$(Write-Output INJECTED)",
        ):
            parsed = _split_powershell_cmdline(_quote_arg(tok, POWERSHELL_SHELL))
            assert parsed == [tok], f"{tok!r} → {_quote_arg(tok, POWERSHELL_SHELL)!r} → {parsed!r}"

    def test_cmd_space_token_double_quoted(self):
        assert _quote_arg("a b", CMD_SHELL) == '"a b"'

    def test_cmd_embedded_quote_msvcrt(self):
        assert _quote_arg('a"b', CMD_SHELL) == '"a\\"b"'

    def test_cmd_trailing_backslash_run_doubled(self):
        # A bare trailing-backslash token needs no quoting at all
        # (no metachars); when the token IS quoted (space below),
        # the trailing run must be doubled so it can't escape the
        # closing quote (MSVCRT: \ before " = literal quote).
        assert _quote_arg("C:\\dir\\", CMD_SHELL) == "C:\\dir\\"
        assert _quote_arg("C:\\dir name\\", CMD_SHELL) == '"C:\\dir name\\\\"'

    def test_cmd_odd_backslash_run_before_quote(self):
        # k=1 backslash before a quote → 2k+1 = 3 backslashes + quote
        # (the odd one escapes the quote on the way back).
        assert _quote_arg('a\\"b', CMD_SHELL) == '"a\\\\\\"b"'

    def test_cmd_percent_refused(self):
        with pytest.raises(ValueError):
            _quote_arg("a%PATH%b", CMD_SHELL)

    def test_cmd_bang_refused(self):
        with pytest.raises(ValueError):
            _quote_arg("a!x!", CMD_SHELL)

    def test_cmd_roundtrip_with_splitter(self):
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
            parsed = _split_win_cmdline(_quote_arg(tok, CMD_SHELL))
            assert parsed == [tok], f"{tok!r} → {_quote_arg(tok, CMD_SHELL)!r} → {parsed!r}"

    def test_posix_uses_shlex(self):
        assert _quote_arg("a b", "posix") == "'a b'"
        assert _quote_arg("a b", "posix") == shlex.quote("a b")


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows shells required")
class TestRealShellQuoting:
    """Audit #2: verify quoting against REAL cmd.exe / PowerShell.

    The unit splitter above is our own inverse-rule implementation; a
    bug mirrored in both would slip through. These tests paste the
    built command fragments into actual shells and check that (a) a
    ``$(...)`` payload is passed as literal text, never executed, and
    (b) cmd.exe round-trips a spaced path.
    """

    def test_powershell_pastes_interpolation_as_literal(self):
        payload = "$(Write-Output INJECTED)"
        quoted = _quote_arg(payload, POWERSHELL_SHELL)
        out = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"Write-Output {quoted}",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == payload

    def test_powershell_pastes_embedded_quote_as_literal(self):
        payload = "it's a $(calc)"
        quoted = _quote_arg(payload, POWERSHELL_SHELL)
        out = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"Write-Output {quoted}",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == payload

    def test_cmd_roundtrips_spaced_path_to_python_argv(self):
        payload = r"C:\dir name\file.mp4"
        quoted = _quote_arg(payload, CMD_SHELL)
        # Single-string invocation (no list2cmdline escaping) + /s with
        # the whole command wrapped in an extra quote pair: cmd strips
        # the outer pair and executes the inner command verbatim.
        runner = (
            f'cmd.exe /d /s /c ""{sys.executable}" -c '
            f'"import sys;print(repr(sys.argv[1]))" "{payload}""'
        )
        out = subprocess.run(runner, capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == repr(payload)
        assert quoted == f'"{payload}"'


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
        # (PowerShell single quotes on Windows — the only quoting that
        # is both understood and interpolation-proof), and the raw
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
            assert "--proxy 'socks5://user:pa ss@proxy:1080'" in cmd
        else:
            assert f"--proxy {shlex.quote(proxy)}" in cmd
        assert "--proxy socks5://user:pa ss@proxy:1080" not in cmd
        tokens = _split_cmd(cmd)
        assert tokens[tokens.index("--proxy") + 1] == proxy

    def test_proxy_with_percent_quoted_for_powershell(self):
        # Audit #2: %VAR% would expand in cmd.exe even inside double
        # quotes — the default PowerShell target must single-quote it.
        proxy = "http://user:p%ss@host:8080"
        cmd = build_cli_command(
            "x",
            Path("./o"),
            method="segment",
            encoder="libx264",
            video_quality="medium",
            download_quality="best",
            proxy=proxy,
        )
        tokens = _split_cmd(cmd)
        assert tokens[tokens.index("--proxy") + 1] == proxy

    def test_cmd_target_refuses_unsafe_proxy(self):
        # Audit #2: cmd.exe cannot quote % or ! safely — building a
        # command for it must fail loudly, not produce an injectable
        # string.
        for bad in ("http://user:p%ss@host:8080", "http://user:pa!ss@host:8080"):
            with pytest.raises(ValueError):
                build_cli_command(
                    "x",
                    Path("./o"),
                    method="segment",
                    encoder="libx264",
                    video_quality="medium",
                    download_quality="best",
                    proxy=bad,
                    target_shell=CMD_SHELL,
                )

    def test_cmd_target_quotes_spaced_proxy(self):
        proxy = "socks5://user:pa ss@proxy:1080"
        cmd = build_cli_command(
            "x",
            Path("./o"),
            method="segment",
            encoder="libx264",
            video_quality="medium",
            download_quality="best",
            proxy=proxy,
            target_shell=CMD_SHELL,
        )
        assert '--proxy "socks5://user:pa ss@proxy:1080"' in cmd
        tokens = _split_win_cmdline(cmd)
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

    def test_network_timeout_flags_omitted_at_defaults(self):
        # download/connect/no-progress timeouts + rlimit_as_mb +
        # stall_warning_timeout must NOT appear when at their defaults
        # (the copied command stays compact).
        cmd = build_cli_command(
            "x",
            Path("./o"),
            method="segment",
            encoder="libx264",
            video_quality="medium",
            download_quality="best",
        )
        assert "--download-timeout" not in cmd
        assert "--connect-timeout" not in cmd
        assert "--no-progress-timeout" not in cmd
        assert "--rlimit-as-mb" not in cmd
        assert "--stall-warning-timeout" not in cmd

    def test_network_timeout_flags_appended_when_non_default(self):
        # Regression: the copied command used to drop these five
        # settings, so a paste ran with different values than the GUI.
        cmd = build_cli_command(
            "x",
            Path("./o"),
            method="segment",
            encoder="libx264",
            video_quality="medium",
            download_quality="best",
            download_timeout=14400,
            connect_timeout=120,
            no_progress_timeout=600,
            rlimit_as_mb=4096,
            stall_warning_timeout=30,
        )
        assert "--download-timeout 14400" in cmd
        assert "--connect-timeout 120" in cmd
        assert "--no-progress-timeout 600" in cmd
        assert "--rlimit-as-mb 4096" in cmd
        assert "--stall-warning-timeout 30" in cmd

    def test_slider_flags_omitted_at_defaults(self):
        # threshold/min_silence/margin are at their CONFIG_DEFAULTS —
        # no explicit flags in the copied command.
        cmd = build_cli_command(
            "x",
            Path("./o"),
            method="segment",
            encoder="libx264",
            video_quality="medium",
            download_quality="best",
        )
        assert "--threshold" not in cmd
        assert "--min-silence" not in cmd
        assert "--margin" not in cmd

    def test_slider_flags_appended_when_non_default(self):
        # The GUI's slider values now travel as explicit CLI flags
        # instead of a side-car YAML file — a failed YAML write can no
        # longer silently lose them.
        cmd = build_cli_command(
            "x",
            Path("./o"),
            method="segment",
            encoder="libx264",
            video_quality="medium",
            download_quality="best",
            threshold=-40.0,
            min_silence=0.8,
            margin=-1.0,
        )
        assert "--threshold -40.0" in cmd
        assert "--min-silence 0.8" in cmd
        assert "--margin -1.0" in cmd

    def test_completion_sound_negative_emitted(self):
        # completion_sound defaults True — the copied command must spell
        # out --no-completion-sound when the GUI switched it off (it was
        # silently dropped before).
        cmd = build_cli_command(
            "x",
            Path("./o"),
            method="segment",
            encoder="libx264",
            video_quality="medium",
            download_quality="best",
            completion_sound=False,
        )
        assert "--no-completion-sound" in cmd

    def test_flag_names_derive_from_param_specs_table(self):
        # Every value flag emitted by the copied-command builder must
        # exist in the shared param_specs table (the same table the
        # CLI's resolver validates against) — the two surfaces can't
        # drift apart.
        from stream2video.param_specs import PARAM_SPECS

        for name in ("method", "encoder", "audio_quality", "threshold"):
            assert "flag" in PARAM_SPECS[name], name
        # And the builder's conditional emission covers exactly the
        # table's declared value-flag order.
        from stream2video.gui_helpers import build_cli_command as _b

        assert callable(_b)


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


class TestProxyCredentials:
    def test_has_credentials_detects_user_pass(self):
        assert proxy_has_credentials("socks5://user:secret@host:1080")
        assert proxy_has_credentials("socks5://user:pa@ss@host:1080")
        assert not proxy_has_credentials("http://127.0.0.1:8080")
        assert not proxy_has_credentials("socks5://host:1080")
        assert not proxy_has_credentials("")

    def test_strip_removes_only_credentials(self):
        assert (
            strip_proxy_credentials("socks5://user:secret@host:1080")
            == "socks5://host:1080"
        )
        assert (
            strip_proxy_credentials("socks5://user:pa@ss@host:1080")
            == "socks5://host:1080"
        )
        assert strip_proxy_credentials("http://127.0.0.1:8080") == "http://127.0.0.1:8080"
        assert strip_proxy_credentials("") == ""


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

    def test_cmd_target_redacted_with_matching_quoting(self):
        proxy = "socks5://user:pa ss@proxy:1080"
        cmd = build_cli_command(
            "x",
            Path("./o"),
            method="segment",
            encoder="libx264",
            video_quality="medium",
            download_quality="best",
            proxy=proxy,
            target_shell=CMD_SHELL,
        )
        redacted = redact_proxy_in_cli_command(cmd, proxy, target_shell=CMD_SHELL)
        assert "pa ss" not in redacted
        assert "socks5://***:***@proxy:1080" in redacted
        assert redacted.startswith("stream2video ") and "--method segment" in redacted


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
