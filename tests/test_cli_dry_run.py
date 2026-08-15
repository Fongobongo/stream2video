"""Tests for --dry-run flag (walks pipeline through silence detection,
prints stats, exits before encode).

The tests mock the controller's download / detect_silence / cut_and_concat
(patched where the controller imports them: ``stream2video.pipeline_controller``)
so no real ffmpeg is needed — what we're checking is the control-flow branch
that short-circuits after step 2 (silence) and never reaches the encode call.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

import stream2video.cli as cli_mod


def _make_video_file(path: Path, size_mb: float = 1.0) -> None:
    """Write a real file with the given size so stat() works."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * int(size_mb * 1024 * 1024))


def _make_silence_segments() -> list:
    """Two silence segments: [2-4] + [6-8] seconds (for a 10s source)."""
    from stream2video.silence import SilenceSegment

    return [SilenceSegment(2.0, 4.0), SilenceSegment(6.0, 8.0)]


def _invoke(argv: list[str]) -> tuple[int, str, MagicMock, MagicMock]:
    """Invoke the CLI with mocked heavy I/O. Returns
    (exit_code, stdout, download_mock, cut_concat_mock)."""
    runner = CliRunner()
    argv = [str(a) for a in argv]

    with (
        patch.object(cli_mod, "_check_ffmpeg", lambda: None),
        # The CLI now orchestrates through PipelineController, so the
        # heavy I/O lives behind the controller's module-level imports
        # (audit #11): patch there, NOT on the cli module.
        patch("stream2video.pipeline_controller.download") as mock_dl,
        patch("stream2video.pipeline_controller.detect_silence") as mock_detect,
        patch("stream2video.pipeline_controller.load_silence_cache", return_value=None),
        patch("stream2video.pipeline_controller.save_silence_cache", lambda *a, **kw: None),
        # src_duration is probed by the controller via its module-level
        # binding of ``stream2video.utils.get_video_duration``; the real
        # generate_keep_segments probes internally via
        # ``stream2video.concat.get_video_duration``.
        patch("stream2video.pipeline_controller.get_video_duration", return_value=10.0),
        patch("stream2video.concat.get_video_duration", return_value=10.0),
        patch("stream2video.pipeline_controller.cut_and_concat") as mock_cut,
        patch("stream2video.pipeline_controller.check_memory_reserve", return_value=True),
        patch(
            "stream2video.pipeline_controller.apply_per_video_dir",
            side_effect=lambda o, v, d, per_video_dir=False: (o, v),
        ),
    ):
        # download() passthrough: return the input path as-is.
        mock_dl.return_value = MagicMock(
            path=Path(argv[0]),  # first positional arg = input_video
            is_downloaded=False,
        )
        mock_detect.return_value = _make_silence_segments()

        # The controller validates the output file exists and is non-empty
        # (audit #4/#8), so the stubbed concat must materialize it.
        def _fake_cut(source, silence_segments, output_video, **kwargs):
            Path(output_video).write_bytes(b"\x00" * 1024)

        mock_cut.side_effect = _fake_cut
        result = runner.invoke(cli_mod.app, argv, catch_exceptions=False)
        return result.exit_code, result.stdout, mock_dl, mock_cut


class TestDryRun:
    """--dry-run fires the silence phase, prints stats, skips encode."""

    def test_dry_run_exits_zero(self, tmp_path: Path) -> None:
        src = tmp_path / "video.mp4"
        _make_video_file(src, size_mb=10.0)
        out_dir = tmp_path / "out"

        code, stdout, mock_dl, mock_cut = _invoke([src, "-o", out_dir, "--dry-run"])

        assert code == 0, f"expected exit 0, got {code}: {stdout}"
        mock_dl.assert_called_once()
        # The encoder must NEVER be called in dry-run mode.
        assert mock_cut.call_count == 0, (
            "cut_and_concat was called during --dry-run — the dry-run "
            "branch must exit before the encode phase"
        )

    def test_dry_run_shows_summary_block(self, tmp_path: Path) -> None:
        src = tmp_path / "video.mp4"
        _make_video_file(src, size_mb=10.0)
        out_dir = tmp_path / "out"

        code, stdout, _, _ = _invoke([src, "-o", out_dir, "--dry-run"])

        assert code == 0
        # Summary block markers.
        assert "Dry" in stdout and "run" in stdout.lower(), (
            f"expected 'Dry-run' banner in output; got:\n{stdout}"
        )
        # Size line (source was 10 MiB).
        assert "10.0 MB" in stdout or "10 MB" in stdout
        # Keep/cut counts (2 silence segments on a 10s source).
        assert "silence" in stdout.lower()

    def test_dry_run_with_alias(self, tmp_path: Path) -> None:
        """Short -n flag is equivalent to --dry-run."""
        src = tmp_path / "video.mp4"
        _make_video_file(src, size_mb=1.0)
        out_dir = tmp_path / "out"

        code, _, _, mock_cut = _invoke([src, "-o", out_dir, "-n"])

        assert code == 0
        assert mock_cut.call_count == 0

    def test_dry_run_off_by_default(self, tmp_path: Path) -> None:
        """Without --dry-run the encode path runs (legacy behaviour)."""
        src = tmp_path / "video.mp4"
        _make_video_file(src, size_mb=1.0)
        out_dir = tmp_path / "out"

        code, _, _, mock_cut = _invoke([src, "-o", out_dir])

        assert code == 0
        # Encode path was invoked (mocked, but the call happened).
        assert mock_cut.call_count == 1

    def test_dry_run_skips_memory_reserve_preflight_for_concat(self, tmp_path: Path) -> None:
        """After --dry-run exits, the concat-phase reserve check must
        not run. We patch check_memory_reserve to count calls.
        """
        src = tmp_path / "video.mp4"
        _make_video_file(src, size_mb=5.0)
        out_dir = tmp_path / "out"

        calls: list[str] = []

        def _counted(reserve_mb: int, phase: str, on_log=None, **kwargs) -> bool:
            calls.append(phase)
            return True

        runner = CliRunner()
        with (
            patch.object(cli_mod, "_check_ffmpeg", lambda: None),
            patch("stream2video.pipeline_controller.download") as mock_dl,
            patch("stream2video.pipeline_controller.detect_silence") as mock_detect,
            patch("stream2video.pipeline_controller.load_silence_cache", return_value=None),
            patch("stream2video.pipeline_controller.save_silence_cache", lambda *a, **kw: None),
            # generate_keep_segments reads duration via ``stream2video.concat.get_video_duration``;
            # the controller probes src duration via its own binding
            # (``stream2video.pipeline_controller.get_video_duration``).
            patch("stream2video.pipeline_controller.get_video_duration", return_value=10.0),
            patch("stream2video.concat.get_video_duration", return_value=10.0),
            patch("stream2video.pipeline_controller.cut_and_concat"),
            patch("stream2video.pipeline_controller.check_memory_reserve", side_effect=_counted),
            patch(
                "stream2video.pipeline_controller.apply_per_video_dir",
                side_effect=lambda o, v, d, per_video_dir=False: (o, v),
            ),
        ):
            mock_dl.return_value = MagicMock(path=src, is_downloaded=False)
            mock_detect.return_value = _make_silence_segments()
            result = runner.invoke(
                cli_mod.app,
                [str(src), "-o", str(out_dir), "--dry-run"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        # Silence-phase pre-flight ran; concat-phase pre-flight did NOT.
        assert "silence detection" in calls, f"silence pre-flight missing: {calls}"
        assert "concat phase" not in calls, f"concat pre-flight ran despite --dry-run: {calls}"

    def test_dry_run_guard_rejects_missing_segment_lists(self, tmp_path: Path) -> None:
        """The dry-run summary path must FAIL LOUDLY when the controller
        violates the dry-run contract (no segment lists) — the old
        ``assert`` here vanished under ``python -O`` and ``fmt_dry_run_summary``
        was fed None. The guard is an explicit if-check + typer.Exit(1)."""
        from stream2video.pipeline_controller import PipelineResult

        src = tmp_path / "video.mp4"
        _make_video_file(src, size_mb=1.0)
        out_dir = tmp_path / "out"

        broken = PipelineResult(
            output_path=None,
            video_path=src,
            src_size_bytes=1024,
            src_duration=10.0,
            dst_size_bytes=0,
            keep_duration=8.0,
            pipeline_seconds=1.0,
            silence_segments=None,
            keep_segments=None,
        )

        runner = CliRunner()
        with (
            patch.object(cli_mod, "_check_ffmpeg", lambda: None),
            # cli.py binds PipelineController at import time — patch the
            # cli module's binding, not pipeline_controller's.
            patch.object(
                cli_mod,
                "PipelineController",
                return_value=MagicMock(run=MagicMock(return_value=broken)),
            ),
        ):
            result = runner.invoke(
                cli_mod.app,
                [str(src), "-o", str(out_dir), "--dry-run"],
                catch_exceptions=False,
            )

        assert result.exit_code == 1, f"expected exit 1, got {result.exit_code}: {result.stdout}"
        assert "Dry-run summary unavailable" in result.stdout
