"""Tests for stream2video.paths — per-video project directory helpers."""
import pytest
from pathlib import Path
from tempfile import TemporaryDirectory

from stream2video.paths import (
    ensure_project_dir,
    move_into_project,
    project_dir,
)


class TestProjectDir:
    """project_dir() — pure path resolution, no side effects."""

    def test_per_video_dir_false_returns_output_dir(self):
        out = Path("/some/output")
        assert project_dir(out, "myvideo", False) == out

    def test_per_video_dir_true_nests_under_stem(self):
        out = Path("/some/output")
        assert project_dir(out, "myvideo", True) == Path("/some/output/myvideo")

    def test_stem_with_special_chars_preserved(self):
        out = Path("/out")
        # Stems are not validated — yt-dlp may produce stems with hyphens, dots
        assert project_dir(out, "VID-2024.01.15", True) == Path("/out/VID-2024.01.15")


class TestEnsureProjectDir:
    """ensure_project_dir() — creates the directory, returns the path."""

    def test_creates_subdir_when_per_video_dir_true(self):
        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            result = ensure_project_dir(out, "video1", True)
            assert result == out / "video1"
            assert result.is_dir()

    def test_creates_intermediate_parents(self):
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "a" / "b" / "c"
            result = ensure_project_dir(out, "video1", True)
            assert result.is_dir()

    def test_no_op_when_per_video_dir_false(self):
        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            assert ensure_project_dir(out, "video1", False) == out
            assert not (out / "video1").exists()

    def test_existing_subdir_is_preserved(self):
        """Calling twice on the same stem must not raise (exist_ok=True)."""
        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            d1 = ensure_project_dir(out, "video1", True)
            (d1 / "existing.txt").write_text("keep me")
            d2 = ensure_project_dir(out, "video1", True)
            assert d1 == d2
            assert (d2 / "existing.txt").read_text() == "keep me"


class TestMoveIntoProject:
    """move_into_project() — relocate a file into the project directory."""

    def test_moves_file_into_subdir(self):
        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            src = out / "video1.mp4"
            src.write_text("data")
            project = out / "video1"
            result = move_into_project(src, project)
            assert result == project / "video1.mp4"
            assert result.read_text() == "data"
            assert not src.exists()
            assert project.is_dir()

    def test_no_op_if_already_in_project_dir(self):
        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            project = out / "video1"
            project.mkdir()
            src = project / "video1.mp4"
            src.write_text("data")
            result = move_into_project(src, project)
            assert result == src
            assert src.exists()

    def test_target_exists_keeps_existing_removes_source(self):
        """On retry / re-run, an existing target wins (avoids clobbering)."""
        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            src = out / "video1.mp4"
            src.write_text("new")
            project = out / "video1"
            project.mkdir()
            existing = project / "video1.mp4"
            existing.write_text("old")
            result = move_into_project(src, project)
            assert result == existing
            assert result.read_text() == "old"
            assert not src.exists()

    def test_creates_project_dir_if_missing(self):
        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            src = out / "video1.mp4"
            src.write_text("data")
            project = out / "video1"  # does not exist yet
            result = move_into_project(src, project)
            assert project.is_dir()
            assert result == project / "video1.mp4"
