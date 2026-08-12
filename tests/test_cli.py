"""Tests for cli.py module behaviour (separate from import smoke tests)."""

import logging
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest
import typer

from stream2video.cli import load_config
from stream2video.cli_resolver import make_resolver


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
                "stream2video.cli.download", return_value=DownloadResult(src, is_downloaded=False)
            ),
            patch("stream2video.memory._available_ram_mb", return_value=8000.0),
        ):
            result = CliRunner().invoke(
                app,
                [str(src), "-o", str(tmp_path / "out"), "-c", str(cfg)],
                catch_exceptions=False,
            )

        assert result.exit_code == 1
        assert "Not enough free RAM" in result.output

    def test_continues_when_ram_above_reserve(self, tmp_path: Path):
        from typer.testing import CliRunner

        from stream2video.cli import app
        from stream2video.download import DownloadResult

        src = tmp_path / "src.mp4"
        src.write_bytes(b"source")
        out_dir = tmp_path / "out"

        def fake_cut_and_concat(video_path, silence_segments, output_path, **kwargs):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"out")
            return output_path

        with (
            patch(
                "stream2video.cli.download", return_value=DownloadResult(src, is_downloaded=False)
            ),
            patch("stream2video.cli.load_silence_cache", return_value=[]),
            patch("stream2video.cli.cut_and_concat", side_effect=fake_cut_and_concat),
            patch("stream2video.memory._available_ram_mb", return_value=64 * 1024.0),
        ):
            result = CliRunner().invoke(
                app,
                [str(src), "-o", str(out_dir), "--no-per-video-dir"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output


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


class TestCliUseCrf:
    def test_use_crf_flag_reaches_cut_and_concat(self, tmp_path: Path):
        from typer.testing import CliRunner

        from stream2video.cli import app
        from stream2video.download import DownloadResult

        src = tmp_path / "src.mp4"
        src.write_bytes(b"source")
        out_dir = tmp_path / "out"
        received: dict = {}

        def fake_cut_and_concat(video_path, silence_segments, output_path, **kwargs):
            received.update(kwargs)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"out")
            return output_path

        with (
            patch(
                "stream2video.cli.download", return_value=DownloadResult(src, is_downloaded=False)
            ),
            patch("stream2video.cli.load_silence_cache", return_value=[]),
            patch("stream2video.cli.cut_and_concat", side_effect=fake_cut_and_concat),
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

        def fake_cut_and_concat(video_path, silence_segments, output_path, **kwargs):
            received.update(kwargs)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"out")
            return output_path

        with (
            patch(
                "stream2video.cli.download", return_value=DownloadResult(src, is_downloaded=False)
            ),
            patch("stream2video.cli.load_silence_cache", return_value=[]),
            patch("stream2video.cli.cut_and_concat", side_effect=fake_cut_and_concat),
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

        def fake_cut_and_concat(video_path, silence_segments, output_path, **kwargs):
            received.update(kwargs)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"out")
            return output_path

        with (
            patch(
                "stream2video.cli.download", return_value=DownloadResult(src, is_downloaded=False)
            ),
            patch("stream2video.cli.load_silence_cache", return_value=[]),
            patch("stream2video.cli.cut_and_concat", side_effect=fake_cut_and_concat),
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

        def fake_cut_and_concat(video_path, silence_segments, output_path, **kwargs):
            received.update(kwargs)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"out")
            return output_path

        with (
            patch(
                "stream2video.cli.download", return_value=DownloadResult(src, is_downloaded=False)
            ),
            patch("stream2video.cli.load_silence_cache", return_value=[]),
            patch("stream2video.cli.cut_and_concat", side_effect=fake_cut_and_concat),
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

            project = out / "myvideo"
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
            assert (project / "myvideo_audio.wav").exists()
            assert (project / "myvideo_silence_cache.json").exists()
            assert (project / "myvideo_compressed.mp4").exists()

            # And none of them in the base output_dir.
            assert not (out / "myvideo_audio.wav").exists()
            assert not (out / "myvideo_silence_cache.json").exists()
            assert not (out / "myvideo_compressed.mp4").exists()
