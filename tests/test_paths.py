"""Tests for stream2video.paths — per-video project directory helpers."""

import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

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

    def test_old_target_survives_failed_swap(self, monkeypatch):
        """Audit #5: if the atomic swap fails (locked target, disk
        error), the previous good copy must remain in place AND the
        source must be restored for the user's retry."""
        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            src = out / "video1.mp4"
            src.write_text("new")
            project = out / "video1"
            project.mkdir()
            existing = project / "video1.mp4"
            existing.write_text("old")

            def _boom(_src, _dst):
                raise OSError("target locked by another process")

            monkeypatch.setattr(shutil, "move", _boom)
            with pytest.raises(OSError):
                move_into_project(src, project)
            monkeypatch.undo()
            assert existing.read_text() == "old", "old target must survive a failed move"
            assert src.exists(), "source must be restored for the retry"
            assert src.read_text() == "new"

    def test_no_tmp_siblings_left_after_success(self):
        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            src = out / "video1.mp4"
            src.write_text("data")
            project = out / "video1"
            move_into_project(src, project)
            leftovers = [p for p in project.iterdir() if p.name != "video1.mp4"]
            assert leftovers == [], f"temp siblings leaked: {leftovers}"

    def test_swap_failure_after_move_restores_source(self, monkeypatch):
        """Audit #5: the failure point that killed the old copy used to
        be the pre-move unlink. Now the old target is only replaced via
        os.replace AFTER the new file is fully staged — simulate a
        failure exactly there and verify both files survive."""
        import stream2video.paths as paths_mod

        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            src = out / "video1.mp4"
            src.write_text("new")
            project = out / "video1"
            project.mkdir()
            existing = project / "video1.mp4"
            existing.write_text("old")

            real_move = paths_mod.shutil.move

            def _fail_replace(_a, _b):
                raise OSError("swap refused")

            monkeypatch.setattr(paths_mod.shutil, "move", real_move)
            monkeypatch.setattr(paths_mod.os, "replace", _fail_replace)
            with pytest.raises(OSError):
                move_into_project(src, project)
            monkeypatch.undo()
            assert existing.read_text() == "old"
            assert src.exists() and src.read_text() == "new"
            assert not list(project.glob("*.tmp-*")), "staged temp must be cleaned/restored"


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
        """Case-insensitive FS: two casings of one file share a key.

        Windows-only by construction: on a case-SENSITIVE filesystem
        (Linux CI) ``CLIP.MP4`` and ``clip.mp4`` are different names,
        and ``os.path.normcase`` — the production normaliser — is the
        identity function there, so the production code correctly
        treats them as distinct sources (different hashes). The old
        guard ``str(upper).lower() == str(video).lower()`` was a
        tautology (lowering erases exactly the difference under test),
        so the assertion ran on Linux and compared two legitimately
        different keys.
        """
        import sys

        if sys.platform != "win32":
            pytest.skip("case-insensitive filesystem behaviour is Windows-only")
        with TemporaryDirectory() as tmp:
            video = Path(tmp) / "clip.mp4"
            video.write_text("x")
            upper = Path(tmp) / "CLIP.MP4"
            # On NTFS the differently-cased name resolves to the SAME
            # file (upper.exists() must be True for the test to mean
            # anything — guard against a case-sensitive volume).
            if not upper.exists():
                pytest.skip("volume is case-sensitive; nothing to assert")
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


class TestDownloadedEpochStripping:
    """yt-dlp ``<id>-<epoch>.mp4`` downloads must get a stable per-URL identity.

    The ``%(epoch)s`` outtmpl field makes the raw filename different on
    every run; without epoch stripping the project dir and every cache
    keyed on the artifact stem fork per re-download of the same URL
    (silence re-detection + WAV re-extraction every run)."""

    def test_epochless_strips_only_trailing_10_digit_epoch(self):
        from stream2video.paths import _epochless

        assert _epochless("vid123-1755000000") == "vid123"
        assert _epochless("vid123") == "vid123"
        assert _epochless("vid123-12345") == "vid123-12345"  # not 10 digits
        assert _epochless("vid123-12345678901") == "vid123-12345678901"  # 11 digits
        assert _epochless("my-clip-1755000000") == "my-clip"
        assert _epochless("-1755000000") == "-1755000000"  # id empty after strip → keep

    def test_epochless_strips_run_token_suffix(self):
        """Audit round 24 P2: the outtmpl gained a per-run token
        (``<id>-<epoch>-<token>``, 8 lowercase hex); the identity strip
        must remove both suffixes. Non-hex / wrong-length tokens are not
        stripped."""
        from stream2video.paths import _epochless

        assert _epochless("vid123-1755000000-a1b2c3d4") == "vid123"
        assert _epochless("vid123-1755000000-abcdef01") == "vid123"
        # Legacy single-suffix names still strip.
        assert _epochless("vid123-1755000000") == "vid123"
        # Token with non-hex chars / wrong length → keep whole name.
        assert _epochless("vid123-1755000000-zzzzzzzz") == "vid123-1755000000-zzzzzzzz"
        assert _epochless("vid123-1755000000-a1b2c3d4e5") == "vid123-1755000000-a1b2c3d4e5"
        # Uppercase hex is not the token format we generate → keep.
        assert _epochless("vid123-1755000000-A1B2C3D4") == "vid123-1755000000-A1B2C3D4"
        # Empty id after strip → keep.
        assert _epochless("-1755000000-a1b2c3d4") == "-1755000000-a1b2c3d4"

    def test_apply_per_video_dir_moves_download_to_epochless_name(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "vid123-1755000000.mp4"
            src.write_text("data")
            out = root / "out"
            project, moved = apply_per_video_dir(out, src, is_downloaded=True)
            assert project == out / "vid123"
            assert moved == project / "vid123.mp4"
            assert moved.read_text() == "data"
            assert not src.exists()

    def test_downloaded_identity_stable_across_epochs(self):
        """Two runs of the same URL (different epochs) must land in the
        same project dir with the same artifact identity, so the silence
        / WAV / resume caches are reused instead of missed."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "out"
            src1 = root / "vid123-1755000000.mp4"
            src1.write_text("a")
            project1, moved1 = apply_per_video_dir(out, src1, is_downloaded=True)
            id1 = artifact_stem(moved1)
            src2 = root / "vid123-1755000001.mp4"
            src2.write_text("b")
            project2, moved2 = apply_per_video_dir(out, src2, is_downloaded=True)
            assert project1 == project2 == out / "vid123"
            assert artifact_stem(moved2) == id1
            assert moved2.read_text() == "b", "fresh download must replace the old one"

    def test_local_files_keep_path_hash_project_names(self):
        """The epoch strip must not collapse same-named LOCAL files in
        different directories into one project dir."""
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
            assert out_a.name.startswith("clip_")
            assert out_b.name.startswith("clip_")

    def test_flat_mode_renames_download_to_epochless_name(self):
        """Audit round 24 P6: per_video_dir=False must still give a
        downloaded source its stable epochless name — the artifact stem
        and every cache key on the RAW filename, so a per-run
        ``<id>-<epoch>-<token>`` name would fork the identity on every
        re-download even in flat mode."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "vid123-1755000000-a1b2c3d4.mp4"
            src.write_text("data")
            out = root / "out"
            out.mkdir()
            project, moved = apply_per_video_dir(out, src, is_downloaded=True, per_video_dir=False)
            assert project == out
            assert moved == out / "vid123.mp4"
            assert moved.read_text() == "data"
            assert not src.exists()

    def test_flat_mode_renames_legacy_epoch_name_too(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "vid123-1755000000.mp4"
            src.write_text("data")
            out = root / "out"
            out.mkdir()
            project, moved = apply_per_video_dir(out, src, is_downloaded=True, per_video_dir=False)
            assert project == out
            assert moved == out / "vid123.mp4"
            assert moved.read_text() == "data"
            assert not src.exists()

    def test_flat_mode_keeps_local_files_untouched(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "clip.mp4"
            src.write_text("data")
            out = root / "out"
            out.mkdir()
            project, moved = apply_per_video_dir(out, src, is_downloaded=False, per_video_dir=False)
            assert project == out
            assert moved == src
            assert src.exists()

    def test_flat_mode_epochless_name_is_idempotent(self):
        """A download that already landed under its epochless name must
        not be moved again (guard ``video_path != dest``)."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "out"
            out.mkdir()
            src = out / "vid123.mp4"
            src.write_text("data")
            project, moved = apply_per_video_dir(out, src, is_downloaded=True, per_video_dir=False)
            assert project == out
            assert moved == src
            assert (out / "vid123.mp4").read_text() == "data"


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
        """The sensitive-path list wins even over a forged marker.

        Patches the ``_user_home`` seam (not ``Path.home`` directly):
        on Python 3.13.15 the monkeypatch of ``Path.home`` no longer
        reaches the guard on CI, so the forged-marker scenario saw the
        delete allowed. The seam is version-stable.
        """
        from stream2video import paths as paths_mod

        with TemporaryDirectory() as tmp:
            marked = Path(tmp) / "video1"
            marked.mkdir()
            mark_project_dir(marked)
            monkeypatch.setattr(paths_mod, "_user_home", lambda: marked)
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
