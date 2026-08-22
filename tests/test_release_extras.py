"""Release-hardening extras: ``--version``, brand rename (silencecut),
config auto-detection, doctor install hints and ``--doctor --full``."""

import json
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

import stream2video
from stream2video import cli as cli_mod

# ---------------------------------------------------------------------------
# --version
# ---------------------------------------------------------------------------


class TestVersionFlag:
    def test_prints_brand_and_package_version(self):
        from stream2video.cli import app

        result = CliRunner().invoke(app, ["--version"])
        assert result.exit_code == 0
        assert f"silencecut {stream2video.__version__}" in result.output

    def test_works_without_input_argument(self):
        """--version is eager: like --doctor it must bypass the required
        INPUT_VIDEO positional instead of erroring with 'missing argument'."""
        from stream2video.cli import app

        result = CliRunner().invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "Missing argument" not in result.output

    def test_version_matches_pyproject(self):
        data = tomllib.loads(
            (Path(stream2video.__file__).parent.parent / "pyproject.toml").read_text(
                encoding="utf-8"
            )
        )
        assert stream2video.__version__ == data["project"]["version"]


# ---------------------------------------------------------------------------
# Branding surfaces
# ---------------------------------------------------------------------------


class TestSilencecutBranding:
    def test_gui_window_title(self):
        src = (Path(stream2video.__file__).parent / "gui.py").read_text(encoding="utf-8")
        assert 'self.title("silencecut")' in src

    def test_console_scripts_renamed_with_legacy_aliases(self):
        root = Path(stream2video.__file__).parent.parent
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        scripts = data["project"]["scripts"]
        # New brand commands point at the unchanged import package...
        assert scripts["silencecut"] == "stream2video.cli:app"
        assert scripts["silencecut-gui"] == "stream2video.gui:main"
        # ...and the historical names stay as aliases (no settings migration).
        assert scripts["stream2video"] == "stream2video.cli:app"
        assert scripts["stream2video-gui"] == "stream2video.gui:main"

    def test_py_typed_marker_shipped(self):
        assert (Path(stream2video.__file__).parent / "py.typed").is_file()
        root = Path(stream2video.__file__).parent.parent
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        assert data["tool"]["setuptools"]["package-data"]["stream2video"] == ["py.typed"]


# ---------------------------------------------------------------------------
# Config auto-detection
# ---------------------------------------------------------------------------


class TestDetectDefaultConfig:
    def test_none_when_no_file(self, tmp_path: Path):
        from stream2video.cli_config import detect_default_config

        assert detect_default_config(tmp_path) is None

    def test_silencecut_yaml_wins(self, tmp_path: Path):
        from stream2video.cli_config import CONFIG_FILENAMES, detect_default_config

        assert CONFIG_FILENAMES[0] == "silencecut.yaml"
        (tmp_path / "stream2video.yaml").write_text("threshold: -35\n", encoding="utf-8")
        (tmp_path / "silencecut.yaml").write_text("threshold: -40\n", encoding="utf-8")
        assert detect_default_config(tmp_path) == tmp_path / "silencecut.yaml"

    def test_legacy_stream2video_yaml_still_detected(self, tmp_path: Path):
        from stream2video.cli_config import detect_default_config

        (tmp_path / "stream2video.yaml").write_text("threshold: -35\n", encoding="utf-8")
        assert detect_default_config(tmp_path) == tmp_path / "stream2video.yaml"

    def test_directory_is_not_a_config(self, tmp_path: Path):
        from stream2video.cli_config import detect_default_config

        (tmp_path / "silencecut.yaml").mkdir()
        assert detect_default_config(tmp_path) is None


class TestCliAutoDetect:
    """End-to-end pickup proof: an INVALID key in the auto-detected file
    must abort the run through the exact same loader an explicit --config
    uses (unknown-key rejection), proving discovery reached the loader."""

    @pytest.mark.parametrize("name", ["silencecut.yaml", "stream2video.yaml"])
    def test_auto_detected_yaml_is_loaded_and_validated(self, tmp_path, monkeypatch, name):
        from stream2video.cli import app

        src = tmp_path / "src.mp4"
        src.write_bytes(b"source")
        (tmp_path / name).write_text("threshhold: -25\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(app, [str(src)])
        assert result.exit_code == 1
        assert "Unknown config key" in result.output
        assert "threshhold" in result.output

    def test_explicit_config_beats_auto_detection(self, tmp_path, monkeypatch):
        from stream2video.cli import app

        src = tmp_path / "src.mp4"
        src.write_bytes(b"source")
        (tmp_path / "silencecut.yaml").write_text("threshhold: -25\n", encoding="utf-8")
        good = tmp_path / "explicit.yaml"
        good.write_text("threshold: -35\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(app, [str(src), "-c", str(good)])
        # The GOOD explicit file was used: no unknown-key rejection here.
        assert "Unknown config key" not in result.output


# ---------------------------------------------------------------------------
# Doctor: install hints + --full log tail
# ---------------------------------------------------------------------------


def _doctor_json(monkeypatch, capsys, *args, **kwargs):
    """Run ``cli._doctor_impl`` in JSON mode and return (ok, records)."""
    monkeypatch.setattr(cli_mod, "_JSON_LOG_MODE", True)
    ok = cli_mod._doctor_impl(*args, **kwargs)
    out = capsys.readouterr().out
    records = [json.loads(line) for line in out.splitlines() if line.strip()]
    return ok, records


class TestDoctorInstallHints:
    def test_missing_ffmpeg_row_carries_install_command(self, monkeypatch, capsys):
        monkeypatch.setattr(cli_mod.shutil, "which", lambda name: None)
        # Skip real probes that would otherwise spawn/retry a missing binary.
        monkeypatch.setattr("stream2video.tools._ffmpeg_major_minor", lambda: None)
        monkeypatch.setattr("stream2video.concat.encoders.check_encoder", lambda name: False)
        ok, records = _doctor_json(monkeypatch, capsys)
        assert ok is False
        labels = [
            r["check"]
            for r in records
            if r.get("doctor") == "check" and "not found in PATH" in r.get("check", "")
        ]
        assert len(labels) >= 1  # ffmpeg and/or ffprobe
        assert all("install with:" in label for label in labels)

    def test_windows_hint_names_winget(self, monkeypatch):
        from stream2video.tools import ffmpeg_install_hint

        monkeypatch.setattr("stream2video.tools.os.name", "nt")
        assert "winget install Gyan.FFmpeg" in ffmpeg_install_hint()

    def test_posix_hint_names_apt(self, monkeypatch):
        from stream2video.tools import ffmpeg_install_hint

        monkeypatch.setattr("stream2video.tools.os.name", "posix")
        monkeypatch.setattr("stream2video.tools.sys.platform", "linux")
        assert "apt install ffmpeg" in ffmpeg_install_hint()


class TestDoctorFullLogTail:
    @pytest.fixture(autouse=True)
    def _fast_probes(self, monkeypatch):
        # The tail tests only exercise the --full section; keep every
        # environment probe instant so the suite stays quiet.
        monkeypatch.setattr("stream2video.tools._ffmpeg_major_minor", lambda: None)

    def test_tail_reports_existing_log(self, tmp_path, monkeypatch, capsys):
        log_dir = tmp_path / "processed_videos"
        log_dir.mkdir()
        (log_dir / "stream2video.log").write_text(
            "line one\nERROR marker-tail-check\n", encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)
        ok, records = _doctor_json(monkeypatch, capsys, None, full=True)
        assert ok is True
        tails = [r for r in records if r.get("doctor") == "log_tail"]
        assert len(tails) == 1
        assert tails[0]["file"].endswith("processed_videos\\stream2video.log") or tails[0][
            "file"
        ].endswith("processed_videos/stream2video.log")
        assert "ERROR marker-tail-check" in tails[0]["lines"]

    def test_no_log_yet_yields_empty_lines(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        ok, records = _doctor_json(monkeypatch, capsys, None, full=True)
        assert ok is True
        tails = [r for r in records if r.get("doctor") == "log_tail"]
        assert len(tails) == 1
        assert tails[0]["lines"] == []

    def test_relative_output_dir_resolves_against_config_dir(self, tmp_path, monkeypatch, capsys):
        cfg_dir = tmp_path / "proj"
        (cfg_dir / "out_rel").mkdir(parents=True)
        (cfg_dir / "cfg.yaml").write_text("output_dir: out_rel\n", encoding="utf-8")
        (cfg_dir / "out_rel" / "stream2video.log").write_text(
            "tail-from-config-dir\n", encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)  # NOT proj/: proves cfg-relative resolution
        ok, records = _doctor_json(monkeypatch, capsys, cfg_dir / "cfg.yaml", full=True)
        assert ok is True
        tails = [r for r in records if r.get("doctor") == "log_tail"]
        assert len(tails) == 1
        assert tails[0]["lines"] == ["tail-from-config-dir"]

    def test_full_off_emits_no_tail_record(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        ok, records = _doctor_json(monkeypatch, capsys, None, full=False)
        assert ok is True
        assert not [r for r in records if r.get("doctor") == "log_tail"]

    def test_rich_mode_prints_tail_section(self, tmp_path, monkeypatch, capsys):
        log_dir = tmp_path / "processed_videos"
        log_dir.mkdir()
        (log_dir / "stream2video.log").write_text("rich-marker-line\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(cli_mod, "_JSON_LOG_MODE", False)
        assert cli_mod._doctor_impl(None, full=True) is True
        out = capsys.readouterr().out
        assert "Last log tail" in out
        assert "rich-marker-line" in out
