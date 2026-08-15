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
        _write_user_defaults(isolated_defaults, {"proxy": "http://127.0.0.1:8080", "proxy_active": True})
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
            runner.invoke(cli_mod.app, [str(src), "-o", str(tmp_path / "o"), "--log-format", "json"])
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
            runner.invoke(
                cli_mod.app, ["--doctor", "--log-format", "json"], catch_exceptions=False
            )
        assert cli_mod._JSON_LOG_MODE is False


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
