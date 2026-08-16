"""GUI <-> CLI parity contract tests (audit round 10).

The copied CLI command must reproduce the GUI's run exactly: the audit
found two silent divergences that these tests pin down --

  * the resource preset was applied to the run config and then
    immediately overwritten by the widget snapshot (a no-op), while the
    copied command carried ``--preset`` which the CLI honours correctly;
  * a proxy switched OFF in the GUI still ran through the proxy in the
    pasted command whenever ``user_defaults.json`` kept
    ``proxy_active: true`` (the command carried no negative flag).

Each test builds the run's config the same way ``_start_pipeline``
does (widget snapshot -> ``apply_preset`` -> widget re-overlay), builds
the copied command the same way ``_copy_cli_command`` does
(``build_cli_command``), tokenises it for the platform's shell and runs
it through the real CLI with the heavy phases mocked, then compares the
captured ``PipelineConfig`` field-by-field.
"""

from __future__ import annotations

import dataclasses
import shlex
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

import stream2video.cli as cli_mod
from stream2video.config import PRESETS, effective_defaults
from stream2video.gui_helpers import build_cli_command
from stream2video.param_specs import CLI_BOOL_FLAG_ORDER, PARAM_SPECS
from stream2video.pipeline_controller import PipelineConfig, PipelineController
from stream2video.pipeline_worker import PipelineWorkerParams, build_pipeline_config_from_snapshot

# Every tunable ``_read_widget_values`` reports. The parity scenarios
# override these; everything else stays at the effective default.
_WIDGET_KEYS = (
    "method",
    "encoder",
    "video_quality",
    "audio_quality",
    "download_quality",
    "software_fallback",
    "x264_preset",
    "encoder_threads",
    "output_fps",
    "output_format",
    "threshold",
    "min_silence",
    "margin",
    "memory_limit_mb",
    "memory_reserve_mb",
    "rlimit_as_mb",
    "download_timeout",
    "connect_timeout",
    "no_progress_timeout",
    "segment_encode_timeout",
    "final_concat_timeout",
    "silence_timeout",
    "stall_kill_timeout",
    "stall_warning_timeout",
    "waveform_timeout",
    "batch_chunk_size",
    "min_part_bytes",
    "force",
    "delete_after",
    "per_video_dir",
    "completion_sound",
    "x264_low_memory",
    "use_crf",
    "gapless_concat",
    "low_process_priority",
    "preset",
    "proxy",
    "proxy_active",
)

# PipelineConfig fields this parity contract compares. input_raw /
# output_dir are positional (always carried, trivially equal) and
# dry_run is controlled by the flag under test, not a tunable.
_COMPARE_FIELDS = tuple(
    f.name
    for f in dataclasses.fields(PipelineConfig)
    if f.name not in ("input_raw", "output_dir", "dry_run")
)


def _widget_values(**overrides: Any) -> dict[str, Any]:
    """The widget snapshot (``_read_widget_values`` shape) at effective
    defaults, with scenario overrides applied."""
    defaults = effective_defaults()
    values = {key: defaults[key] for key in _WIDGET_KEYS}
    values.update(overrides)
    return values


def _gui_run_config(widget_values: dict[str, Any]) -> dict[str, Any]:
    """Replicate ``_start_pipeline``: the widget snapshot IS the run's
    config (audit round 10 -- the preset selection synced its tunables
    into the managed widgets via ``_on_preset_change``, and the preset
    no longer overlays run_config at Start)."""
    return dict(widget_values)


def _gui_pipeline_config(widget_values: dict[str, Any]) -> PipelineConfig:
    """The PipelineConfig the GUI worker would build for this snapshot."""
    run_config = _gui_run_config(widget_values)
    params = PipelineWorkerParams(
        input_raw="in.mp4",
        output_dir=Path("out"),
        method=widget_values["method"],
        encoder=widget_values["encoder"],
        video_quality=widget_values["video_quality"],
        audio_quality=widget_values["audio_quality"],
        download_quality=widget_values["download_quality"],
        force=widget_values["force"],
        per_video_dir=widget_values["per_video_dir"],
        delete_after=widget_values["delete_after"],
        dry_run=True,
    )
    return build_pipeline_config_from_snapshot(params, run_config)


def _copied_argv(widget_values: dict[str, Any]) -> list[str]:
    """Build the copied command the way ``_copy_cli_command`` does and
    split it into argv for the platform's shell quoting rules."""
    cmd = build_cli_command(
        "in.mp4",
        Path("out"),
        method=widget_values["method"],
        encoder=widget_values["encoder"],
        video_quality=widget_values["video_quality"],
        audio_quality=widget_values["audio_quality"],
        download_quality=widget_values["download_quality"],
        software_fallback=widget_values["software_fallback"],
        x264_preset=widget_values["x264_preset"],
        encoder_threads=widget_values["encoder_threads"],
        output_fps=widget_values["output_fps"],
        output_format=widget_values["output_format"],
        threshold=widget_values["threshold"],
        min_silence=widget_values["min_silence"],
        margin=widget_values["margin"],
        force=widget_values["force"],
        delete_after=widget_values["delete_after"],
        x264_low_memory=widget_values["x264_low_memory"],
        use_crf=widget_values["use_crf"],
        gapless_concat=widget_values["gapless_concat"],
        low_process_priority=widget_values["low_process_priority"],
        completion_sound=widget_values["completion_sound"],
        preset=widget_values["preset"],
        memory_limit_mb=widget_values["memory_limit_mb"],
        memory_reserve_mb=widget_values["memory_reserve_mb"],
        rlimit_as_mb=widget_values["rlimit_as_mb"],
        download_timeout=widget_values["download_timeout"],
        connect_timeout=widget_values["connect_timeout"],
        no_progress_timeout=widget_values["no_progress_timeout"],
        segment_encode_timeout=widget_values["segment_encode_timeout"],
        final_concat_timeout=widget_values["final_concat_timeout"],
        silence_timeout=widget_values["silence_timeout"],
        stall_kill_timeout=widget_values["stall_kill_timeout"],
        stall_warning_timeout=widget_values["stall_warning_timeout"],
        waveform_timeout=widget_values["waveform_timeout"],
        batch_chunk_size=widget_values["batch_chunk_size"],
        min_part_bytes=widget_values["min_part_bytes"],
        proxy=widget_values["proxy"] if widget_values["proxy_active"] else "",
        proxy_active=bool(widget_values["proxy_active"]),
        per_video_dir=widget_values["per_video_dir"],
    )
    tokens = cmd.split(" ")
    assert tokens[0] == "stream2video"
    # The builder quotes for the default shell of this platform
    # (PowerShell on Windows); shlex.split implements the same rules
    # for the single-quoted literal strings it emits.
    return shlex.split(" ".join(tokens[1:]))


def _run_cli_captured(argv: list[str]) -> PipelineConfig:
    """Run the real CLI (heavy phases mocked) on a copied command and
    return the PipelineConfig handed to PipelineController."""
    captured: dict[str, Any] = {}

    def _capture(cfg: PipelineConfig, **kwargs: Any) -> PipelineController:
        captured["cfg"] = cfg
        raise RuntimeError("stop after config capture")

    with (
        patch.object(cli_mod, "_check_ffmpeg", lambda: None),
        patch.object(cli_mod, "PipelineController", side_effect=_capture),
        patch("stream2video.pipeline_controller.download") as mock_dl,
        patch("stream2video.pipeline_controller.detect_silence", return_value=[]),
        patch("stream2video.pipeline_controller.load_silence_cache", return_value=None),
        patch("stream2video.pipeline_controller.save_silence_cache", lambda *a, **kw: None),
        patch("stream2video.pipeline_controller.get_video_duration", return_value=10.0),
        patch("stream2video.concat.get_video_duration", return_value=10.0),
        patch("stream2video.pipeline_controller.cut_and_concat"),
        patch("stream2video.pipeline_controller.check_memory_reserve", return_value=True),
    ):
        mock_dl.return_value.path = Path(argv[0])
        mock_dl.return_value.is_downloaded = False
        # The capture raises inside controller construction; typer
        # catches it and exits non-zero -- the config is already
        # captured, which is all this test needs.
        CliRunner().invoke(cli_mod.app, argv, catch_exceptions=False)
        assert captured, "PipelineController was never constructed"
    return captured["cfg"]


def _assert_parity(widget_values: dict[str, Any]) -> None:
    gui_cfg = _gui_pipeline_config(widget_values)
    cli_cfg = _run_cli_captured(_copied_argv(widget_values))
    for name in _COMPARE_FIELDS:
        assert getattr(cli_cfg, name) == getattr(gui_cfg, name), (
            f"parity mismatch on {name!r}: CLI={getattr(cli_cfg, name)!r} "
            f"GUI={getattr(gui_cfg, name)!r}"
        )


@pytest.fixture
def isolated_defaults(tmp_path, monkeypatch):
    """Point user_defaults.json at tmp_path so each test owns its
    effective defaults (a real user's defaults file must not change
    what the parity scenarios compare against)."""
    user_defaults = tmp_path / "user_defaults.json"

    monkeypatch.setattr("stream2video.config._base_dir", lambda: tmp_path)
    return user_defaults


def _write_user_defaults(path: Path, payload: dict[str, Any]) -> None:
    import json

    path.write_text(json.dumps(payload), encoding="utf-8")


class TestParityScenarios:
    def test_defaults_scenario(self):
        _assert_parity(_widget_values())

    @pytest.mark.parametrize("preset", sorted(PRESETS))
    def test_every_preset(self, isolated_defaults, preset):
        """Selecting a preset in the GUI syncs the managed widgets; the
        copied command's --preset must produce the identical config.
        (Audit P1: the preset used to be a no-op in the GUI run while
        the pasted command honoured it -- different configurations.)"""
        values = _widget_values(preset=preset)
        # _on_preset_change pushes the preset's tunables into the
        # widgets/settings; simulate that pre-synced widget state.
        values.update(PRESETS[preset])
        _assert_parity(values)

    def test_preset_with_explicit_checkbox_override(self, isolated_defaults):
        """low_memory selected, then low_process_priority manually
        unchecked -- the explicit post-selection choice wins in BOTH the
        GUI run and the copied command."""
        values = _widget_values(preset="low_memory")
        values.update(PRESETS["low_memory"])
        values["low_process_priority"] = False  # user's explicit override
        _assert_parity(values)

    def test_proxy_off_against_user_default_true(self, isolated_defaults):
        """The audit's proxy scenario: user_defaults keeps
        proxy_active=true; the GUI switched the proxy off for this
        session. The copied command must carry --no-proxy-active and
        the CLI must run WITHOUT the proxy."""
        _write_user_defaults(
            isolated_defaults, {"proxy": "http://127.0.0.1:8080", "proxy_active": True}
        )
        values = _widget_values(proxy="http://127.0.0.1:8080", proxy_active=False)

        argv = _copied_argv(values)
        assert "--no-proxy-active" in argv
        assert "--proxy" not in argv

        cli_cfg = _run_cli_captured(argv)
        gui_cfg = _gui_pipeline_config(values)
        assert gui_cfg.proxy == ""
        assert cli_cfg.proxy == ""

    def test_proxy_on_with_user_default_false(self, isolated_defaults):
        """The mirror case: defaults keep the proxy off; the GUI enables
        it -- the address travels via --proxy in both configurations."""
        values = _widget_values(proxy="socks5://proxy:1080", proxy_active=True)
        argv = _copied_argv(values)
        assert "--proxy" in argv
        assert argv[argv.index("--proxy") + 1] == "socks5://proxy:1080"
        _assert_parity(values)

    def test_proxy_on_empty_address_pins_direct(self, isolated_defaults):
        """GUI checkbox ON but the address field is empty (the user
        cleared it). user_defaults still holds an old address -- the
        paste must not re-activate it, so ``--proxy ''`` pins an empty
        address explicitly."""
        _write_user_defaults(
            isolated_defaults, {"proxy": "http://old-proxy:3128", "proxy_active": True}
        )
        values = _widget_values(proxy="", proxy_active=True)
        argv = _copied_argv(values)
        assert "--proxy" in argv
        assert argv[argv.index("--proxy") + 1] == ""
        cli_cfg = _run_cli_captured(argv)
        assert cli_cfg.proxy == ""

    def test_bool_toggles_diverging_from_user_defaults(self, isolated_defaults):
        """user_defaults flip several bools; the GUI toggles them back to
        factory values -- the copied command must pin the GUI's state."""
        _write_user_defaults(
            isolated_defaults,
            {
                "gapless_concat": False,
                "per_video_dir": False,
                "completion_sound": False,
                "x264_low_memory": True,
            },
        )
        values = _widget_values(
            gapless_concat=True,
            per_video_dir=True,
            completion_sound=True,
            x264_low_memory=False,
        )
        argv = _copied_argv(values)
        assert "--gapless-concat" in argv
        assert "--per-video-dir" in argv
        assert "--completion-sound" in argv
        assert "--no-x264-low-memory" in argv
        _assert_parity(values)

    def test_diverged_tunable_values(self, isolated_defaults):
        values = _widget_values(
            method="cut_then_encode",
            encoder="libx264",
            video_quality="high",
            audio_quality="low",
            download_quality="720p",
            software_fallback="enabled",
            x264_preset="veryfast",
            encoder_threads=4,
            output_fps="30",
            output_format="mp3",
            threshold=-40.0,
            min_silence=0.8,
            margin=1.5,
            memory_limit_mb=4096,
            memory_reserve_mb=1024,
            rlimit_as_mb=2048,
            download_timeout=14400,
            connect_timeout=120,
            no_progress_timeout=600,
            segment_encode_timeout=1200,
            final_concat_timeout=172800,
            silence_timeout=72000,
            stall_kill_timeout=600,
            stall_warning_timeout=30,
            waveform_timeout=900,
            batch_chunk_size=20,
            min_part_bytes=2048,
            force=True,
            delete_after=True,
            use_crf=True,
        )
        _assert_parity(values)


class TestBoolFlagCoverage:
    """Every bool tunable must be pin-able in BOTH directions from the
    copied command; a missing spelling is exactly the proxy regression."""

    @pytest.mark.parametrize("name", sorted(CLI_BOOL_FLAG_ORDER))
    def test_both_directions_resolve(self, isolated_defaults, name):
        from stream2video.cli_resolver import make_resolver

        class _FakeConsole:
            def print(self, *a, **kw):
                pass

        spec = PARAM_SPECS[name]
        assert spec["flag"] and spec.get("flag_off"), name
        if name == "proxy_active":
            # The gate isn't resolved directly -- the proxy kind reads
            # the pin. Both pin directions were covered by the
            # scenario classes above.
            return
        # With ctx=None the resolver reads from the config dict -- feed
        # both bool values through it and verify they pass unchanged.
        for flag_value in (True, False):
            resolver = make_resolver(None, {name: flag_value}, _FakeConsole())
            assert resolver.resolve(name, None) is flag_value


class TestJsonLogStateIsolation:
    """Two CLI invocations in one process must not leak logging state
    (audit P1/P2): a JSON run followed by a rich run kept console.stderr
    and the JSON handler attached before the fix."""

    def test_json_run_then_rich_run_restores_state(self, isolated_defaults, tmp_path):
        import logging

        from stream2video.cli import console, logger
        from stream2video.json_logging import _JsonFormatter

        src = tmp_path / "src.mp4"
        src.write_bytes(b"x")
        runner = CliRunner()

        def _leaked_json_handlers():
            return [
                h
                for h in (*logger.handlers, *logging.getLogger().handlers)
                if isinstance(getattr(h, "formatter", None), _JsonFormatter)
            ]

        with (
            patch.object(cli_mod, "_check_ffmpeg", lambda: None),
            patch("stream2video.pipeline_controller.download", side_effect=OSError("stop")),
        ):
            runner.invoke(
                cli_mod.app, [str(src), "-o", str(tmp_path / "o"), "--log-format", "json"]
            )
            assert cli_mod._JSON_LOG_MODE is False, "JSON mode leaked past main()'s exit"
            assert console.stderr is False, "console.stderr leaked past main()'s exit"
            assert not _leaked_json_handlers(), f"JSON handler leaked: {_leaked_json_handlers()}"
            # The second invocation in rich mode must work the same way.
            result = runner.invoke(cli_mod.app, [str(src), "-o", str(tmp_path / "o")])
            assert result.exit_code != 0  # the patched download still fails
            assert cli_mod._JSON_LOG_MODE is False
            assert console.stderr is False

    def test_eager_doctor_does_not_leak_json_mode(self, isolated_defaults, monkeypatch):
        monkeypatch.setattr("sys.argv", ["stream2video", "--doctor", "--log-format", "json"])
        runner = CliRunner()
        with patch("stream2video.cli._run_doctor", return_value=True):
            result = runner.invoke(cli_mod.app, ["--doctor", "--log-format", "json"])
        assert result.exit_code == 0
        assert cli_mod._JSON_LOG_MODE is False

    def test_eager_doctor_exception_does_not_leak_json_mode(self, isolated_defaults, monkeypatch):
        """An exception inside the diagnostics must not skip the
        _JSON_LOG_MODE reset (the eager path has no main() ``finally``
        to fall back on)."""
        monkeypatch.setattr("sys.argv", ["stream2video", "--doctor", "--log-format", "json"])
        runner = CliRunner()
        with (
            patch("stream2video.cli._run_doctor", side_effect=RuntimeError("doctor boom")),
            pytest.raises(RuntimeError, match="doctor boom"),
        ):
            runner.invoke(cli_mod.app, ["--doctor", "--log-format", "json"], catch_exceptions=False)
        assert cli_mod._JSON_LOG_MODE is False

    def test_doctor_log_format_is_case_insensitive(self, isolated_defaults, monkeypatch):
        """``--log-format JSON`` (uppercase) must enable JSON mode on the
        eager doctor path exactly like lowercase ``json`` does on the
        normal run path (audit P2): the main() validator lowercases before
        matching, so the eager argv scan must too."""
        monkeypatch.setattr("sys.argv", ["stream2video", "--doctor", "--log-format", "JSON"])
        seen: list[bool] = []

        def _capture(cfg):
            seen.append(cli_mod._JSON_LOG_MODE)
            return True

        runner = CliRunner()
        with patch("stream2video.cli._run_doctor", side_effect=_capture):
            result = runner.invoke(cli_mod.app, ["--doctor", "--log-format", "JSON"])
        assert result.exit_code == 0
        # The diagnostics ran WITH json mode active (both spellings agree).
        assert seen == [True]
        # ...and the eager path reset it before returning.
        assert cli_mod._JSON_LOG_MODE is False


class TestEarlyExitLoggingRestore:
    """The logging-state restore must run on EVERY exit, including the
    early ones (missing ffmpeg, bad --log-level). Previously the
    ``try/finally`` began after those checks, so an early failure leaked
    the freshly installed handlers / JSON mode / console.stderr into the
    host process (audit round 12, P1)."""

    def _leaked_json_handlers(self):
        import logging as _logging

        from stream2video.json_logging import _JsonFormatter

        return [
            h
            for h in (*cli_mod.logger.handlers, *_logging.getLogger().handlers)
            if isinstance(getattr(h, "formatter", None), _JsonFormatter)
        ]

    def test_missing_ffmpeg_exits_without_leak(self, isolated_defaults, tmp_path):
        from stream2video.cli import console, logger

        src = tmp_path / "src.mp4"
        src.write_bytes(b"x")
        runner = CliRunner()
        # Fail at the ffmpeg check — BEFORE the try block used to begin.
        with patch.object(cli_mod, "_check_ffmpeg", side_effect=typer.Exit(1)):
            result = runner.invoke(
                cli_mod.app, [str(src), "-o", str(tmp_path / "o"), "--log-format", "json"]
            )
        assert result.exit_code == 1
        assert cli_mod._JSON_LOG_MODE is False, "JSON mode leaked past an early exit"
        assert console.stderr is False, "console.stderr leaked past an early exit"
        assert not self._leaked_json_handlers(), "JSON handler leaked past an early exit"
        assert logger.propagate is True

    def test_bad_log_level_exits_without_leak(self, isolated_defaults, tmp_path):
        from stream2video.cli import console, logger
        from stream2video.cli_helpers import _console_handler

        src = tmp_path / "src.mp4"
        src.write_bytes(b"x")
        runner = CliRunner()
        level_before = _console_handler.level
        with patch.object(cli_mod, "_check_ffmpeg", lambda: None):
            result = runner.invoke(
                cli_mod.app,
                [str(src), "-o", str(tmp_path / "o"), "--log-level", "bogus"],
                catch_exceptions=False,
            )
        assert result.exit_code == 1
        assert "Invalid log level" in result.output
        assert cli_mod._JSON_LOG_MODE is False
        assert console.stderr is False
        assert not self._leaked_json_handlers()
        assert logger.propagate is True
        # The console handler level is snapshotted + restored, so a bad
        # --log-level run can't leave a shifted level for the next run.
        assert _console_handler.level == level_before

    def test_bad_log_level_same_error_json_and_rich(self, isolated_defaults, tmp_path):
        """Audit round 13 P2: a bad ``--log-level`` must produce the SAME
        user-facing error on both log-format paths. Before the fix the
        JSON branch fed the level to ``install_json_handler`` before the
        validation ran, so ``--log-format json`` raised a logging
        ValueError while the rich path printed "Invalid log level"."""
        from typer.testing import CliRunner

        src = tmp_path / "src.mp4"
        src.write_bytes(b"x")
        runner = CliRunner()
        with patch.object(cli_mod, "_check_ffmpeg", lambda: None):
            for fmt in ("rich", "json"):
                result = runner.invoke(
                    cli_mod.app,
                    [
                        str(src),
                        "-o",
                        str(tmp_path / "o"),
                        "--log-format",
                        fmt,
                        "--log-level",
                        "bogus",
                    ],
                )
                assert result.exit_code == 1, (fmt, result.output)
                assert "Invalid log level" in result.output, (
                    f"{fmt}: expected the user-facing message, got: {result.output!r}"
                )
                assert not isinstance(result.exception, ValueError), (
                    f"{fmt}: logging raised instead of the validator"
                )

    def test_rich_session_attaches_console_handler_to_preconfigured_root(self, isolated_defaults):
        """Audit round 13 P3: when the HOST already configured the root
        logger, the rich session must still install the Rich console
        handler (old basicConfig-without-force was a no-op there), and on
        exit the host's handler list must come back INTACT — not closed,
        not dropped (one CLI run inside an embedded host must not break
        the host's logging)."""
        import logging

        from stream2video.cli_helpers import _console_handler, logging_session

        host = logging.StreamHandler()
        root = logging.getLogger()
        saved_handlers = list(root.handlers)
        saved_level = root.level
        root.handlers = [host]
        with logging_session("rich", "INFO") as state:
            # Inside the session the Rich handler is the ONLY root
            # handler — that is what ``--log-level`` acts on.
            assert root.handlers == [_console_handler]
            assert state.file_handler is None
        # Session exit restores the host's list verbatim and does NOT
        # close the host handler (closing it would break the host).
        assert root.handlers == [host]
        assert root.level == saved_level
        assert not host.stream.closed
        root.handlers = saved_handlers
        root.setLevel(saved_level)

    def test_rich_session_detaches_host_app_logger_handlers(self, isolated_defaults):
        """Audit round 14 P2: a host that pre-attached its OWN handler to
        the ``stream2video`` logger (not the root) must not have it fire
        during a rich CLI run — every record would double-log (host
        handler + root Rich handler via propagation) and bypass
        ``--log-level``. The session must detach it for the run's
        duration and restore it verbatim (not closed) on exit."""
        import logging

        from stream2video.cli import logger
        from stream2video.cli_helpers import _console_handler, logging_session

        host = logging.StreamHandler()
        root = logging.getLogger()
        saved_handlers = list(root.handlers)
        saved_level = root.level
        # NOTE: assert membership/closure, not exact list equality —
        # earlier tests in the same pytest process leave caplog's
        # LogCaptureHandlers attached to the app logger, so the restored
        # list legitimately contains them too.
        app_before = list(logger.handlers)
        logger.addHandler(host)
        try:
            with logging_session("rich", "INFO"):
                assert logger.handlers == []
                assert root.handlers == [_console_handler]
            assert host in logger.handlers
            assert not host.stream.closed
            assert set(app_before) <= set(logger.handlers)
        finally:
            logger.removeHandler(host)
            host.close()
            root.handlers = saved_handlers
            root.setLevel(saved_level)

    def test_json_session_replaces_host_app_logger_handlers(self, isolated_defaults):
        """Audit round 14 P2 (JSON branch): during a ``--log-format json``
        run the app logger must carry ONLY the JSON handler — a host
        handler on ``stream2video`` would otherwise dump JSON records into
        the host's own log stream and double-fire every record. On exit
        the host handler comes back intact (not closed)."""
        import logging

        from stream2video.cli import logger
        from stream2video.cli_helpers import logging_session
        from stream2video.json_logging import _JsonFormatter

        host = logging.StreamHandler()
        root = logging.getLogger()
        saved_handlers = list(root.handlers)
        saved_level = root.level
        app_before = list(logger.handlers)
        logger.addHandler(host)
        try:
            with logging_session("json", "INFO"):
                assert len(logger.handlers) == 1
                assert host not in logger.handlers
                assert isinstance(logger.handlers[0].formatter, _JsonFormatter)
                assert logger.propagate is False
            assert host in logger.handlers
            assert not host.stream.closed
            assert set(app_before) <= set(logger.handlers)
        finally:
            logger.removeHandler(host)
            host.close()
            root.handlers = saved_handlers
            root.setLevel(saved_level)

    def test_install_failure_midway_restores_state(self, isolated_defaults, tmp_path):
        """An exception raised INSIDE the session's install section —
        after it has already mutated logging state — must still restore
        everything. The audit's experiment: a fake ``install_json_handler``
        that flips ``logger.propagate`` (and attaches a handler) before
        raising. Pre-fix, the try began *after* the install branches,
        so the ``finally`` never ran and the partial state leaked —
        exactly the "try boundary not where the mutations are" bug class.
        """
        import logging

        from stream2video.cli import console, logger

        src = tmp_path / "src.mp4"
        src.write_bytes(b"x")
        runner = CliRunner()
        propagate_before = logger.propagate

        def _boom(log, level="INFO"):
            # Mutate state, THEN raise — mimicking a handler install that
            # succeeds halfway (propagate flipped, handler attached) and
            # dies before returning.
            log.propagate = False
            log.addHandler(logging.StreamHandler())
            raise RuntimeError("install boom")

        with (
            patch("stream2video.cli_helpers.install_json_handler", _boom),
            pytest.raises(RuntimeError, match="install boom"),
        ):
            runner.invoke(
                cli_mod.app,
                [str(src), "-o", str(tmp_path / "o"), "--log-format", "json"],
                catch_exceptions=False,
            )
        assert logger.propagate is propagate_before
        assert console.stderr is False
        assert cli_mod._JSON_LOG_MODE is False

    def test_session_enter_restores_state_on_install_failure(self, isolated_defaults):
        """Direct unit-level pin of the same boundary: ``__enter__`` itself
        must restore the snapshot when an install step raises after mutating
        state — the exception surfaces from ``.enter()`` with state intact,
        no CLI plumbing in the picture."""
        import logging

        from stream2video.cli import console, logger
        from stream2video.cli_helpers import logging_session

        def _boom(log, level="INFO"):
            log.propagate = False
            log.addHandler(logging.StreamHandler())
            raise RuntimeError("enter boom")

        propagate_before = logger.propagate
        stderr_before = console.stderr
        with (
            patch("stream2video.cli_helpers.install_json_handler", _boom),
            pytest.raises(RuntimeError, match="enter boom"),
        ):
            logging_session("json", "INFO").__enter__()
        assert logger.propagate is propagate_before
        assert console.stderr is stderr_before

    def test_console_level_restored_after_successful_set(self, isolated_defaults, tmp_path):
        """A run that DID apply --log-level must put the handler level
        back, so a following run in the same process starts clean."""
        from stream2video.cli_helpers import _console_handler

        src = tmp_path / "src.mp4"
        src.write_bytes(b"x")
        runner = CliRunner()
        level_before = _console_handler.level
        with (
            patch.object(cli_mod, "_check_ffmpeg", lambda: None),
            patch("stream2video.pipeline_controller.download", side_effect=OSError("stop")),
        ):
            runner.invoke(
                cli_mod.app,
                [str(src), "-o", str(tmp_path / "o"), "--log-level", "WARNING"],
            )
        assert _console_handler.level == level_before


class TestValidatePipelineConfigTypes:
    """validate_pipeline_config must report wrong-typed values instead
    of silently skipping them (audit P2)."""

    def _cfg(self, **overrides):
        from stream2video.pipeline_controller import validate_pipeline_config
        from tests.test_pipeline_controller import _valid_config

        return validate_pipeline_config(_valid_config(**overrides))

    def test_string_timeout_is_an_error(self):
        errors = self._cfg(download_timeout="abc")  # type: ignore[arg-type]
        assert any("download_timeout" in e for e in errors)

    def test_bool_on_int_slot_is_an_error(self):
        errors = self._cfg(batch_chunk_size=True)  # type: ignore[arg-type]
        assert any("batch_chunk_size" in e for e in errors)

    def test_non_bool_toggle_is_an_error(self):
        errors = self._cfg(force=1)  # type: ignore[arg-type]
        assert any("force" in e for e in errors)

    def test_int_on_float_slot_is_an_error(self):
        errors = self._cfg(threshold=-30)  # type: ignore[arg-type]
        assert any("threshold" in e for e in errors)


class TestValidateAdvancedWidgets:
    def test_garbage_entry_rejected(self):
        from stream2video.settings_io import validate_advanced_widgets

        errors = validate_advanced_widgets({"download_timeout": "abc"})
        assert "download_timeout" in errors

    def test_empty_entry_rejected(self):
        from stream2video.settings_io import validate_advanced_widgets

        errors = validate_advanced_widgets({"download_timeout": ""})
        assert "download_timeout" in errors

    def test_out_of_range_rejected(self):
        from stream2video.settings_io import validate_advanced_widgets

        errors = validate_advanced_widgets({"stall_kill_timeout": "999999"})
        assert "stall_kill_timeout" in errors

    def test_invalid_combo_rejected(self):
        from stream2video.settings_io import validate_advanced_widgets

        errors = validate_advanced_widgets({"x264_preset": "ultra-mega"})
        assert "x264_preset" in errors

    def test_auto_and_int_accepted(self):
        from stream2video.settings_io import validate_advanced_widgets

        ok = validate_advanced_widgets(
            {"encoder_threads": "auto", "memory_limit_mb": "4096", "batch_chunk_size": "40"}
        )
        assert ok == {}

    def test_parse_and_validate_agree(self):
        """The Start gate and the run's fallback parse must label the
        same inputs valid/invalid -- they share rules by construction;
        this pins it."""
        from stream2video.settings_io import parse_advanced_widgets, validate_advanced_widgets

        raw = {"encoder_threads": "abc", "download_timeout": "3600"}
        assert "encoder_threads" in validate_advanced_widgets(raw)
        parsed = parse_advanced_widgets(raw, current={"encoder_threads": "auto"})
        assert parsed["encoder_threads"] == "auto"  # the fallback value


class TestLoggingSessionThreadSerialization:
    """Audit round 16 P2 / 19 P2: logging_session mutates GLOBAL logging
    state, so overlapping sessions in different threads would corrupt
    each other. The first fix serialized them under a module-level
    RLock — but the CLI holds its session for the WHOLE run (hours), so
    a second embedded CLI call blocked silently for hours. The lock is
    now acquired NON-blocking: a concurrent session is rejected with an
    explicit RuntimeError instead of hanging."""

    def test_concurrent_session_rejected_with_runtime_error(self, isolated_defaults):
        import logging
        import threading

        from stream2video.cli import logger
        from stream2video.cli_helpers import logging_session

        host_root = logging.StreamHandler()
        host_app = logging.StreamHandler()
        root = logging.getLogger()
        saved_handlers = list(root.handlers)
        saved_level = root.level
        app_before = list(logger.handlers)
        root.handlers = [host_root]
        logger.addHandler(host_app)
        try:
            entered = threading.Event()
            release = threading.Event()
            outcomes: list[str] = []

            def holder() -> None:
                with logging_session("rich", "INFO"):
                    entered.set()
                    release.wait(30)

            def second() -> None:
                entered.wait(30)
                try:
                    with logging_session("json", "INFO"):
                        outcomes.append("entered")  # must NOT happen
                except RuntimeError as exc:
                    outcomes.append(f"rejected: {exc}")

            t1 = threading.Thread(target=holder)
            t2 = threading.Thread(target=second)
            t1.start()
            t2.start()
            t2.join(timeout=30)
            assert not t2.is_alive()
            release.set()
            t1.join(timeout=30)
            assert not t1.is_alive()

            # The second session was rejected up-front — it must never
            # wait for the first (hours-long) run to finish.
            assert len(outcomes) == 1
            assert outcomes[0].startswith("rejected: ")
            assert "another embedded CLI session is active" in outcomes[0]

            # The holder's session restored everything.
            assert root.handlers == [host_root]
            assert host_app in logger.handlers
            assert not host_root.stream.closed
            assert not host_app.stream.closed
            assert set(app_before) <= set(logger.handlers)
        finally:
            release.set()
            logger.removeHandler(host_app)
            host_app.close()
            host_root.close()
            root.handlers = saved_handlers
            root.setLevel(saved_level)

    def test_lock_released_after_exception_inside_session(self, isolated_defaults):
        import logging
        import threading

        from stream2video.cli import logger
        from stream2video.cli_helpers import logging_session

        host_root = logging.StreamHandler()
        host_app = logging.StreamHandler()
        root = logging.getLogger()
        saved_handlers = list(root.handlers)
        saved_level = root.level
        app_before = list(logger.handlers)
        root.handlers = [host_root]
        logger.addHandler(host_app)
        try:
            start = threading.Event()

            def failing_worker() -> None:
                start.wait()
                with (
                    pytest.raises(RuntimeError, match="boom"),
                    logging_session("json", "INFO"),
                ):
                    raise RuntimeError("boom")

            def ok_worker() -> None:
                start.wait()
                with logging_session("rich", "INFO"):
                    pass

            t1 = threading.Thread(target=failing_worker)
            t2 = threading.Thread(target=ok_worker)
            t1.start()
            t2.start()
            start.set()
            t1.join(timeout=30)
            t2.join(timeout=30)
            assert not t1.is_alive() and not t2.is_alive()

            # The exception path releases the lock (finally ran), so the
            # second session could proceed and restore everything.
            assert root.handlers == [host_root]
            assert host_root not in logger.handlers
            assert host_app in logger.handlers
            assert set(app_before) <= set(logger.handlers)
        finally:
            logger.removeHandler(host_app)
            host_app.close()
            host_root.close()
            root.handlers = saved_handlers
            root.setLevel(saved_level)
