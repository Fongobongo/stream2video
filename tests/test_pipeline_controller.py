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
