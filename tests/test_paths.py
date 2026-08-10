"""Tests for stream2video.paths — per-video project directory helpers."""

from pathlib import Path
from tempfile import TemporaryDirectory

from stream2video.paths import (
    RECENT_NAME_MAX,
    add_recent_project,
    ensure_project_dir,
    move_into_project,
    project_dir,
    prune_recent_projects,
    truncate_recent_name,
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

    def test_target_exists_replaces_existing_with_source(self):
        """On retry / re-run, the fresh download must win — keeping a stale
        previous-run target would silently process the *old* video."""
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
            assert result.read_text() == "new"
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


class TestAddRecentProject:
    """add_recent_project() — MRU semantics, dedup, capped."""

    def test_empty_list_gets_new_entry(self):
        assert add_recent_project([], "/a/b") == ["/a/b"]

    def test_new_path_goes_to_front(self):
        result = add_recent_project(["/a", "/b", "/c"], "/d")
        assert result == ["/d", "/a", "/b", "/c"]

    def test_existing_path_moves_to_front_dedup(self):
        result = add_recent_project(["/a", "/b", "/c"], "/b")
        assert result == ["/b", "/a", "/c"]

    def test_existing_first_path_is_noop(self):
        result = add_recent_project(["/a", "/b"], "/a")
        assert result == ["/a", "/b"]

    def test_caps_at_max_keep(self):
        result = add_recent_project(["/a", "/b", "/c", "/d"], "/e", max_keep=3)
        assert result == ["/e", "/a", "/b"]

    def test_default_max_keep_is_5(self):
        result = add_recent_project(["/a", "/b", "/c", "/d", "/e"], "/f")
        assert result == ["/f", "/a", "/b", "/c", "/d"]
        assert len(result) == 5

    def test_accepts_path_object(self):
        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "video1"
            target.mkdir()
            result = add_recent_project([], target)
            assert result == [str(target)]
            assert all(isinstance(x, str) for x in result)

    def test_does_not_mutate_input(self):
        original = ["/a", "/b"]
        add_recent_project(original, "/c")
        assert original == ["/a", "/b"]


class TestPruneRecentProjects:
    """prune_recent_projects() — drop entries whose directory is gone."""

    def test_drops_nonexistent_dirs(self):
        with TemporaryDirectory() as tmp:
            existing = Path(tmp) / "exists"
            existing.mkdir()
            result = prune_recent_projects([str(existing), "/nonexistent/path"])
            assert result == [str(existing)]

    def test_keeps_all_existing(self):
        with TemporaryDirectory() as tmp:
            a = Path(tmp) / "a"
            a.mkdir()
            b = Path(tmp) / "b"
            b.mkdir()
            result = prune_recent_projects([str(a), str(b)])
            assert result == [str(a), str(b)]

    def test_drops_non_string_entries(self):
        with TemporaryDirectory() as tmp:
            a = Path(tmp) / "a"
            a.mkdir()
            result = prune_recent_projects([str(a), None, 42, "/missing"])
            assert result == [str(a)]

    def test_empty_list_returns_empty(self):
        assert prune_recent_projects([]) == []

    def test_does_not_mutate_input(self):
        with TemporaryDirectory() as tmp:
            a = Path(tmp) / "a"
            a.mkdir()
            original = [str(a), "/nonexistent"]
            prune_recent_projects(original)
            assert original == [str(a), "/nonexistent"]


class TestTruncateRecentName:
    """truncate_recent_name — display-name truncation for Recent Projects rows."""

    def test_short_text_unchanged(self):
        for text in ("", "a", "video", "x" * RECENT_NAME_MAX):
            assert truncate_recent_name(text, RECENT_NAME_MAX) == text

    def test_long_text_truncated_with_ellipsis(self):
        text = "x" * (RECENT_NAME_MAX + 10)
        result = truncate_recent_name(text, RECENT_NAME_MAX)
        assert len(result) == RECENT_NAME_MAX
        assert result.endswith("\u2026")  # unicode horizontal ellipsis
        assert result == "x" * (RECENT_NAME_MAX - 1) + "\u2026"

    def test_realistic_long_filename(self):
        """The user's actual filename pattern is <id>_compressed_<n>_<m>."""
        long_name = "v2786949142_compressed_4_30_extra_long_suffix.mp4"
        truncated = truncate_recent_name(long_name, 24)
        assert len(truncated) == 24
        # The ellipsis replaces the file extension — that's fine since
        # the tooltip shows the full path; the column doesn't grow.
        assert truncated.endswith("\u2026")

    def test_custom_max_len(self):
        assert truncate_recent_name("abcdef", 3) == "ab\u2026"
        assert truncate_recent_name("abc", 3) == "abc"
        assert truncate_recent_name("", 3) == ""
