"""Tests for stream2video.tools — binary resolution + ffmpeg option fork."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from stream2video.tools import (
    _ffmpeg_major_minor,
    filter_complex_script_args,
    reset_tool_cache,
)


def _version_run(stdout: str):
    """A ``subprocess.run`` stand-in that returns a fixed -version banner."""

    def _run(cmd, **kwargs):
        class _Proc:
            pass

        p = _Proc()
        p.stdout = stdout
        p.stderr = ""
        p.returncode = 0
        return p

    return _run


@pytest.fixture(autouse=True)
def _clear_caches():
    """Both probes cache per-process; every test starts from a clean slate."""
    reset_tool_cache()
    yield
    reset_tool_cache()


class TestFFmpegVersionProbe:
    """_ffmpeg_major_minor — banner parsing must survive every real-world
    spelling (release tags, git hashes, gyan.dev suffixes) or fall back
    to None (→ legacy option), never raise."""

    @pytest.mark.parametrize(
        ("banner", "expected"),
        [
            ("ffmpeg version 8.1.1-full_build-www.gyan.dev Copyright", (8, 1)),
            ("ffmpeg version 9.0.1-essentials_build-www.gyan.dev Copyright", (9, 0)),
            ("ffmpeg version 6.1.1 Copyright (c) 2000", (6, 1)),
            ("ffmpeg version 7.0 Copyright", (7, 0)),
            ("ffmpeg version n7.1.1 Copyright", (7, 1)),
        ],
    )
    def test_parses_release_banners(self, banner, expected):
        with (
            patch("stream2video.tools.ffmpeg_path", return_value="ffmpeg"),
            patch("stream2video.tools.subprocess.run", side_effect=_version_run(banner)),
        ):
            assert _ffmpeg_major_minor() == expected

    def test_git_build_without_version_returns_none(self):
        with (
            patch("stream2video.tools.ffmpeg_path", return_value="ffmpeg"),
            patch("stream2video.tools.subprocess.run", side_effect=_version_run("ffmpeg version ")),
        ):
            assert _ffmpeg_major_minor() is None

    def test_unparseable_banner_returns_none(self):
        with (
            patch("stream2video.tools.ffmpeg_path", return_value="ffmpeg"),
            patch("stream2video.tools.subprocess.run", side_effect=_version_run("garbage")),
        ):
            assert _ffmpeg_major_minor() is None

    def test_spawn_failure_returns_none(self):
        with (
            patch("stream2video.tools.ffmpeg_path", return_value="ffmpeg"),
            patch(
                "stream2video.tools.subprocess.run", side_effect=subprocess.SubprocessError("boom")
            ),
        ):
            assert _ffmpeg_major_minor() is None


class TestFilterComplexScriptArgs:
    """filter_complex_script_args — the 9.x builds removed the legacy flag,
    so the fork must pick ``-/filter_complex`` on >= 7 and the legacy
    spelling everywhere else (including unparseable version)."""

    def _args_for(self, version: tuple[int, int] | None) -> list[str]:
        with patch("stream2video.tools._ffmpeg_major_minor", return_value=version):
            return filter_complex_script_args("graph.txt")

    @pytest.mark.parametrize("version", [(7, 0), (8, 1), (9, 0), (10, 5)])
    def test_modern_builds_use_dash_slash(self, version):
        assert self._args_for(version) == ["-/filter_complex", "graph.txt"]

    @pytest.mark.parametrize("version", [(6, 1), (2, 0), (5, 2)])
    def test_legacy_builds_use_legacy_flag(self, version):
        assert self._args_for(version) == ["-filter_complex_script", "graph.txt"]

    def test_unparseable_version_assumes_legacy(self):
        assert self._args_for(None) == ["-filter_complex_script", "graph.txt"]


class TestResetToolCache:
    def test_reset_includes_version_probe(self):
        """reset_tool_cache must clear the version probe too, otherwise a
        patched-path test would inherit a previously-probed result."""
        with (
            patch("stream2video.tools.ffmpeg_path", return_value="ffmpeg"),
            patch(
                "stream2video.tools.subprocess.run",
                side_effect=_version_run("ffmpeg version 9.0.1"),
            ),
        ):
            first = _ffmpeg_major_minor()
        reset_tool_cache()
        with (
            patch("stream2video.tools.ffmpeg_path", return_value="ffmpeg"),
            patch(
                "stream2video.tools.subprocess.run",
                side_effect=_version_run("ffmpeg version 6.1.1"),
            ),
        ):
            second = _ffmpeg_major_minor()
        assert first == (9, 0)
        assert second == (6, 1)
