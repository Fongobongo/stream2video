"""Tests for cli.py module behaviour (separate from import smoke tests)."""

import logging
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

import pytest
import typer

from stream2video.cli import load_config
from stream2video.cli_resolver import make_resolver


def _make_fake_cut_and_concat(received: dict[str, Any] | None = None):
    """Build the fake ``cut_and_concat`` used across the CLI tests.

    Writes a dummy output file (so the pipeline's output-exists check
    passes) and, when ``received`` is supplied, records the keyword
    arguments the controller forwarded to ``cut_and_concat``.
    """

    def fake_cut_and_concat(video_path, silence_segments, output_path, **kwargs):
        if received is not None:
            received.update(kwargs)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"out")
        return output_path

    return fake_cut_and_concat


class _FakeConsole:
    def print(self, *a, **kw):
        pass


class TestResolverBoolStringCoercion:
    """``resolve()``'s bool kind must not let quoted YAML strings
    (``force: "false"``) fall through Python truthiness to ``True``."""

    def _resolve(self, config: dict, name: str, flag_value):
        return make_resolver(None, config, _FakeConsole()).resolve(name, flag_value)

    def test_quoted_false_resolves_false(self):
        assert self._resolve({"force": "false"}, "force", None) is False

    def test_quoted_true_resolves_true(self):
        assert self._resolve({"force": "true"}, "force", None) is True

    def test_real_bool_passes_through(self):
        assert self._resolve({"force": True}, "force", None) is True
        assert self._resolve({"force": False}, "force", None) is False

    def test_none_falls_through_to_false(self):
        # No config key AND no CLI flag -> default False for force.
        assert self._resolve({}, "force", None) is False

    def test_none_falls_back_to_config_default(self):
        """A missing key OR an explicit None must resolve to the
        CONFIG_DEFAULTS entry, not a hard-coded False — a host/test
        feeding the resolver a partial dict used to silently flip
        True-defaulted flags (gapless_concat / per_video_dir /
        completion_sound) off."""
        from stream2video.config import CONFIG_DEFAULTS

        for name in ("gapless_concat", "per_video_dir", "completion_sound"):
            assert CONFIG_DEFAULTS[name] is True
            assert self._resolve({}, name, None) is True
            assert self._resolve({name: None}, name, None) is True
        # And the False-defaulted ones keep resolving to False.
        assert self._resolve({}, "delete_after", None) is False
        assert self._resolve({"delete_after": None}, "delete_after", None) is False

    def test_garbage_string_rejected(self):
        with pytest.raises(typer.Exit):
            self._resolve({"force": "banana"}, "force", None)


class TestCliMemoryReservePreflight:
    """CLI must refuse to start silence detection when available RAM is below
    ``memory_reserve_mb`` (parity with the GUI pipeline controller).
    """

    def test_exits_before_silence_when_ram_below_reserve(self, tmp_path: Path):
        from typer.testing import CliRunner

        from stream2video.cli import app
        from stream2video.download import DownloadResult

        src = tmp_path / "src.mp4"
        src.write_bytes(b"source")
        cfg = tmp_path / "config.yaml"
        cfg.write_text("memory_reserve_mb: 32000\nper_video_dir: false\n", encoding="utf-8")

        with (
            patch(
                "stream2video.pipeline_controller.download",
                return_value=DownloadResult(src, is_downloaded=False),
            ),
            patch("stream2video.memory._available_ram_mb", return_value=8000.0),
        ):
            result = CliRunner().invoke(
                app,
                [str(src), "-o", str(tmp_path / "out"), "-c", str(cfg)],
                catch_exceptions=False,
            )

        assert result.exit_code == 1
        assert "is below reserve" in result.output

    def test_continues_when_ram_above_reserve(self, tmp_path: Path):
        from typer.testing import CliRunner

        from stream2video.cli import app
        from stream2video.download import DownloadResult

        src = tmp_path / "src.mp4"
        src.write_bytes(b"source")
        out_dir = tmp_path / "out"

        with (
            patch(
                "stream2video.pipeline_controller.download",
                return_value=DownloadResult(src, is_downloaded=False),
            ),
            patch("stream2video.pipeline_controller.load_silence_cache", return_value=[]),
            patch(
                "stream2video.pipeline_controller.cut_and_concat",
                side_effect=_make_fake_cut_and_concat(),
            ),
            patch("stream2video.memory._available_ram_mb", return_value=64 * 1024.0),
            # The controller calls the REAL generate_keep_segments before
            # concat; its internal duration probe needs a fake ffprobe.
            patch("stream2video.concat.get_video_duration", return_value=10.0),
        ):
            result = CliRunner().invoke(
                app,
                [str(src), "-o", str(out_dir), "--no-per-video-dir"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output


class TestLoadConfigProxyValidation:
    """``load_config`` must reject a malformed proxy address with the
    shared format rule (download.validate_proxy_url) — the value is
    stored even while proxy_active is off, so a typo is caught at load
    time instead of dead-ending the first download through it."""

    def test_schemeless_proxy_rejected(self, tmp_path: Path):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text('proxy: "127.0.0.1:8080"\n', encoding="utf-8")
        with pytest.raises(typer.Exit):
            load_config(cfg)

    def test_unknown_scheme_rejected(self, tmp_path: Path):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("proxy: htt://host:8080\n", encoding="utf-8")
        with pytest.raises(typer.Exit):
            load_config(cfg)

    def test_valid_socks5_with_credentials_loads(self, tmp_path: Path):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text(
            "proxy: socks5://user:pass@host:1080\nproxy_active: true\n", encoding="utf-8"
        )
        loaded = load_config(cfg)
        assert loaded["proxy"] == "socks5://user:pass@host:1080"
        assert loaded["proxy_active"] is True

    def test_empty_proxy_loads(self, tmp_path: Path):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text('proxy: ""\n', encoding="utf-8")
        loaded = load_config(cfg)
        assert loaded["proxy"] == ""

    def test_int_proxy_rejected(self, tmp_path: Path):
        # The resolver coerces ints to str ("8080"), but the result still
        # has no scheme — reject at load with the format message.
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("proxy: 8080\n", encoding="utf-8")
        with pytest.raises(typer.Exit):
            load_config(cfg)

    def test_bool_proxy_rejected(self, tmp_path: Path):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("proxy: true\n", encoding="utf-8")
        with pytest.raises(typer.Exit):
            load_config(cfg)


class TestLoadConfigBoolValidation:
    """``load_config`` must reject non-bool values for the bool config keys.

    Quoted YAML strings like ``force: "false"`` parse to the Python string
    ``"false"``, which is truthy — so ``bool("false")`` would return ``True``
    if the value slipped through, inverting the user's intent. The validator
    catches this so downstream code can assume booleans are real booleans.
    """

    def test_bool_default_when_key_absent(self, tmp_path: Path):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("threshold: -30\n")
        loaded = load_config(cfg)
        assert loaded["force"] is False
        assert loaded["delete_after"] is False
        assert loaded["per_video_dir"] is True  # CONFIG_DEFAULTS

    def test_bool_true_passes(self, tmp_path: Path):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("force: true\ndelete_after: true\n")
        loaded = load_config(cfg)
        assert loaded["force"] is True
        assert loaded["delete_after"] is True

    def test_bool_false_passes(self, tmp_path: Path):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("per_video_dir: false\n")
        loaded = load_config(cfg)
        assert loaded["per_video_dir"] is False

    def test_quoted_string_false_rejected(self, tmp_path: Path):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text('force: "false"\n')
        with pytest.raises(typer.Exit):
            load_config(cfg)

    def test_int_rejected_for_bool_key(self, tmp_path: Path):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("delete_after: 1\n")
        with pytest.raises(typer.Exit):
            load_config(cfg)

    def test_quoted_use_crf_false_rejected(self, tmp_path: Path):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text('use_crf: "false"\n')
        with pytest.raises(typer.Exit):
            load_config(cfg)

    def test_invalid_audio_quality_rejected(self, tmp_path: Path):
        # Regression: enum validation must cover ``audio_quality`` —
        # YAML ``audio_quality: garbage`` previously slipped past
        # ``load_config`` and crashed late in ``_audio_bitrate_opts``
        # with an opaque KeyError. ``video_quality`` was already in the
        # list (asymmetry flagged by the v0.4 audit).
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("audio_quality: garbage\n")
        with pytest.raises(typer.Exit):
            load_config(cfg)

    def test_debug_config_dump_masks_proxy_password(self, tmp_path: Path, caplog):
        # Regression: the DEBUG "Final config" dump used to print the
        # whole config dict including the proxy URL with credentials.
        # The proxy value must be masked so secrets never hit the log.
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("proxy: socks5://user:super-secret@host:1080\nproxy_active: true\n")
        with caplog.at_level(logging.DEBUG, logger="stream2video"):
            loaded = load_config(cfg)
        assert loaded["proxy"] == "socks5://user:super-secret@host:1080"
        for record in caplog.records:
            assert "super-secret" not in record.getMessage()
        assert any("socks5://***:***@host:1080" in r.getMessage() for r in caplog.records)


class TestLoadConfigUnknownKeys:
    """Unknown YAML keys must be rejected, not silently ignored (audit
    round 12): ``config.update(file_config)`` used to merge the whole
    file and only known keys were validated, so a typo (``threshhold``)
    or a non-tunable (``log_format`` — CLI-flag only) loaded without
    warning and never had any effect. The rejection names the bad key
    and suggests the nearest valid ones."""

    def test_typo_key_rejected_with_suggestion(self, tmp_path: Path, capsys):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("threshhold: -25\n")
        with pytest.raises(typer.Exit):
            load_config(cfg)
        out = capsys.readouterr().out
        assert "threshhold" in out
        assert "threshold" in out  # nearest-match suggestion

    def test_cli_flag_only_key_gets_migration_message(self, tmp_path: Path, capsys):
        # ``log_format`` looks plausible but is a CLI flag only — it must
        # be surfaced loudly instead of silently no-op'ing. It was also
        # documented as a YAML setting before the audit, so the rejection
        # message must tell the user how to migrate: remove the key, use
        # the ``--log-format`` flag (audit round 13: the generic
        # nearest-match hint suggested ``output_format`` — a Levenshtein
        # neighbour that tunes the container, not logging).
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("log_format: json\n")
        with pytest.raises(typer.Exit):
            load_config(cfg)
        out = capsys.readouterr().out
        assert "log_format" in out
        assert "--log-format" in out  # migration pointer, not a did-you-mean
        assert "output_format" not in out

    def test_multiple_unknown_keys_all_rejected(self, tmp_path: Path):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("threshhold: -25\nencoder_thredz: 4\nvalid_key: 1\n")
        with pytest.raises(typer.Exit):
            load_config(cfg)

    def test_known_keys_still_load(self, tmp_path: Path):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("threshold: -25\nforce: true\n")
        loaded = load_config(cfg)
        assert loaded["threshold"] == -25.0
        assert loaded["force"] is True

    def test_gui_only_keys_rejected_with_dedicated_message(self, tmp_path: Path, capsys):
        """Audit round 15 P2: ``theme`` and ``recent_projects`` are
        GUI-only / session keys — the CLI never applies them, so a YAML
        entry (``theme: banana``, ``recent_projects: 123``) used to load
        silently as a no-op, reproducing the ``log_format`` defect class.
        They must be rejected with a message that says so."""
        for line in ("theme: banana\n", "recent_projects: 123\n", "theme: dark\n"):
            cfg = tmp_path / "cfg.yaml"
            cfg.write_text(line)
            with pytest.raises(typer.Exit):
                load_config(cfg)
            out = capsys.readouterr().out
            assert "Unknown config key" in out
            assert "GUI-only" in out

    def test_non_string_key_rejected_with_clear_message(self, tmp_path: Path, capsys):
        """Audit round 14 P3: YAML allows ``1: value`` — such a key used
        to reach ``sorted()``/``difflib`` and raise an internal TypeError
        instead of a user-facing message."""
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("1: stray\nthreshold: -25\n")
        with pytest.raises(typer.Exit):
            load_config(cfg)
        out = capsys.readouterr().out
        assert "Config keys must be strings" in out

    def test_mixed_int_str_keys_rejected_before_sorted_crash(self, tmp_path: Path):
        """A mix of int and str keys must fail with the key-type message,
        not a TypeError from ``sorted()`` on a mixed-type set."""
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("threshold: -25\n2: stray\n")
        with pytest.raises(typer.Exit):
            load_config(cfg)

    def test_output_dir_valid_string_passes(self, tmp_path: Path):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("output_dir: ./out\n")
        loaded = load_config(cfg)
        assert loaded["output_dir"] == "./out"

    def test_output_dir_non_string_rejected(self, tmp_path: Path, capsys):
        """Audit round 14 P2: ``output_dir: [bad, path]`` used to sail
        past every validator and crash with an internal TypeError at
        ``Path(...)`` — it must fail here with a clear message."""
        for bad in ("[a, b]", "42", "true"):
            cfg = tmp_path / "cfg.yaml"
            cfg.write_text(f"output_dir: {bad}\n")
            with pytest.raises(typer.Exit):
                load_config(cfg)
            out = capsys.readouterr().out
            assert "Invalid output_dir" in out

    def test_output_dir_empty_string_rejected(self, tmp_path: Path, capsys):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text('output_dir: ""\n')
        with pytest.raises(typer.Exit):
            load_config(cfg)
        assert "Invalid output_dir" in capsys.readouterr().out


class TestLoadConfigNonFiniteNumbers:
    """Audit round 15 P1: YAML NaN / ±Infinity values must be rejected
    with a clear message. ``.nan`` parses to float nan, ``1e999``
    overflows to inf — both used to pass ``min <= value <= max`` (all
    comparisons with nan are False) and poison downstream numeric
    consumers (ffmpeg args, cache config, segment/progress math)."""

    def test_yaml_nan_rejected(self, tmp_path: Path, capsys):
        for line in ("threshold: .nan\n", "min_silence: .nan\n", "margin: -nan\n"):
            cfg = tmp_path / "cfg.yaml"
            cfg.write_text(line)
            with pytest.raises(typer.Exit):
                load_config(cfg)
            assert "not a finite number" in capsys.readouterr().out

    def test_yaml_infinity_rejected(self, tmp_path: Path, capsys):
        for line in ("threshold: .inf\n", "min_silence: -.inf\n", "download_timeout: 1e999\n"):
            cfg = tmp_path / "cfg.yaml"
            cfg.write_text(line)
            with pytest.raises(typer.Exit):
                load_config(cfg)
            assert "not a finite number" in capsys.readouterr().out

    def test_cli_float_flag_nan_rejected(self, tmp_path: Path):
        from typer.testing import CliRunner

        from stream2video.cli import app

        src = tmp_path / "src.mp4"
        src.write_bytes(b"source")
        result = CliRunner().invoke(
            app,
            [str(src), "-o", str(tmp_path / "out"), "--threshold", "nan"],
            catch_exceptions=False,
        )
        assert result.exit_code == 1
        assert "Invalid threshold" in result.output


class TestLoadConfigIntKeysRejectFractional:
    """Audit round 13 P3: an INT-typed key with a FRACTIONAL value must be
    rejected, not silently truncated. YAML ``download_timeout: 10.9``
    parsed to float, passed the numeric-range check, then the resolver's
    ``int(value)`` returned 10. The GUI's Advanced field already rejected
    such inputs — YAML now agrees instead of diverging. Parametrized over
    every PARAM_SPECS int/auto_or_int key."""

    def test_every_int_spec_has_coverage(self):
        # Guard: the parametrization must cover every PARAM_SPECS int
        # entry (adding a new int tunable auto-expands, so this stays
        # green by construction — it just documents the invariant).
        from stream2video.param_specs import PARAM_SPECS

        names = tuple(
            n for n, spec in PARAM_SPECS.items() if spec["kind"] in ("int", "auto_or_int")
        )
        assert names  # non-empty; the class below exercises each one

    @pytest.mark.parametrize(
        "key,frac",
        [
            ("download_timeout", "10.9"),
            ("connect_timeout", "30.5"),
            ("no_progress_timeout", "1800.1"),
            ("silence_timeout", "36000.4"),
            ("segment_encode_timeout", "600.75"),
            ("final_concat_timeout", "86400.2"),
            ("stall_kill_timeout", "300.3"),
            ("stall_warning_timeout", "120.9"),
            ("waveform_timeout", "300.6"),
            ("batch_chunk_size", "2.7"),
            ("min_part_bytes", "1024.5"),
            ("memory_reserve_mb", "2048.25"),
            ("rlimit_as_mb", "0.5"),
            ("encoder_threads", "4.2"),
            ("memory_limit_mb", "4096.9"),
        ],
    )
    def test_fractional_value_rejected(self, tmp_path: Path, capsys, key, frac):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text(f"{key}: {frac}\n")
        with pytest.raises(typer.Exit):
            load_config(cfg)
        out = capsys.readouterr().out
        assert key in out
        assert "not an integer" in out

    def test_whole_float_yaml_still_loads_for_float_keys(self):
        # Float-typed keys (threshold / min_silence / margin) keep their
        # fractional value — only INT-typed slots are restricted.
        cfg = load_config(None)  # type: ignore[arg-type]
        assert isinstance(cfg["threshold"], float)

    def test_explicit_keys_surfaced_on_load(self, tmp_path: Path):
        # load_config tags the returned dict with the explicitly-written
        # keys (audit round 13 P1 — feeds apply_preset's per-key override).
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("threshold: -25\nbatch_chunk_size: 33\n")
        loaded = load_config(cfg)
        assert getattr(loaded, "explicit_keys", frozenset()) == frozenset(
            {"threshold", "batch_chunk_size"}
        )

    def test_no_file_explicit_keys_empty(self, tmp_path: Path):
        loaded = load_config(None)  # type: ignore[arg-type]
        assert getattr(loaded, "explicit_keys", frozenset()) == frozenset()


class TestLoadConfigAutoCaseInsensitive:
    """YAML ``auto`` must accept any casing, matching the CLI flag and
    the GUI's Advanced entries — three surfaces, one rule. Regression:
    ``encoder_threads: AUTO`` crashed load_config with "is not a
    number" (float("AUTO")) while --encoder-threads AUTO and the GUI
    field both accepted it."""

    def test_uppercase_auto_yaml_loads(self, tmp_path: Path):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("encoder_threads: AUTO\nmemory_limit_mb: Auto\n")
        loaded = load_config(cfg)
        # Canonical lowercase form for downstream ``== "auto"`` checks.
        assert loaded["encoder_threads"] == "auto"
        assert loaded["memory_limit_mb"] == "auto"

    def test_padded_auto_yaml_loads(self, tmp_path: Path):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text('encoder_threads: " auto "\n')
        loaded = load_config(cfg)
        assert loaded["encoder_threads"] == "auto"

    def test_garbage_string_still_rejected(self, tmp_path: Path):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("encoder_threads: automatic\n")
        with pytest.raises(typer.Exit):
            load_config(cfg)


class TestLoadConfigAutoOrIntQuotedNumbers:
    """A QUOTED number (``encoder_threads: "8"``) on an auto_or_int key
    parses to str in YAML, passes ``float()`` in load_config, and used to
    leak ``8.0`` into the config — the resolver's ``auto_or_int`` path
    then rejected the run with "must be 'auto' or an integer" for a value
    that IS an integer. ``batch_chunk_size: "40"`` never had this problem
    because its CONFIG_DEFAULTS entry is an int. Regression: quoted numbers
    must be coerced back to int, same as the unquoted / GUI / flag paths.
    """

    def test_quoted_numbers_load_as_int(self, tmp_path: Path):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text('encoder_threads: "8"\nmemory_limit_mb: "4096"\nthreshold: "-25"\n')
        loaded = load_config(cfg)
        assert loaded["encoder_threads"] == 8
        assert isinstance(loaded["encoder_threads"], int)
        assert loaded["memory_limit_mb"] == 4096
        assert isinstance(loaded["memory_limit_mb"], int)
        # float-typed keys must NOT be coerced to int — quoted or not,
        # a float-typed slot keeps its float (``-25.0``, not ``-25``).
        assert loaded["threshold"] == -25.0
        assert isinstance(loaded["threshold"], float)

    def test_quoted_number_reaches_cut_and_concat_as_int(self, tmp_path: Path):
        from typer.testing import CliRunner

        from stream2video.cli import app
        from stream2video.download import DownloadResult

        src = tmp_path / "src.mp4"
        src.write_bytes(b"source")
        cfg = tmp_path / "config.yaml"
        cfg.write_text('encoder_threads: "8"\nmemory_limit_mb: "4096"\n', encoding="utf-8")
        received: dict = {}

        with (
            patch(
                "stream2video.pipeline_controller.download",
                return_value=DownloadResult(src, is_downloaded=False),
            ),
            patch("stream2video.pipeline_controller.load_silence_cache", return_value=[]),
            patch(
                "stream2video.pipeline_controller.cut_and_concat",
                side_effect=_make_fake_cut_and_concat(received),
            ),
            patch("stream2video.concat.get_video_duration", return_value=10.0),
        ):
            result = CliRunner().invoke(
                app,
                [
                    str(src),
                    "-o",
                    str(tmp_path / "out"),
                    "-c",
                    str(cfg),
                    "--no-per-video-dir",
                ],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert received["encoder_threads"] == 8
        assert isinstance(received["encoder_threads"], int)
        assert received["memory_limit_mb"] == 4096
        assert isinstance(received["memory_limit_mb"], int)


class TestCliAutoCaseInsensitive:
    """--encoder-threads AUTO / --memory-limit-mb Auto must reach the
    pipeline as the canonical lowercase "auto" (the CLI half of the same
    three-surface rule as TestLoadConfigAutoCaseInsensitive)."""

    def test_uppercase_auto_flag_reaches_cut_and_concat(self, tmp_path: Path):
        from typer.testing import CliRunner

        from stream2video.cli import app
        from stream2video.download import DownloadResult

        src = tmp_path / "src.mp4"
        src.write_bytes(b"source")
        out_dir = tmp_path / "out"
        received: dict = {}

        with (
            patch(
                "stream2video.pipeline_controller.download",
                return_value=DownloadResult(src, is_downloaded=False),
            ),
            patch("stream2video.pipeline_controller.load_silence_cache", return_value=[]),
            patch(
                "stream2video.pipeline_controller.cut_and_concat",
                side_effect=_make_fake_cut_and_concat(received),
            ),
            patch("stream2video.concat.get_video_duration", return_value=10.0),
        ):
            result = CliRunner().invoke(
                app,
                [
                    str(src),
                    "-o",
                    str(out_dir),
                    "--no-per-video-dir",
                    "--encoder-threads",
                    "AUTO",
                    "--memory-limit-mb",
                    "Auto",
                ],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert received["encoder_threads"] == "auto"
        assert received["memory_limit_mb"] == "auto"


class TestCliUseCrf:
    def test_use_crf_flag_reaches_cut_and_concat(self, tmp_path: Path):
        from typer.testing import CliRunner

        from stream2video.cli import app
        from stream2video.download import DownloadResult

        src = tmp_path / "src.mp4"
        src.write_bytes(b"source")
        out_dir = tmp_path / "out"
        received: dict = {}

        with (
            patch(
                "stream2video.pipeline_controller.download",
                return_value=DownloadResult(src, is_downloaded=False),
            ),
            patch("stream2video.pipeline_controller.load_silence_cache", return_value=[]),
            patch(
                "stream2video.pipeline_controller.cut_and_concat",
                side_effect=_make_fake_cut_and_concat(received),
            ),
            # generate_keep_segments (called by the controller before the
            # encode) probes duration via stream2video.concat — fake it.
            patch("stream2video.concat.get_video_duration", return_value=10.0),
        ):
            result = CliRunner().invoke(
                app,
                [
                    str(src),
                    "-o",
                    str(out_dir),
                    "--no-per-video-dir",
                    "--use-crf",
                ],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert received["use_crf"] is True

    def test_use_crf_yaml_reaches_cut_and_concat(self, tmp_path: Path):
        from typer.testing import CliRunner

        from stream2video.cli import app
        from stream2video.download import DownloadResult

        src = tmp_path / "src.mp4"
        src.write_bytes(b"source")
        cfg = tmp_path / "config.yaml"
        cfg.write_text("use_crf: true\nper_video_dir: false\n", encoding="utf-8")
        received: dict = {}

        with (
            patch(
                "stream2video.pipeline_controller.download",
                return_value=DownloadResult(src, is_downloaded=False),
            ),
            patch("stream2video.pipeline_controller.load_silence_cache", return_value=[]),
            patch(
                "stream2video.pipeline_controller.cut_and_concat",
                side_effect=_make_fake_cut_and_concat(received),
            ),
            # generate_keep_segments (called by the controller before the
            # encode) probes duration via stream2video.concat — fake it.
            patch("stream2video.concat.get_video_duration", return_value=10.0),
        ):
            result = CliRunner().invoke(
                app,
                [str(src), "-o", str(tmp_path / "out"), "-c", str(cfg)],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert received["use_crf"] is True


class TestCliLoggingSetup:
    """cli.py must apply the user's --log-level to the console handler, not
    the logger itself. Otherwise the file handler also gets filtered (the
    logger level acts as a global floor) and changing --log-level has no
    visible effect on the console.
    """

    def test_console_handler_is_distinct_from_logger(self):
        from stream2video import cli

        assert cli._console_handler is not None
        assert isinstance(cli._console_handler, logging.Handler)
        assert cli._console_handler is not cli.logger

    def test_console_handler_can_be_releveled_independently(self):
        """Setting a level on the console handler must not change the logger's
        level — the file handler relies on the logger staying open at DEBUG.
        """
        from stream2video import cli

        original_handler_level = cli._console_handler.level
        original_logger_level = cli.logger.level
        try:
            cli._console_handler.setLevel(logging.WARNING)
            assert cli._console_handler.level == logging.WARNING
            assert cli.logger.level == original_logger_level
        finally:
            cli._console_handler.setLevel(original_handler_level)


class TestCliOutputFps:
    def test_output_fps_flag_reaches_cut_and_concat(self, tmp_path: Path):
        from typer.testing import CliRunner

        from stream2video.cli import app
        from stream2video.download import DownloadResult

        src = tmp_path / "src.mp4"
        src.write_bytes(b"source")
        out_dir = tmp_path / "out"
        received: dict = {}

        with (
            patch(
                "stream2video.pipeline_controller.download",
                return_value=DownloadResult(src, is_downloaded=False),
            ),
            patch("stream2video.pipeline_controller.load_silence_cache", return_value=[]),
            patch(
                "stream2video.pipeline_controller.cut_and_concat",
                side_effect=_make_fake_cut_and_concat(received),
            ),
            # generate_keep_segments (called by the controller before the
            # encode) probes duration via stream2video.concat — fake it.
            patch("stream2video.concat.get_video_duration", return_value=10.0),
        ):
            result = CliRunner().invoke(
                app,
                [
                    str(src),
                    "-o",
                    str(out_dir),
                    "--no-per-video-dir",
                    "--output-fps",
                    "30",
                ],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert received["output_fps"] == "30"

    def test_output_fps_yaml_reaches_cut_and_concat(self, tmp_path: Path):
        from typer.testing import CliRunner

        from stream2video.cli import app
        from stream2video.download import DownloadResult

        src = tmp_path / "src.mp4"
        src.write_bytes(b"source")
        cfg = tmp_path / "config.yaml"
        cfg.write_text("output_fps: '60'\nper_video_dir: false\n", encoding="utf-8")
        received: dict = {}

        with (
            patch(
                "stream2video.pipeline_controller.download",
                return_value=DownloadResult(src, is_downloaded=False),
            ),
            patch("stream2video.pipeline_controller.load_silence_cache", return_value=[]),
            patch(
                "stream2video.pipeline_controller.cut_and_concat",
                side_effect=_make_fake_cut_and_concat(received),
            ),
            # generate_keep_segments (called by the controller before the
            # encode) probes duration via stream2video.concat — fake it.
            patch("stream2video.concat.get_video_duration", return_value=10.0),
        ):
            result = CliRunner().invoke(
                app,
                [str(src), "-o", str(tmp_path / "out"), "-c", str(cfg)],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert received["output_fps"] == "60"


class TestCliPerVideoDir:
    """End-to-end test: with ``per_video_dir: true`` in the YAML config,
    the CLI must move the local source (well, NOT copy it — local files
    stay put) and put all generated artifacts (log file, WAV cache,
    silence cache, compressed output) inside ``{output_dir}/{stem}/``.
    """

    @pytest.fixture
    def ffmpeg(self):
        path = shutil.which("ffmpeg")
        if not path:
            pytest.skip("ffmpeg not available")
        return path

    def _make_test_video(self, ffmpeg: str, path: Path):
        cmd = [
            ffmpeg,
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=mono:duration=2",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:r=10",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-t",
            "2",
            str(path),
        ]
        subprocess.run(
            cmd,
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def test_local_file_artifacts_land_in_project_dir(self, ffmpeg):
        from typer.testing import CliRunner

        from stream2video.cli import app

        with TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            src = tmp_p / "myvideo.mp4"
            self._make_test_video(ffmpeg, src)
            out = tmp_p / "out"
            cfg = tmp_p / "config.yaml"
            cfg.write_text("per_video_dir: true\nthreshold: -30\nmin_silence: 0.3\n")

            # Use CliRunner with a local file (no URL) so download() is a
            # passthrough — we test only the project_dir placement logic,
            # not real ffmpeg/yt-dlp downloads.
            runner = CliRunner()
            result = runner.invoke(
                app,
                [str(src), "-o", str(out), "-c", str(cfg), "-e", "libx264", "-m", "segment"],
                catch_exceptions=False,
            )
            assert result.exit_code == 0, f"CLI failed: {result.output}"

            from stream2video.paths import artifact_stem

            stem = artifact_stem(src)
            project = out / stem
            assert project.is_dir(), f"Project dir not created: {project}"

            # The local source itself must stay where the user put it
            # (we don't copy/move local files).
            assert src.exists(), "Local source must not be moved"

            # Log file must live in the project dir.
            log = project / "stream2video.log"
            assert log.exists(), f"Log file not in project dir: {log}"
            # And NOT in the base output_dir.
            assert not (out / "stream2video.log").exists(), (
                "Log file should be in project dir, not base"
            )

            # WAV + JSON cache + compressed output all in project dir.
            assert (project / f"{stem}_audio.wav").exists()
            assert (project / f"{stem}_silence_cache.json").exists()
            assert (project / f"{stem}_compressed.mp4").exists()

            # And none of them in the base output_dir.
            assert not (out / f"{stem}_audio.wav").exists()
            assert not (out / f"{stem}_silence_cache.json").exists()
            assert not (out / f"{stem}_compressed.mp4").exists()


class TestCliPresetResolution:
    """--preset must actually reach the pipeline tunables.

    Regression: the resolver used to be created BEFORE
    ``apply_preset()`` and kept reading the pre-preset config, so
    ``--preset low_memory`` silently failed to enable
    x264_low_memory / batch_chunk_size=20 / low_process_priority.
    """

    def _invoke_and_capture(self, argv: list[str], tmp_path: Path) -> dict:
        from typer.testing import CliRunner

        from stream2video.cli import app
        from stream2video.download import DownloadResult

        src = tmp_path / "src.mp4"
        src.write_bytes(b"source")
        out_dir = tmp_path / "out"
        received: dict = {}

        with (
            patch(
                "stream2video.pipeline_controller.download",
                return_value=DownloadResult(src, is_downloaded=False),
            ),
            patch("stream2video.pipeline_controller.load_silence_cache", return_value=[]),
            patch(
                "stream2video.pipeline_controller.cut_and_concat",
                side_effect=_make_fake_cut_and_concat(received),
            ),
            patch("stream2video.concat.get_video_duration", return_value=10.0),
        ):
            result = CliRunner().invoke(
                app,
                [str(src), "-o", str(out_dir), "--no-per-video-dir", *argv],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        return received

    def test_low_memory_preset_applies_tunables(self, tmp_path: Path):
        received = self._invoke_and_capture(["--preset", "low_memory"], tmp_path)
        assert received["x264_low_memory"] is True
        assert received["batch_chunk_size"] == 20
        assert received["low_process_priority"] is True

    def test_maximum_performance_preset_applies_tunables(self, tmp_path: Path):
        received = self._invoke_and_capture(["--preset", "maximum_performance"], tmp_path)
        assert received["x264_low_memory"] is False
        assert received["memory_limit_mb"] == 0
        assert received["batch_chunk_size"] == 80

    def test_explicit_flag_wins_over_preset(self, tmp_path: Path):
        # ``--preset low_memory --no-low-process-priority`` keeps the
        # preset's other tunables but flips low_process_priority back.
        received = self._invoke_and_capture(
            ["--preset", "low_memory", "--no-low-process-priority"], tmp_path
        )
        assert received["x264_low_memory"] is True
        assert received["batch_chunk_size"] == 20
        assert received["low_process_priority"] is False

    def test_yaml_preset_key_applies_tunables(self, tmp_path: Path):
        # The YAML ``preset: low_memory`` key must be honoured too (the
        # resolver resolves the preset from the config when no --preset
        # flag is passed).
        cfg = tmp_path / "config.yaml"
        cfg.write_text("preset: low_memory\nper_video_dir: false\n", encoding="utf-8")
        received = self._invoke_and_capture(["-c", str(cfg)], tmp_path)
        assert received["x264_low_memory"] is True
        assert received["batch_chunk_size"] == 20
        assert received["low_process_priority"] is True

    def test_explicit_yaml_key_wins_over_preset(self, tmp_path: Path):
        # Audit round 13 P1: a YAML file that picks ``preset: low_memory``
        # AND explicitly writes ``batch_chunk_size: 50`` must run
        # batch_chunk_size=50 (explicit keys win per-key), while the keys
        # the user left unset still pick up the preset's value. Before the
        # fix the preset overlay ran after the merge and won.
        cfg = tmp_path / "config.yaml"
        cfg.write_text("preset: low_memory\nbatch_chunk_size: 50\n", encoding="utf-8")
        received = self._invoke_and_capture(["-c", str(cfg)], tmp_path)
        assert received["batch_chunk_size"] == 50  # explicit YAML wins
        assert received["low_process_priority"] is True  # preset fills unset
        assert received["x264_low_memory"] is True  # preset fills unset

    def test_explicit_flag_still_beats_yaml_and_preset(self, tmp_path: Path):
        # Highest precedence stays --flag: even with an explicit YAML
        # override, an explicit CLI flag wins.
        cfg = tmp_path / "config.yaml"
        cfg.write_text("preset: low_memory\nbatch_chunk_size: 50\n", encoding="utf-8")
        received = self._invoke_and_capture(["-c", str(cfg), "--batch-chunk-size", "99"], tmp_path)
        assert received["batch_chunk_size"] == 99
        assert received["low_process_priority"] is True


class TestCliSilenceFlags:
    """--threshold / --min-silence / --margin must reach the silence
    detector as explicit flags (no side-car YAML needed)."""

    def test_flags_reach_detect_silence(self, tmp_path: Path):
        from typer.testing import CliRunner

        from stream2video.cli import app
        from stream2video.download import DownloadResult

        src = tmp_path / "src.mp4"
        src.write_bytes(b"source")
        out_dir = tmp_path / "out"
        detected: dict = {}

        def fake_detect_silence(video_path, **kwargs):
            detected.update(kwargs)
            return []

        with (
            patch(
                "stream2video.pipeline_controller.download",
                return_value=DownloadResult(src, is_downloaded=False),
            ),
            patch(
                "stream2video.pipeline_controller.detect_silence",
                side_effect=fake_detect_silence,
            ),
            patch("stream2video.pipeline_controller.load_silence_cache", return_value=None),
            patch("stream2video.pipeline_controller.save_silence_cache", lambda *a, **kw: None),
            patch("stream2video.pipeline_controller.get_video_duration", return_value=10.0),
            patch("stream2video.concat.get_video_duration", return_value=10.0),
            patch("stream2video.pipeline_controller.cut_and_concat") as mock_cut,
            patch("stream2video.pipeline_controller.check_memory_reserve", return_value=True),
            patch(
                "stream2video.pipeline_controller.apply_per_video_dir",
                side_effect=lambda o, v, d, per_video_dir=False, namespace=None: (o, v),
            ),
        ):

            def _fake_cut(source, silence_segments, output_video, **kwargs):
                Path(output_video).write_bytes(b"\x00" * 1024)

            mock_cut.side_effect = _fake_cut
            result = CliRunner().invoke(
                app,
                [
                    str(src),
                    "-o",
                    str(out_dir),
                    "--no-per-video-dir",
                    "--threshold",
                    "-40.5",
                    "--min-silence",
                    "0.8",
                    "--margin",
                    "-1.0",
                ],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert detected["threshold"] == -40.5
        assert detected["min_silence"] == 0.8
        assert detected["margin"] == -1.0

    def test_yaml_values_used_without_flags(self, tmp_path: Path):
        # Without the flags the YAML values flow through (existing
        # behaviour preserved).
        from typer.testing import CliRunner

        from stream2video.cli import app
        from stream2video.download import DownloadResult

        src = tmp_path / "src.mp4"
        src.write_bytes(b"source")
        cfg = tmp_path / "config.yaml"
        cfg.write_text("threshold: -50\nmin_silence: 3.5\nmargin: 0.2\n", encoding="utf-8")
        detected: dict = {}

        def fake_detect_silence(video_path, **kwargs):
            detected.update(kwargs)
            return []

        with (
            patch(
                "stream2video.pipeline_controller.download",
                return_value=DownloadResult(src, is_downloaded=False),
            ),
            patch(
                "stream2video.pipeline_controller.detect_silence",
                side_effect=fake_detect_silence,
            ),
            patch("stream2video.pipeline_controller.load_silence_cache", return_value=None),
            patch("stream2video.pipeline_controller.save_silence_cache", lambda *a, **kw: None),
            patch("stream2video.pipeline_controller.get_video_duration", return_value=10.0),
            patch("stream2video.concat.get_video_duration", return_value=10.0),
            patch("stream2video.pipeline_controller.cut_and_concat") as mock_cut,
            patch("stream2video.pipeline_controller.check_memory_reserve", return_value=True),
            patch(
                "stream2video.pipeline_controller.apply_per_video_dir",
                side_effect=lambda o, v, d, per_video_dir=False, namespace=None: (o, v),
            ),
        ):

            def _fake_cut(source, silence_segments, output_video, **kwargs):
                Path(output_video).write_bytes(b"\x00" * 1024)

            mock_cut.side_effect = _fake_cut
            result = CliRunner().invoke(
                app,
                [str(src), "-o", str(tmp_path / "out"), "-c", str(cfg), "--no-per-video-dir"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert detected["threshold"] == -50.0
        assert detected["min_silence"] == 3.5
        assert detected["margin"] == 0.2

    def test_relative_config_output_dir_is_config_relative(self, tmp_path: Path):
        """A relative ``output_dir`` written in the YAML is relative to
        the CONFIG FILE's directory (audit round 28 P8), not the process
        cwd — the same config run from two different cwd's must not
        write into two different trees."""
        from typer.testing import CliRunner

        from stream2video.cli import app
        from stream2video.download import DownloadResult

        conf_dir = tmp_path / "conf"
        conf_dir.mkdir()
        cfg = conf_dir / "config.yaml"
        cfg.write_text("output_dir: rel_out\n", encoding="utf-8")
        src = tmp_path / "src.mp4"
        src.write_bytes(b"source")

        with (
            patch(
                "stream2video.pipeline_controller.download",
                return_value=DownloadResult(src, is_downloaded=False),
            ),
            patch("stream2video.pipeline_controller.detect_silence", return_value=[]),
            patch("stream2video.pipeline_controller.load_silence_cache", return_value=None),
            patch("stream2video.pipeline_controller.save_silence_cache", lambda *a, **kw: None),
            patch("stream2video.pipeline_controller.get_video_duration", return_value=10.0),
            patch("stream2video.concat.get_video_duration", return_value=10.0),
            patch("stream2video.pipeline_controller.cut_and_concat") as mock_cut,
            patch("stream2video.pipeline_controller.check_memory_reserve", return_value=True),
            patch(
                "stream2video.pipeline_controller.apply_per_video_dir",
                side_effect=lambda o, v, d, per_video_dir=False, namespace=None: (o, v),
            ),
        ):

            def _fake_cut(source, silence_segments, output_video, **kwargs):
                Path(output_video).write_bytes(b"\x00" * 1024)

            mock_cut.side_effect = _fake_cut
            result = CliRunner().invoke(
                app,
                [str(src), "-c", str(cfg), "--no-per-video-dir"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert (conf_dir / "rel_out").is_dir(), (
            "relative YAML output_dir resolves against the config dir"
        )

    def test_out_of_range_flag_rejected(self, tmp_path: Path):
        # The CLI flag is range-checked against CONFIG_RANGES exactly
        # like its YAML twin.
        from typer.testing import CliRunner

        from stream2video.cli import app

        src = tmp_path / "src.mp4"
        src.write_bytes(b"source")
        result = CliRunner().invoke(
            app,
            [str(src), "-o", str(tmp_path / "out"), "--threshold", "-80"],
            catch_exceptions=False,
        )
        assert result.exit_code == 1
        assert "Invalid threshold" in result.output


class TestProxyActiveValidation:
    """``proxy_active`` must be validated as a strict boolean like the
    other bool config keys — a quoted ``"false"`` used to be truthy and
    enabled the proxy against the user's intent."""

    def test_quoted_string_false_rejected(self, tmp_path: Path):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text('proxy_active: "false"\nproxy: http://127.0.0.1:8080\n')
        with pytest.raises(typer.Exit):
            load_config(cfg)

    def test_int_rejected(self, tmp_path: Path):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("proxy_active: 1\n")
        with pytest.raises(typer.Exit):
            load_config(cfg)

    def test_real_bool_passes(self, tmp_path: Path):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("proxy_active: false\nproxy: http://127.0.0.1:8080\n")
        loaded = load_config(cfg)
        assert loaded["proxy_active"] is False

    def test_resolver_ignores_non_bool_proxy_active(self):
        # The resolver itself must not treat a truthy string as active
        # (hosts/tests may feed raw dicts bypassing load_config).
        assert (
            make_resolver(
                None, {"proxy_active": "false", "proxy": "http://x:1"}, _FakeConsole()
            ).resolve("proxy", None)
            == ""
        )
        assert (
            make_resolver(
                None, {"proxy_active": True, "proxy": "http://x:1"}, _FakeConsole()
            ).resolve("proxy", None)
            == "http://x:1"
        )


class TestResolverFloatKind:
    """The resolver's ``float`` kind range-checks threshold/min_silence/
    margin exactly like the YAML path."""

    def _resolve(self, config: dict, name: str, flag_value):
        return make_resolver(None, config, _FakeConsole()).resolve(name, flag_value)

    def test_float_value_passes_through(self):
        assert self._resolve({"threshold": -40.5}, "threshold", None) == -40.5

    def test_int_config_value_coerced_to_float(self):
        # YAML ``threshold: -30`` parses as int; the resolver normalises
        # to float for the float-typed PipelineConfig slot.
        assert self._resolve({"threshold": -30}, "threshold", None) == -30.0

    def test_out_of_range_rejected(self):
        with pytest.raises(typer.Exit):
            self._resolve({"threshold": -80}, "threshold", None)
        with pytest.raises(typer.Exit):
            self._resolve({"min_silence": 0.05}, "min_silence", None)

    def test_garbage_rejected(self):
        with pytest.raises(typer.Exit):
            self._resolve({"margin": "banana"}, "margin", None)

    def test_nan_and_infinity_rejected(self):
        """Audit round 15 P1: the resolver must reject non-finite floats
        (hosts/tests can feed raw dicts; ``--threshold nan`` arrives as
        float nan via Typer). The old ``min <= fv <= max`` checks are
        all False for nan, so it used to pass straight into
        PipelineConfig."""
        for key in ("threshold", "min_silence", "margin"):
            for bad in (float("nan"), float("inf"), float("-inf")):
                with pytest.raises(typer.Exit):
                    self._resolve({key: bad}, key, None)


class TestB11CliSanitization:
    """CLI validation must match YAML (CONFIG_RANGES
    ceilings), proxy must be a str, JSON logging must be idempotent
    across repeated main() calls, and the doctor argv scan must not
    swallow the next flag as a --config value."""

    def _resolver(self, config: dict):
        return make_resolver(None, config, _FakeConsole())

    def test_stall_kill_timeout_ceiling_matches_config_ranges(self):
        # YAML twin (stall_kill_timeout: 99999) is rejected by
        # cli_config via CONFIG_RANGES; the CLI flag used to accept it.
        # (With a bare resolver the CLI value rides the same int branch
        # as the config value — the ceiling check is shared.)
        with pytest.raises(typer.Exit) as exc:
            self._resolver({"stall_kill_timeout": 99999}).resolve("stall_kill_timeout", None)
        assert exc.value.exit_code == 1
        # And the boundary value itself is accepted.
        assert (
            self._resolver({"stall_kill_timeout": 3600}).resolve("stall_kill_timeout", None) == 3600
        )

    def test_final_concat_timeout_ceiling_applied(self):
        with pytest.raises(typer.Exit):
            self._resolver({"final_concat_timeout": 604801}).resolve("final_concat_timeout", None)
        assert (
            self._resolver({"final_concat_timeout": 604800}).resolve("final_concat_timeout", None)
            == 604800
        )

    def test_yaml_int_proxy_is_coerced_to_str(self):
        # ``proxy: 8080`` (number in YAML) must not leak into yt-dlp as
        # an int — resolver coerces both CLI and YAML sources to str.
        assert (
            self._resolver({"proxy_active": True, "proxy": 8080}).resolve("proxy", None) == "8080"
        )

    def test_json_handler_installation_is_idempotent(self):
        from stream2video import cli
        from stream2video.json_logging import install_json_handler

        json_handlers_before = [
            h
            for h in cli.logger.handlers
            if type(h).__name__ == "StreamHandler"
            and hasattr(h, "formatter")
            and "JsonFormatter" in type(h.formatter).__name__
        ]
        try:
            install_json_handler(cli.logger, level="INFO")
            second = install_json_handler(cli.logger, level="INFO")
            # A second main() must not leave BOTH handlers attached —
            # that duplicates every record line-by-line on stdout.
            json_handlers = [
                h
                for h in cli.logger.handlers
                if type(h).__name__ == "StreamHandler"
                and hasattr(h, "formatter")
                and "JsonFormatter" in type(h.formatter).__name__
            ]
            assert len(json_handlers) == 1, (
                f"expected exactly 1 JSON handler, got {len(json_handlers)}"
            )
            assert json_handlers[0] is second
        finally:
            for h in list(cli.logger.handlers):
                if (
                    type(h).__name__ == "StreamHandler"
                    and hasattr(h, "formatter")
                    and "JsonFormatter" in type(h.formatter).__name__
                ):
                    cli.logger.removeHandler(h)
            for h in json_handlers_before:
                cli.logger.addHandler(h)

    def test_doctor_config_scan_rejects_flag_like_value(self, monkeypatch):
        # ``-c --doctor`` (value is another flag) must NOT become
        # Path("--doctor") — and must not silently run with the default
        # either: a normal Click run of the same argv rejects "option
        # requires an argument", so the doctor must exit 1 with the same
        # parity error (audit round 23 P7).
        from stream2video import cli

        def fake_run_doctor(cfg, full=False):
            raise AssertionError("doctor must not run on a missing option value")

        monkeypatch.setattr(sys, "argv", ["stream2video", "-c", "--doctor", "--doctor"])
        monkeypatch.setattr(cli, "_run_doctor", fake_run_doctor)
        monkeypatch.setattr(cli, "_JSON_LOG_MODE", False)
        with pytest.raises(typer.Exit) as exc:
            cli._doctor_callback(None, None, True)
        assert exc.value.exit_code == 1

    def test_doctor_scan_rejects_bare_last_option(self, monkeypatch):
        # ``--doctor --log-format`` (option as the LAST token) has no
        # value to scan — the doctor must exit 1 instead of running with
        # the default (audit round 23 P7).
        from stream2video import cli

        monkeypatch.setattr(sys, "argv", ["stream2video", "--doctor", "--log-format"])
        monkeypatch.setattr(cli, "_run_doctor", lambda cfg, full=False: True)
        monkeypatch.setattr(cli, "_JSON_LOG_MODE", False)
        with pytest.raises(typer.Exit) as exc:
            cli._doctor_callback(None, None, True)
        assert exc.value.exit_code == 1
