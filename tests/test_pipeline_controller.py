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

import pytest

from stream2video.pipeline_controller import (
    PipelineCallbacks,
    PipelineConfig,
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
