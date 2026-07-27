"""Tests for stream2video.pipeline_controller (Этап 10 incremental).

The pipeline controller's run() body still lives in gui._pipeline_worker
because it's deeply intertwined with Tk callback dispatch. These tests
cover the parts that ARE pure:
  * PipelineConfig dataclass shape (immutable, frozen=True)
  * PipelineCallbacks has the documented callables
  * validate_pipeline_config catches common mistakes

A future refactor that extracts run() will populate this module with
state-machine transition tests; this is the skeleton.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from stream2video.pipeline_controller import (
    PipelineCallbacks,
    PipelineCancelled,
    PipelineConcatError,
    PipelineConfig,
    PipelineController,
    PipelineDownloadError,
    PipelineResult,
    PipelineSilenceError,
    validate_pipeline_config,
)


def _valid_config(**overrides) -> PipelineConfig:
    """Build a valid PipelineConfig with optional field overrides."""
    defaults = {
        "input_raw": "video.mp4",
        "output_dir": Path("./out"),
        "method": "segment",
        "encoder": "libx264",
        "video_quality": "medium",
        "audio_quality": "medium",
        "download_quality": "best",
        "software_fallback": "ask",
        "x264_preset": "medium",
        "encoder_threads": "auto",
        "output_fps": "source",
        "output_format": "video",
        "force": False,
        "delete_after": False,
        "per_video_dir": True,
        "threshold": -30.0,
        "min_silence": 2.0,
        "margin": 0.5,
        "memory_limit_mb": "auto",
        "memory_reserve_mb": 2048,
        "download_timeout": 28800,
        "connect_timeout": 300,
        "no_progress_timeout": 1800,
        "x264_low_memory": False,
        "gapless_concat": False,
        "low_process_priority": False,
        "rlimit_as_mb": 0,
    }
    defaults.update(overrides)
    return PipelineConfig(**defaults)


class TestPipelineConfig:
    def test_default_config_valid(self):
        cfg = _valid_config()
        assert cfg.input_raw == "video.mp4"
        assert cfg.method == "segment"
        assert cfg.threshold == -30.0

    def test_frozen(self):
        cfg = _valid_config()
        with pytest.raises(FrozenInstanceError):
            cfg.threshold = -25.0  # type: ignore[misc]

    def test_all_documented_fields_present(self):
        cfg = _valid_config()
        # Pin the field set so a future addition doesn't silently drop
        # a setting the GUI used to pass to cut_and_concat.
        expected = {
            "input_raw",
            "output_dir",
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
            "force",
            "delete_after",
            "per_video_dir",
            "threshold",
            "min_silence",
            "margin",
            "memory_limit_mb",
            "memory_reserve_mb",
            "download_timeout",
            "connect_timeout",
            "no_progress_timeout",
            "x264_low_memory",
            "gapless_concat",
            "low_process_priority",
            "rlimit_as_mb",
            "segment_encode_timeout",
            "final_concat_timeout",
            "silence_timeout",
            "stall_kill_timeout",
            "stall_warning_timeout",
            "waveform_timeout",
            "batch_chunk_size",
            "min_part_bytes",
        }
        assert set(cfg.__dataclass_fields__.keys()) == expected


class TestPipelineCallbacks:
    def test_has_documented_callables(self):
        cb = PipelineCallbacks(
            on_progress=lambda f: None,
            on_status=lambda s: None,
            on_log=lambda s: None,
            on_info=lambda s: None,
            on_overall=lambda e, r, m: None,
            on_total=lambda t: None,
            on_download_progress=lambda p: None,
            on_pipeline_complete=lambda d: None,
        )
        assert isinstance(cb.on_progress, Callable)
        assert isinstance(cb.on_status, Callable)
        assert isinstance(cb.on_log, Callable)
        assert isinstance(cb.on_info, Callable)
        assert isinstance(cb.on_overall, Callable)
        assert isinstance(cb.on_total, Callable)
        assert isinstance(cb.on_download_progress, Callable)
        assert isinstance(cb.on_pipeline_complete, Callable)

    def test_frozen(self):
        cb = PipelineCallbacks(
            on_progress=lambda f: None,
            on_status=lambda s: None,
            on_log=lambda s: None,
            on_info=lambda s: None,
            on_overall=lambda e, r, m: None,
            on_total=lambda t: None,
            on_download_progress=lambda p: None,
            on_pipeline_complete=lambda d: None,
        )
        with pytest.raises(FrozenInstanceError):
            cb.on_progress = lambda f: None  # type: ignore[misc]


class TestValidatePipelineConfig:
    def test_valid_config_no_errors(self):
        cfg = _valid_config()
        assert validate_pipeline_config(cfg) == []

    def test_empty_input(self):
        cfg = _valid_config(input_raw="   ")
        errors = validate_pipeline_config(cfg)
        assert any("Input is empty" in e for e in errors)

    def test_unknown_method(self):
        cfg = _valid_config(method="weird")
        errors = validate_pipeline_config(cfg)
        assert any("Unknown method" in e for e in errors)

    def test_unknown_encoder(self):
        cfg = _valid_config(encoder="vp9")
        errors = validate_pipeline_config(cfg)
        assert any("Unknown encoder" in e for e in errors)

    def test_unknown_output_format(self):
        cfg = _valid_config(output_format="ogg")
        errors = validate_pipeline_config(cfg)
        assert any("Unknown output_format" in e for e in errors)

    @pytest.mark.parametrize("fmt", ["video", "mp3", "opus", "aac", "wav", "flac"])
    def test_all_output_formats_valid(self, fmt: str):
        cfg = _valid_config(output_format=fmt)
        assert validate_pipeline_config(cfg) == []

    def test_threshold_out_of_range(self):
        cfg = _valid_config(threshold=-70.0)
        errors = validate_pipeline_config(cfg)
        assert any("threshold" in e and "out of range" in e for e in errors)

    def test_min_silence_out_of_range(self):
        cfg = _valid_config(min_silence=0.05)
        errors = validate_pipeline_config(cfg)
        assert any("min_silence" in e for e in errors)

    def test_margin_out_of_range(self):
        cfg = _valid_config(margin=10.0)
        errors = validate_pipeline_config(cfg)
        assert any("margin" in e for e in errors)

    def test_multiple_errors_returned(self):
        cfg = _valid_config(
            input_raw="",
            method="bad",
            encoder="bad",
            threshold=-100.0,
        )
        errors = validate_pipeline_config(cfg)
        # All four errors reported, not just the first.
        assert len(errors) >= 4

    def test_boundary_values_accepted(self):
        # Edges of the valid ranges should pass without errors.
        cfg = _valid_config(threshold=-60.0, min_silence=0.1, margin=-3.0)
        assert validate_pipeline_config(cfg) == []
        cfg = _valid_config(threshold=-5.0, min_silence=60.0, margin=5.0)
        assert validate_pipeline_config(cfg) == []


class TestPipelineControllerRun:
    """Orchestration tests for PipelineController.run().

    Mocks the download/detect_silence/cut_and_concat functions so the
    controller's state machine is exercised without ffmpeg / network.
    """

    def _make_callbacks(self) -> tuple[PipelineCallbacks, dict]:
        """Build a PipelineCallbacks bundle that records calls in a dict."""
        calls: dict = {
            "progress": [],
            "status": [],
            "log": [],
            "overall": [],
            "total": [],
            "download_progress": [],
            "complete": [],
        }

        def on_progress(f: float) -> None:
            calls["progress"].append(f)

        def on_status(s: str) -> None:
            calls["status"].append(s)

        def on_log(s: str) -> None:
            calls["log"].append(s)

        def on_info(s: str) -> None:
            calls.setdefault("info", []).append(s)

        def on_overall(elapsed: float, remaining: float | None, more: bool) -> None:
            calls["overall"].append((elapsed, remaining, more))

        def on_total(t: float) -> None:
            calls["total"].append(t)

        def on_download_progress(p) -> None:
            calls["download_progress"].append(p)

        def on_pipeline_complete(d: dict) -> None:
            calls["complete"].append(d)

        cb = PipelineCallbacks(
            on_progress=on_progress,
            on_status=on_status,
            on_log=on_log,
            on_info=on_info,
            on_overall=on_overall,
            on_total=on_total,
            on_download_progress=on_download_progress,
            on_pipeline_complete=on_pipeline_complete,
        )
        return cb, calls

    def test_success_path_calls_callbacks_and_returns_result(self, tmp_path: Path):
        cfg = _valid_config(output_dir=tmp_path, input_raw=str(tmp_path / "src.mp4"))
        cb, calls = self._make_callbacks()
        cancel = __import__("threading").Event()
        controller = PipelineController(cfg=cfg, cb=cb, cancel_event=cancel)

        # Mock download to return a local file
        fake_video = tmp_path / "src.mp4"
        fake_video.write_text("dummy")

        def fake_download(url, out_dir, **kwargs):
            from stream2video.download import DownloadResult

            return DownloadResult(path=fake_video, is_downloaded=False)

        with (
            patch("stream2video.pipeline_controller.download", side_effect=fake_download),
            patch("stream2video.pipeline_controller.detect_silence", return_value=[]),
            patch("stream2video.pipeline_controller.save_silence_cache"),
            patch("stream2video.pipeline_controller.load_silence_cache", return_value=None),
            patch("stream2video.pipeline_controller.cut_and_concat"),
            patch(
                "stream2video.pipeline_controller.generate_keep_segments", return_value=[(0.0, 1.0)]
            ),
            patch("stream2video.pipeline_controller.get_video_duration", return_value=10.0),
            patch(
                "stream2video.pipeline_controller.apply_per_video_dir",
                return_value=(tmp_path, fake_video),
            ),
        ):
            # Create a fake output file so .stat().st_size works
            output_file = tmp_path / "src_compressed.mp4"
            output_file.write_text("output")
            with patch.object(Path, "stat") as mock_stat:
                mock_stat.return_value = MagicMock(st_size=100)
                result = controller.run()

        assert isinstance(result, PipelineResult)
        assert result.video_path == fake_video
        # Status updates were called
        assert any("Step 1/3" in s for s in calls["status"])
        assert any("Step 2/3" in s for s in calls["status"])
        assert any("Step 3/3" in s for s in calls["status"])
        # Progress was reported
        assert len(calls["progress"]) > 0
        # Completion callback was called
        assert len(calls["complete"]) == 1

    def test_download_failure_raises_pipeline_download_error(self, tmp_path: Path):
        cfg = _valid_config(output_dir=tmp_path, input_raw="https://example.com/v")
        cb, _calls = self._make_callbacks()
        cancel = __import__("threading").Event()
        controller = PipelineController(cfg=cfg, cb=cb, cancel_event=cancel)

        from stream2video.download import DownloadError

        with (
            patch(
                "stream2video.pipeline_controller.download",
                side_effect=DownloadError("network fail"),
            ),
            pytest.raises(PipelineDownloadError, match="network fail"),
        ):
            controller.run()

    def test_silence_failure_raises_pipeline_silence_error(self, tmp_path: Path):
        cfg = _valid_config(output_dir=tmp_path, input_raw=str(tmp_path / "src.mp4"))
        cb, _calls = self._make_callbacks()
        cancel = __import__("threading").Event()
        controller = PipelineController(cfg=cfg, cb=cb, cancel_event=cancel)

        fake_video = tmp_path / "src.mp4"
        fake_video.write_text("dummy")

        def fake_download(url, out_dir, **kwargs):
            from stream2video.download import DownloadResult

            return DownloadResult(path=fake_video, is_downloaded=False)

        from stream2video.silence import SilenceDetectionError

        with (
            patch("stream2video.pipeline_controller.download", side_effect=fake_download),
            patch(
                "stream2video.pipeline_controller.detect_silence",
                side_effect=SilenceDetectionError("ffmpeg fail"),
            ),
            patch("stream2video.pipeline_controller.load_silence_cache", return_value=None),
            patch("stream2video.pipeline_controller.get_video_duration", return_value=10.0),
            patch(
                "stream2video.pipeline_controller.apply_per_video_dir",
                return_value=(tmp_path, fake_video),
            ),
            patch.object(Path, "stat", return_value=MagicMock(st_size=100)),
            pytest.raises(PipelineSilenceError, match="ffmpeg fail"),
        ):
            controller.run()

    def test_concat_failure_raises_pipeline_concat_error(self, tmp_path: Path):
        cfg = _valid_config(output_dir=tmp_path, input_raw=str(tmp_path / "src.mp4"))
        cb, _calls = self._make_callbacks()
        cancel = __import__("threading").Event()
        controller = PipelineController(cfg=cfg, cb=cb, cancel_event=cancel)

        fake_video = tmp_path / "src.mp4"
        fake_video.write_text("dummy")

        def fake_download(url, out_dir, **kwargs):
            from stream2video.download import DownloadResult

            return DownloadResult(path=fake_video, is_downloaded=False)

        from stream2video.concat import ConcatError

        with (
            patch("stream2video.pipeline_controller.download", side_effect=fake_download),
            patch("stream2video.pipeline_controller.detect_silence", return_value=[]),
            patch("stream2video.pipeline_controller.save_silence_cache"),
            patch("stream2video.pipeline_controller.load_silence_cache", return_value=None),
            patch(
                "stream2video.pipeline_controller.cut_and_concat",
                side_effect=ConcatError("encode fail"),
            ),
            patch(
                "stream2video.pipeline_controller.generate_keep_segments", return_value=[(0.0, 1.0)]
            ),
            patch("stream2video.pipeline_controller.get_video_duration", return_value=10.0),
            patch(
                "stream2video.pipeline_controller.apply_per_video_dir",
                return_value=(tmp_path, fake_video),
            ),
            patch.object(Path, "stat", return_value=MagicMock(st_size=100)),
            pytest.raises(PipelineConcatError, match="encode fail"),
        ):
            controller.run()

    def test_cancel_before_silence_raises_pipeline_cancelled(self, tmp_path: Path):
        cfg = _valid_config(output_dir=tmp_path, input_raw=str(tmp_path / "src.mp4"))
        cb, _calls = self._make_callbacks()
        cancel = __import__("threading").Event()
        controller = PipelineController(cfg=cfg, cb=cb, cancel_event=cancel)

        fake_video = tmp_path / "src.mp4"
        fake_video.write_text("dummy")

        def fake_download(url, out_dir, **kwargs):
            from stream2video.download import DownloadResult

            return DownloadResult(path=fake_video, is_downloaded=False)

        with (
            patch("stream2video.pipeline_controller.download", side_effect=fake_download),
            patch("stream2video.pipeline_controller.get_video_duration", return_value=10.0),
            patch(
                "stream2video.pipeline_controller.apply_per_video_dir",
                return_value=(tmp_path, fake_video),
            ),
            patch("stream2video.pipeline_controller.detect_silence", return_value=[]),
            patch("stream2video.pipeline_controller.save_silence_cache"),
            patch.object(Path, "stat", return_value=MagicMock(st_size=100, st_mtime=1000.0)),
        ):
            # Set cancel AFTER download but BEFORE silence phase
            def set_cancel(*args, **kwargs):
                cancel.set()
                return None

            with (
                patch(
                    "stream2video.pipeline_controller.load_silence_cache", side_effect=set_cancel
                ),
                pytest.raises(PipelineCancelled),
            ):
                controller.run()

    def test_cancel_then_restart_resumes_via_cache(self, tmp_path: Path):
        """Fix-plan section 4 Resume/failure: "Cancel и повторный запуск CLI/GUI".

        When the user cancels mid-detection and then re-runs, the second
        run must pick up the cached silence segments from the first run
        (via ``load_silence_cache``) instead of re-detecting from scratch.

        The cache write happens inside ``detect_silence`` itself; here
        we verify the controller's wiring: the first run is cancelled
        AFTER the cache was written (simulating a Ctrl+C after the
        silence phase completed but before concat started), and the
        second run loads the cache instead of calling detect_silence
        again.
        """
        cfg = _valid_config(output_dir=tmp_path, input_raw=str(tmp_path / "src.mp4"))
        cb, _ = self._make_callbacks()
        cancel = __import__("threading").Event()
        controller = PipelineController(cfg=cfg, cb=cb, cancel_event=cancel)

        fake_video = tmp_path / "src.mp4"
        fake_video.write_text("dummy")

        def fake_download(url, out_dir, **kwargs):
            from stream2video.download import DownloadResult

            return DownloadResult(path=fake_video, is_downloaded=False)

        # Track detect_silence / load_silence_cache invocations across
        # both runs. The first run calls detect_silence (cancelled after);
        # the second run must call load_silence_cache and NOT detect_silence.
        detect_calls: list[int] = []
        load_calls: list[int] = []

        def fake_detect_silence(*args, **kwargs):
            detect_calls.append(1)
            return [MagicMock(start=0.0, end=1.0)]

        def fake_load_silence_cache(*args, **kwargs):
            load_calls.append(1)
            # Second run: return the cached segments so detect_silence
            # is never called.
            return [MagicMock(start=0.0, end=1.0)]

        # First run: cancel AFTER silence detection completes (simulated
        # by having detect_silence return successfully) but BEFORE concat.
        def fake_cut_and_concat_cancel(*args, **kwargs):
            cancel.set()
            raise PipelineCancelled("cancelled before concat")

        with (
            patch("stream2video.pipeline_controller.download", side_effect=fake_download),
            patch("stream2video.pipeline_controller.get_video_duration", return_value=10.0),
            patch(
                "stream2video.pipeline_controller.apply_per_video_dir",
                return_value=(tmp_path, fake_video),
            ),
            patch(
                "stream2video.pipeline_controller.detect_silence", side_effect=fake_detect_silence
            ),
            patch("stream2video.pipeline_controller.save_silence_cache"),
            patch("stream2video.pipeline_controller.load_silence_cache", return_value=None),
            patch(
                "stream2video.pipeline_controller.generate_keep_segments",
                return_value=[(0.0, 1.0)],
            ),
            patch.object(Path, "stat", return_value=MagicMock(st_size=100, st_mtime=1000.0)),
            patch(
                "stream2video.pipeline_controller.cut_and_concat",
                side_effect=fake_cut_and_concat_cancel,
            ),
            pytest.raises(PipelineCancelled),
        ):
            controller.run()

        # First run: detect_silence called once, load_silence_cache
        # returned None (no cache existed).
        assert len(detect_calls) == 1, "first run should call detect_silence"
        assert len(load_calls) == 0, "first run has no cache to load"

        # Second run: new controller, same config (force=False so cache
        # is consulted). load_silence_cache now returns the cached list,
        # so detect_silence must NOT be called.
        cancel2 = __import__("threading").Event()
        controller2 = PipelineController(cfg=cfg, cb=cb, cancel_event=cancel2)

        def fake_cut_and_concat_success(*args, **kwargs):
            return tmp_path / "out.mp4"

        with (
            patch("stream2video.pipeline_controller.download", side_effect=fake_download),
            patch("stream2video.pipeline_controller.get_video_duration", return_value=10.0),
            patch(
                "stream2video.pipeline_controller.apply_per_video_dir",
                return_value=(tmp_path, fake_video),
            ),
            patch(
                "stream2video.pipeline_controller.detect_silence", side_effect=fake_detect_silence
            ),
            patch("stream2video.pipeline_controller.save_silence_cache"),
            patch(
                "stream2video.pipeline_controller.load_silence_cache",
                side_effect=fake_load_silence_cache,
            ),
            patch(
                "stream2video.pipeline_controller.generate_keep_segments",
                return_value=[(0.0, 1.0)],
            ),
            patch.object(Path, "stat", return_value=MagicMock(st_size=100, st_mtime=1000.0)),
            patch(
                "stream2video.pipeline_controller.cut_and_concat",
                side_effect=fake_cut_and_concat_success,
            ),
        ):
            result = controller2.run()

        # Second run: cache hit → detect_silence NOT called again.
        assert len(load_calls) == 1, "second run should load cached silence"
        assert len(detect_calls) == 1, (
            "second run must NOT re-detect silence when cache exists "
            f"(detect_calls={len(detect_calls)})"
        )
        assert isinstance(result, PipelineResult)


class TestPipelineControllerTkIsolation:
    """Fix-plan §4 GUI/threading: "Ни одного Tk call из worker thread".

    PipelineController is the worker-thread surface — if it imported
    Tkinter (or customtkinter), the worker would be able to call widgets
    directly, which is unsafe (Tk widgets are main-thread-only). The
    architectural guard is enforced by static import analysis: the
    module must not import tkinter / customtkinter / PIL.

    A real event-loop test (preview concurrent with pipeline, popup
    close during decode, etc.) requires pytest-qt and is deferred —
    this static check is the cheap regression net that catches a
    careless ``from tkinter import messagebox`` added during a refactor.
    """

    def test_pipeline_controller_does_not_import_tk(self):
        import importlib
        import sys

        # Drop any cached copy so we get a fresh import that records
        # which modules it pulls in.
        mods_before = set(sys.modules.keys())
        try:
            importlib.import_module("stream2video.pipeline_controller")
        finally:
            pass
        mods_after = set(sys.modules.keys())
        new_mods = mods_after - mods_before
        # Must not pull in tkinter / customtkinter / PIL. The controller
        # is pure orchestration; callbacks dispatch to Tk via the
        # GUI's bound methods (which use self._tk_after on the main loop).
        forbidden = {"tkinter", "customtkinter", "PIL", "PIL.Image"}
        leaked = forbidden & new_mods
        assert not leaked, (
            f"pipeline_controller pulled Tk deps into sys.modules: {leaked}. "
            f"The worker thread must not be able to call Tk widgets directly."
        )

    def test_pipeline_controller_source_has_no_tk_references(self):
        # Static source check: scan pipeline_controller.py for direct
        # references to tkinter / customtkinter / messagebox. A stray
        # ``import tkinter`` (even if unused) is a smell that the module
        # is gaining UI coupling.
        from pathlib import Path

        source = (
            Path(__file__).parent.parent / "stream2video" / "pipeline_controller.py"
        ).read_text(encoding="utf-8")
        forbidden_tokens = (
            "import tkinter",
            "from tkinter",
            "import customtkinter",
            "from customtkinter",
            "messagebox",
            ".after(",
            "tkinter.",
        )
        leaked = [t for t in forbidden_tokens if t in source]
        assert not leaked, (
            f"pipeline_controller.py contains Tk references: {leaked}. "
            f"Move the UI coupling into the GUI layer."
        )

    def test_pipeline_worker_does_not_read_widgets_directly(self):
        """Fix-plan P1.10: ``_pipeline_worker`` runs on a background
        thread; reading Tk widgets (``self.combo_*``, ``self.entry_*``,
        ``self.chk_*``, ``self.spin_*``) from there is unsafe because
        Tk widgets are main-thread-only. The GUI snapshots widget
        values in ``_start_pipeline`` (main thread) and passes them as
        args; the worker reads only these local copies + ``self.config``
        (a plain dict snapshot, safe to read from any thread).

        This test parses gui.py's AST and walks the body of
        ``_pipeline_worker`` looking for ``self.<widget_attr>`` reads.
        A regression here means a future edit added a widget read
        directly in the worker instead of plumbing it through args.
        """
        import ast
        from pathlib import Path

        source = (Path(__file__).parent.parent / "stream2video" / "gui.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)

        # Widget attribute prefixes the GUI uses. A read of any
        # ``self.<prefix>_*`` inside _pipeline_worker is a violation.
        widget_prefixes = ("combo_", "entry_", "chk_", "spin_", "btn_")

        def _is_widget_read(node: ast.Attribute) -> bool:
            # ``self.combo_encoder`` etc. — attribute access on self.
            if not isinstance(node.value, ast.Name):
                return False
            if node.value.id != "self":
                return False
            return any(node.attr.startswith(p) for p in widget_prefixes)

        # Find _pipeline_worker method definition.
        worker_fn: ast.FunctionDef | None = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_pipeline_worker":
                worker_fn = node
                break
        assert worker_fn is not None, "_pipeline_worker method not found in gui.py"

        violations: list[str] = []
        for node in ast.walk(worker_fn):
            if isinstance(node, ast.Attribute) and _is_widget_read(node):
                violations.append(
                    f"line {node.lineno}: self.{node.attr} (widget read in worker thread)"
                )
        assert not violations, (
            "_pipeline_worker reads Tk widgets directly from the worker "
            "thread (P1.10 violation). Snapshot the value in _start_pipeline "
            "(main thread) and pass it as a positional arg:\n  " + "\n  ".join(violations)
        )
