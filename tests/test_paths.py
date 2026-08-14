"""Tests for stream2video.paths — per-video project directory helpers."""

from pathlib import Path
from tempfile import TemporaryDirectory

from stream2video.paths import (
    PROJECT_MARKER_FILENAME,
    RECENT_NAME_MAX,
    add_recent_project,
    apply_per_video_dir,
    artifact_stem,
    ensure_project_dir,
    is_marked_project_dir,
    is_sensitive_delete_target,
    mark_project_dir,
    move_into_project,
    project_dir,
    prune_recent_projects,
    source_path_key,
    truncate_recent_name,
    validate_project_delete,
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


class TestSourceArtifactKey:
    """source_path_key / artifact_stem — per-source identity for artifacts."""

    def test_same_stem_different_dirs_get_different_keys(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = root / "channel_a" / "clip.mp4"
            b = root / "channel_b" / "clip.mp4"
            a.parent.mkdir()
            b.parent.mkdir()
            a.write_text("a")
            b.write_text("b")
            assert source_path_key(a) != source_path_key(b)
            assert artifact_stem(a) != artifact_stem(b)
            assert artifact_stem(a).startswith("clip_")
            assert artifact_stem(b).startswith("clip_")

    def test_same_file_key_is_stable_across_calls(self):
        with TemporaryDirectory() as tmp:
            video = Path(tmp) / "clip.mp4"
            video.write_text("x")
            assert source_path_key(video) == source_path_key(video)
            assert artifact_stem(video) == artifact_stem(video)

    def test_same_file_key_stable_across_casing_on_windows(self):
        with TemporaryDirectory() as tmp:
            video = Path(tmp) / "clip.mp4"
            video.write_text("x")
            upper = Path(tmp) / "CLIP.MP4"
            # Same file viewed via a different-cased name must not fork
            # the key (the FS is case-insensitive on Windows).
            if upper.exists() or str(upper).lower() == str(video).lower():
                assert source_path_key(upper) == source_path_key(video)


class TestSameStemIndependence:
    """Regression: two local files sharing a name in different dirs must
    get independent project dirs and caches instead of overwriting each
    other's artifacts."""

    def test_apply_per_video_dir_gives_distinct_project_dirs(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = root / "channel_a" / "clip.mp4"
            b = root / "channel_b" / "clip.mp4"
            a.parent.mkdir()
            b.parent.mkdir()
            a.write_text("aaa")
            b.write_text("bbb")
            out = root / "out"

            out_a, _ = apply_per_video_dir(out, a, is_downloaded=False)
            out_b, _ = apply_per_video_dir(out, b, is_downloaded=False)
            assert out_a != out_b
            assert out_a.is_dir() and out_b.is_dir()
            assert out_a.parent == out_b.parent == out

    def test_caches_and_output_names_are_independent(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = root / "channel_a" / "clip.mp4"
            b = root / "channel_b" / "clip.mp4"
            a.parent.mkdir()
            b.parent.mkdir()
            a.write_text("aaa")
            b.write_text("bbb")
            out = root / "out"

            from stream2video.silence.cache import (
                build_resume_cache_path,
                build_silence_cache_path,
                build_wav_cache_path,
            )

            wav_a, wav_b = build_wav_cache_path(a, out), build_wav_cache_path(b, out)
            cache_a, cache_b = build_silence_cache_path(a, out), build_silence_cache_path(b, out)
            resume_a, resume_b = build_resume_cache_path(a, out), build_resume_cache_path(b, out)
            assert wav_a != wav_b
            assert cache_a != cache_b
            assert resume_a != resume_b
            # Every artifact for one source shares the same identifier.
            assert wav_a.name.startswith(artifact_stem(a))
            assert cache_a.name.startswith(artifact_stem(a))
            assert resume_a.name.startswith(artifact_stem(a))


class TestProjectMarker:
    """mark_project_dir / is_marked_project_dir — the app-owns-this-dir claim."""

    def test_mark_writes_marker_file(self):
        with TemporaryDirectory() as tmp:
            d = Path(tmp) / "video1"
            d.mkdir()
            mark_project_dir(d)
            assert (d / PROJECT_MARKER_FILENAME).is_file()

    def test_unmarked_dir_is_not_marked(self):
        with TemporaryDirectory() as tmp:
            assert not is_marked_project_dir(Path(tmp))

    def test_missing_dir_is_not_marked(self):
        assert not is_marked_project_dir(Path("/nonexistent/stream2video/xyz"))

    def test_foreign_marker_content_is_not_accepted(self):
        """A same-named file with foreign content must not mark the dir."""
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / PROJECT_MARKER_FILENAME).write_text(
                '{"app": "other", "kind": "project_dir"}', encoding="utf-8"
            )
            assert not is_marked_project_dir(d)

    def test_malformed_marker_is_not_accepted(self):
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / PROJECT_MARKER_FILENAME).write_text("not json", encoding="utf-8")
            assert not is_marked_project_dir(d)

    def test_ensure_project_dir_marks_created_dir(self):
        """The pipeline's project-dir creation funnel stamps the marker."""
        with TemporaryDirectory() as tmp:
            d = ensure_project_dir(Path(tmp), "video1", True)
            assert is_marked_project_dir(d)


class TestSensitiveDeleteTarget:
    """is_sensitive_delete_target — never-rmtree paths (defence in depth)."""

    def test_drive_root_is_sensitive(self):
        assert is_sensitive_delete_target(Path(Path.cwd().anchor))

    def test_home_is_sensitive(self):
        assert is_sensitive_delete_target(Path.home())

    def test_user_profile_subdirs_are_sensitive(self):
        assert is_sensitive_delete_target(Path.home() / "Desktop")
        assert is_sensitive_delete_target(Path.home() / "Downloads")

    def test_app_root_is_sensitive(self):
        from stream2video.paths import __file__ as paths_file

        assert is_sensitive_delete_target(Path(paths_file).parent.parent)

    def test_ordinary_subdir_is_not_sensitive(self):
        with TemporaryDirectory() as tmp:
            assert not is_sensitive_delete_target(Path(tmp))


class TestValidateProjectDelete:
    """The GUI's delete guard — regression net for arbitrary-path rmtree."""

    def test_rejects_missing_path(self):
        ok, _ = validate_project_delete("/nonexistent/stream2video/xyz")
        assert not ok

    def test_rejects_foreign_directory(self):
        """Regression: a foreign path planted in recent_projects (e.g. a
        swapped settings.json) must never pass the guard, so it can never
        reach shutil.rmtree()."""
        with TemporaryDirectory() as tmp:
            foreign = Path(tmp) / "user_data"
            foreign.mkdir()
            (foreign / "precious.txt").write_text("keep me", encoding="utf-8")
            ok, reason = validate_project_delete(foreign)
            assert not ok
            assert "not a project directory" in reason
            assert (foreign / "precious.txt").read_text() == "keep me"
            assert foreign.is_dir()

    def test_rejects_sensitive_target_even_with_marker(self, monkeypatch):
        """The sensitive-path list wins even over a forged marker."""
        with TemporaryDirectory() as tmp:
            marked = Path(tmp) / "video1"
            marked.mkdir()
            mark_project_dir(marked)
            monkeypatch.setattr(Path, "home", classmethod(lambda cls: marked))
            ok, reason = validate_project_delete(marked)
            assert not ok
            assert "system or user" in reason
            assert marked.is_dir()

    def test_rejects_dotdot_trick_to_foreign_dir(self):
        """'..' in the stored string must not bypass the guard — the
        resolved (not the raw) path is validated."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = ensure_project_dir(root, "video1", True)
            foreign = root / "foreign"
            foreign.mkdir()
            (foreign / "keep.txt").write_text("keep me", encoding="utf-8")
            trick = str(project / ".." / "foreign")
            ok, _ = validate_project_delete(trick)
            assert not ok
            assert (foreign / "keep.txt").read_text() == "keep me"

    def test_accepts_marked_project_dir(self):
        with TemporaryDirectory() as tmp:
            project = ensure_project_dir(Path(tmp), "video1", True)
            ok, reason = validate_project_delete(project)
            assert ok, reason


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
