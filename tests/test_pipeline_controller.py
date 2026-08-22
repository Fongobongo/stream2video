"""Tests for stream2video.pipeline_controller.

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

# The autouse conftest fixture replaces _output_media_is_valid with a
# dummy True-returning patch for the whole suite (the dummy-output
# tests would otherwise all fail the real ffmpeg gate). Tests that
# exercise the REAL seam logic capture the genuine implementation
# here — at import time, before any fixture runs — and re-install it
# with patch.object.
_REAL_OUTPUT_MEDIA_IS_VALID = PipelineController._output_media_is_valid


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
        "use_crf": False,
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
            "use_crf",
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
            "proxy",
            "dry_run",
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

    @pytest.mark.parametrize("quality", ["source", "high", "medium", "low"])
    def test_all_quality_presets_valid(self, quality: str):
        cfg = _valid_config(video_quality=quality, audio_quality=quality)
        assert validate_pipeline_config(cfg) == []

    def test_all_x264_presets_valid(self):
        # Same drift guard as the quality presets: every value the
        # VALID_X264_PRESETS list advertises must pass validation (the
        # list had values no test had ever fed through the validator).
        from stream2video.config import VALID_X264_PRESETS

        for preset in VALID_X264_PRESETS:
            assert validate_pipeline_config(_valid_config(x264_preset=preset)) == []

    def test_proxy_schemeless_rejected(self):
        # The classic typo: an address without a scheme never works in
        # yt-dlp — reject it at validation with the shared rule.
        cfg = _valid_config(proxy="127.0.0.1:8080")
        errors = validate_pipeline_config(cfg)
        assert any("Invalid proxy" in e and "proxy scheme" in e for e in errors)

    def test_proxy_with_credentials_accepted(self):
        cfg = _valid_config(proxy="socks5://user:pass@host:1080")
        assert validate_pipeline_config(cfg) == []

    def test_empty_proxy_accepted(self):
        cfg = _valid_config(proxy="")
        assert validate_pipeline_config(cfg) == []

    def test_proxy_bad_port_rejected(self):
        cfg = _valid_config(proxy="http://host:abc")
        errors = validate_pipeline_config(cfg)
        assert any("Invalid proxy" in e and "invalid port" in e for e in errors)

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

    def test_nan_and_infinity_rejected(self):
        """Audit round 15 P1: a non-finite float used to pass
        ``lo <= value <= hi`` (all comparisons with nan are False) and
        poison the pipeline's numeric model for direct API hosts / stale
        settings.json. It must be rejected with a dedicated message."""
        for key, bad in (
            ("threshold", float("nan")),
            ("min_silence", float("inf")),
            ("margin", float("-inf")),
            ("download_timeout", float("nan")),
            ("batch_chunk_size", float("inf")),
        ):
            errors = validate_pipeline_config(_valid_config(**{key: bad}))
            assert any(key in e and "finite" in e for e in errors), (key, errors)

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

    def test_encoder_threads_boundaries_accepted(self):
        # ``auto`` and the 1..1024 edges are the valid forms.
        assert validate_pipeline_config(_valid_config(encoder_threads="auto")) == []
        assert validate_pipeline_config(_valid_config(encoder_threads=1)) == []
        assert validate_pipeline_config(_valid_config(encoder_threads=1024)) == []

    @pytest.mark.parametrize("bad", [0, 1025, -4, True, "4"])
    def test_encoder_threads_rejected(self, bad):
        errors = validate_pipeline_config(_valid_config(encoder_threads=bad))
        assert any("encoder_threads" in e for e in errors)

    def test_memory_limit_mb_boundaries_accepted(self):
        assert validate_pipeline_config(_valid_config(memory_limit_mb="auto")) == []
        assert validate_pipeline_config(_valid_config(memory_limit_mb=0)) == []
        assert validate_pipeline_config(_valid_config(memory_limit_mb=1048576)) == []

    @pytest.mark.parametrize("bad", [-1, 1048577, True, "8192"])
    def test_memory_limit_mb_rejected(self, bad):
        errors = validate_pipeline_config(_valid_config(memory_limit_mb=bad))
        assert any("memory_limit_mb" in e for e in errors)

    def test_pipeline_timeouts_out_of_range(self):
        # The audit found only threshold/min_silence/margin were range-
        # checked pre-flight; a stale settings.json timeout used to
        # survive until the phase's watchdog fired mid-run.
        cfg = _valid_config(segment_encode_timeout=-5, batch_chunk_size=0)
        errors = validate_pipeline_config(cfg)
        assert any("segment_encode_timeout" in e and "out of range" in e for e in errors)
        assert any("batch_chunk_size" in e and "out of range" in e for e in errors)

    def test_stall_warning_below_kill_accepted(self):
        """The warning may fire before the kill — the sane pair."""
        assert (
            validate_pipeline_config(
                _valid_config(stall_warning_timeout=5, stall_kill_timeout=3600)
            )
            == []
        )

    def test_stall_warning_not_below_kill_rejected(self):
        """Audit round 22 P7: stall_warning_timeout >= stall_kill_timeout
        is contradictory — the warning would fire after (or never before)
        the kill — and must be rejected as a cross-field error, not just
        range-checked per key."""
        cfg = _valid_config(stall_warning_timeout=1800, stall_kill_timeout=10)
        errors = validate_pipeline_config(cfg)
        assert any(
            "stall_warning_timeout must be lower than stall_kill_timeout" in e for e in errors
        )

    def test_stall_warning_equal_kill_rejected(self):
        errors = validate_pipeline_config(
            _valid_config(stall_warning_timeout=300, stall_kill_timeout=300)
        )
        assert any(
            "stall_warning_timeout must be lower than stall_kill_timeout" in e for e in errors
        )

    @pytest.mark.parametrize(
        "key",
        [
            "download_timeout",
            "connect_timeout",
            "no_progress_timeout",
            "segment_encode_timeout",
            "final_concat_timeout",
            "silence_timeout",
            "stall_kill_timeout",
            "stall_warning_timeout",
            "waveform_timeout",
            "batch_chunk_size",
            "min_part_bytes",
            "memory_reserve_mb",
            "rlimit_as_mb",
        ],
    )
    def test_all_number_keys_covered_by_config_ranges(self, key: str):
        # Every numeric PipelineConfig slot must be range-validated —
        # pin the loop invariant so a new tunable doesn't silently slip
        # past the pre-flight check.
        from stream2video.config import CONFIG_RANGES

        assert key in CONFIG_RANGES, f"{key} has no range entry"


def _make_callbacks() -> tuple[PipelineCallbacks, dict]:
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

    def on_status(s: str, force: bool = False) -> None:
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


class TestPipelineControllerRun:
    """Orchestration tests for PipelineController.run().

    Mocks the download/detect_silence/cut_and_concat functions so the
    controller's state machine is exercised without ffmpeg / network.
    """

    def _make_callbacks(self) -> tuple[PipelineCallbacks, dict]:
        return _make_callbacks()

    def test_success_path_calls_callbacks_and_returns_result(self, tmp_path: Path):
        cfg = _valid_config(output_dir=tmp_path, input_raw=str(tmp_path / "src.mp4"))
        cb, calls = _make_callbacks()
        cancel = __import__("threading").Event()
        controller = PipelineController(cfg=cfg, cb=cb, cancel_event=cancel)

        # Mock download to return a local file
        fake_video = tmp_path / "src.mp4"
        fake_video.write_text("dummy")

        def fake_download(url, out_dir, **kwargs):
            from stream2video.download import DownloadResult

            return DownloadResult(path=fake_video, is_downloaded=False)

        def fake_cut_and_concat(*args, **kwargs):
            # Staged publish (audit round 30 P0): concat writes into the
            # STAGING path (3rd positional arg), not the stable output.
            Path(args[2]).write_text("output")

        with (
            patch("stream2video.pipeline_controller.download", side_effect=fake_download),
            patch("stream2video.pipeline_controller.detect_silence", return_value=[]),
            patch("stream2video.pipeline_controller.save_silence_cache"),
            patch("stream2video.pipeline_controller.load_silence_cache", return_value=None),
            patch(
                "stream2video.pipeline_controller.cut_and_concat",
                side_effect=fake_cut_and_concat,
            ),
            patch(
                "stream2video.pipeline_controller.generate_keep_segments", return_value=[(0.0, 1.0)]
            ),
            patch("stream2video.pipeline_controller.get_video_duration", return_value=10.0),
            patch(
                "stream2video.pipeline_controller.apply_per_video_dir",
                return_value=(tmp_path, fake_video),
            ),
        ):
            result = controller.run()

        assert isinstance(result, PipelineResult)
        assert result.video_path == fake_video
        assert calls.get("info") == ["Silence: 0 segments\nKeep: 1 segments (1s)"]
        # Status updates were called — now 1/4..4/4
        assert any("Step 1/4" in s for s in calls["status"])
        assert any("Step 2/4" in s for s in calls["status"])
        assert any("Step 3/4" in s or "Step 4/4" in s for s in calls["status"])
        # Progress was reported
        assert len(calls["progress"]) > 0
        # Completion callback was called
        assert len(calls["complete"]) == 1

    def test_old_ffmpeg_logs_startup_warning_before_phases(self, tmp_path: Path):
        """ffmpeg below FFMPEG_MIN_VERSION (the README floor): run() says so
        through on_log right after config validation — the audio quality
        presets mis-encode on old builds, and the user must hear it before
        a multi-hour run, not from the output file afterwards."""
        cfg = _valid_config(output_dir=tmp_path, input_raw=str(tmp_path / "src.mp4"))
        cb, calls = _make_callbacks()
        cancel = __import__("threading").Event()
        controller = PipelineController(cfg=cfg, cb=cb, cancel_event=cancel)

        fake_video = tmp_path / "src.mp4"
        fake_video.write_text("dummy")

        def fake_download(url, out_dir, **kwargs):
            from stream2video.download import DownloadResult

            return DownloadResult(path=fake_video, is_downloaded=False)

        def fake_cut_and_concat(*args, **kwargs):
            Path(args[2]).write_text("output")

        warning = "ffmpeg 4.4.2 is older than the supported minimum 5.0"
        with (
            patch(
                "stream2video.pipeline_controller.ffmpeg_min_version_warning",
                return_value=warning,
            ),
            patch("stream2video.pipeline_controller.download", side_effect=fake_download),
            patch("stream2video.pipeline_controller.detect_silence", return_value=[]),
            patch("stream2video.pipeline_controller.save_silence_cache"),
            patch("stream2video.pipeline_controller.load_silence_cache", return_value=None),
            patch(
                "stream2video.pipeline_controller.cut_and_concat",
                side_effect=fake_cut_and_concat,
            ),
            patch(
                "stream2video.pipeline_controller.generate_keep_segments", return_value=[(0.0, 1.0)]
            ),
            patch("stream2video.pipeline_controller.get_video_duration", return_value=10.0),
            patch(
                "stream2video.pipeline_controller.apply_per_video_dir",
                return_value=(tmp_path, fake_video),
            ),
        ):
            controller.run()

        assert calls["log"].count(warning) == 1

    def test_modern_ffmpeg_logs_no_version_warning(self, tmp_path: Path):
        """The None branch: a modern (or unparseable-banner) ffmpeg adds no
        log line at all — the happy path stays quiet."""
        cfg = _valid_config(output_dir=tmp_path, input_raw=str(tmp_path / "src.mp4"))
        cb, calls = _make_callbacks()
        cancel = __import__("threading").Event()
        controller = PipelineController(cfg=cfg, cb=cb, cancel_event=cancel)

        fake_video = tmp_path / "src.mp4"
        fake_video.write_text("dummy")

        def fake_download(url, out_dir, **kwargs):
            from stream2video.download import DownloadResult

            return DownloadResult(path=fake_video, is_downloaded=False)

        def fake_cut_and_concat(*args, **kwargs):
            Path(args[2]).write_text("output")

        with (
            patch(
                "stream2video.pipeline_controller.ffmpeg_min_version_warning",
                return_value=None,
            ),
            patch("stream2video.pipeline_controller.download", side_effect=fake_download),
            patch("stream2video.pipeline_controller.detect_silence", return_value=[]),
            patch("stream2video.pipeline_controller.save_silence_cache"),
            patch("stream2video.pipeline_controller.load_silence_cache", return_value=None),
            patch(
                "stream2video.pipeline_controller.cut_and_concat",
                side_effect=fake_cut_and_concat,
            ),
            patch(
                "stream2video.pipeline_controller.generate_keep_segments", return_value=[(0.0, 1.0)]
            ),
            patch("stream2video.pipeline_controller.get_video_duration", return_value=10.0),
            patch(
                "stream2video.pipeline_controller.apply_per_video_dir",
                return_value=(tmp_path, fake_video),
            ),
        ):
            controller.run()

        assert not any("minimum" in s for s in calls["log"])

    def test_local_input_uses_compact_dynamic_progress_weights(self, tmp_path: Path):
        cfg = _valid_config(output_dir=tmp_path, input_raw=str(tmp_path / "src.mp4"))
        cb, calls = _make_callbacks()
        cancel = __import__("threading").Event()
        controller = PipelineController(cfg=cfg, cb=cb, cancel_event=cancel)

        fake_video = tmp_path / "src.mp4"
        fake_video.write_bytes(b"dummy")

        def fake_download(url, out_dir, **kwargs):
            from stream2video.download import DownloadResult

            return DownloadResult(path=fake_video, is_downloaded=False)

        def fake_detect_silence(*args, **kwargs):
            kwargs["progress_callback"](0.5)
            return []

        def fake_cut_and_concat(*args, **kwargs):
            kwargs["progress_callback"](0.5)
            args[2].write_bytes(b"output")

        with (
            patch("stream2video.pipeline_controller.download", side_effect=fake_download),
            patch(
                "stream2video.pipeline_controller.detect_silence",
                side_effect=fake_detect_silence,
            ),
            patch("stream2video.pipeline_controller.save_silence_cache"),
            patch("stream2video.pipeline_controller.load_silence_cache", return_value=None),
            patch(
                "stream2video.pipeline_controller.cut_and_concat", side_effect=fake_cut_and_concat
            ),
            patch(
                "stream2video.pipeline_controller.generate_keep_segments", return_value=[(0.0, 1.0)]
            ),
            patch("stream2video.pipeline_controller.get_video_duration", return_value=300.0),
            patch(
                "stream2video.pipeline_controller.apply_per_video_dir",
                return_value=(tmp_path, fake_video),
            ),
        ):
            result = controller.run()

        assert isinstance(result, PipelineResult)
        assert "Step 1/4: Local file ready" in calls["status"]
        assert not any(s == "Step 1/4: Download complete" for s in calls["status"])
        # Local input skips downloading, so download 0%, silence 25%,
        # cutting 68%, concatenating 7% (concat total 75%).
        assert any("download 0%, silence 25%" in s for s in calls["log"])
        assert any("cutting 68%" in s for s in calls["log"])
        assert any("[concat total 75%]" in s for s in calls["log"])
        assert any(pytest.approx(v, abs=1e-6) == 0.0 for v in calls["progress"])
        assert any(pytest.approx(v, abs=1e-6) == 0.125 for v in calls["progress"])
        assert any(pytest.approx(v, abs=1e-6) == 0.25 for v in calls["progress"])
        assert any(pytest.approx(v, abs=1e-6) == 0.625 for v in calls["progress"])
        assert calls["progress"][-1] == 1.0
        assert calls["progress"] == sorted(calls["progress"])

    def test_silence_cache_hit_reweights_progress_toward_concat(self, tmp_path: Path):
        cfg = _valid_config(output_dir=tmp_path, input_raw="https://example.com/v")
        cb, calls = _make_callbacks()
        cancel = __import__("threading").Event()
        controller = PipelineController(cfg=cfg, cb=cb, cancel_event=cancel)

        fake_video = tmp_path / "downloaded.mp4"
        fake_video.write_bytes(b"dummy")
        cached_segments = [MagicMock(start=0.0, end=1.0)]

        def fake_download(url, out_dir, **kwargs):
            from stream2video.download import DownloadResult

            return DownloadResult(path=fake_video, is_downloaded=True)

        def fake_cut_and_concat(*args, **kwargs):
            kwargs["progress_callback"](0.5)
            args[2].write_bytes(b"output")

        with (
            patch("stream2video.pipeline_controller.download", side_effect=fake_download),
            patch.object(PipelineController, "_downloaded_media_is_valid", return_value=True),
            patch("stream2video.pipeline_controller.detect_silence") as detect_mock,
            patch("stream2video.pipeline_controller.save_silence_cache"),
            patch(
                "stream2video.pipeline_controller.load_silence_cache",
                return_value=cached_segments,
            ),
            patch(
                "stream2video.pipeline_controller.generate_keep_segments", return_value=[(0.0, 1.0)]
            ),
            patch(
                "stream2video.pipeline_controller.cut_and_concat",
                side_effect=fake_cut_and_concat,
            ),
            patch("stream2video.pipeline_controller.get_video_duration", return_value=300.0),
            patch(
                "stream2video.pipeline_controller.apply_per_video_dir",
                return_value=(tmp_path, fake_video),
            ),
        ):
            result = controller.run()

        assert isinstance(result, PipelineResult)
        detect_mock.assert_not_called()
        # Cache hit makes silence 10%, cutting 76%, concatenating 8% (concat total 85%).
        assert any("download 5%, silence 10%" in s for s in calls["log"])
        assert any("cutting 76%" in s for s in calls["log"])
        assert any("[concat total 85%]" in s for s in calls["log"])
        assert any(pytest.approx(v, abs=1e-6) == 0.05 for v in calls["progress"])
        assert any(pytest.approx(v, abs=1e-6) == 0.15 for v in calls["progress"])
        assert any(pytest.approx(v, abs=1e-6) == 0.575 for v in calls["progress"])
        assert calls["progress"][-1] == 1.0
        assert calls["progress"] == sorted(calls["progress"])

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

    def test_concat_failure_deletes_partial_output(self, tmp_path: Path):
        # P0: a failed cut_and_concat previously left a partially muxed
        # *_compressed.mp4 on disk (the runner kills ffmpeg mid-encode,
        # but nothing unlinked the in-progress output). The user then saw
        # a "finished-looking" file that is actually truncated.
        # With staged publish (audit round 30 P0) the partial lives in
        # the STAGING dir — the stable path is never touched — and the
        # cleanup removes the staged output file while KEEPING the
        # staging dir so the next run can resume (audit round 32 P1-1).
        cfg = _valid_config(output_dir=tmp_path, input_raw=str(tmp_path / "src.mp4"))
        cb, _calls = self._make_callbacks()
        cancel = __import__("threading").Event()
        controller = PipelineController(cfg=cfg, cb=cb, cancel_event=cancel)

        fake_video = tmp_path / "src.mp4"
        fake_video.write_text("dummy")
        from stream2video.paths import artifact_stem

        stem = artifact_stem(fake_video)
        stable_output = tmp_path / f"{stem}_compressed.mp4"
        staging_dir = tmp_path / f".{stem}_compressed.mp4.s2v_staging"

        def fake_download(url, out_dir, **kwargs):
            from stream2video.download import DownloadResult

            return DownloadResult(path=fake_video, is_downloaded=False)

        from stream2video.concat import ConcatError

        def fake_cut_and_concat(*args, **kwargs):
            # Staged publish: the concat phase writes into the staging
            # path (3rd positional arg). Simulate ffmpeg writing a
            # partial file into staging then dying mid-encode.
            Path(args[2]).write_text("partial")
            raise ConcatError("encode fail")

        with (
            patch("stream2video.pipeline_controller.download", side_effect=fake_download),
            patch("stream2video.pipeline_controller.detect_silence", return_value=[]),
            patch("stream2video.pipeline_controller.save_silence_cache"),
            patch("stream2video.pipeline_controller.load_silence_cache", return_value=None),
            patch(
                "stream2video.pipeline_controller.cut_and_concat",
                side_effect=fake_cut_and_concat,
            ),
            patch(
                "stream2video.pipeline_controller.generate_keep_segments", return_value=[(0.0, 1.0)]
            ),
            patch("stream2video.pipeline_controller.get_video_duration", return_value=10.0),
            patch(
                "stream2video.pipeline_controller.apply_per_video_dir",
                return_value=(tmp_path, fake_video),
            ),
            pytest.raises(PipelineConcatError, match="encode fail"),
        ):
            controller.run()

        # The partial went into staging — the stable output was never created.
        assert not stable_output.exists(), "stable output must never exist after a failed encode"
        # The staged OUTPUT file is deleted, but the staging dir itself
        # survives — its segment/chunk work dirs are the resume source
        # for the next run (audit round 32 P1-1).
        staged_partial = staging_dir / stable_output.name
        assert not staged_partial.exists(), "staged partial output left on disk after failure"
        assert staging_dir.exists(), "staging dir with resume parts must survive a failed encode"

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
        """Cancel и повторный запуск CLI/GUI.

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
            # Staged publish (audit round 30 P0): write to the staging
            # path (3rd positional arg), not the stable output.
            Path(args[2]).write_bytes(b"output")

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


class TestProjectLocks:
    """Audit round 24 P4: the controller takes project-level locks from
    BEFORE the download / cache / move phases — the output lock inside
    cut_and_concat is taken too late to stop two runs of the same URL
    (or same source) from colliding on .part files and cache writes.
    The locks are released (and their files removed) when run()
    finishes, and a second run WAITS (logged) then refuses.
    """

    def test_second_url_run_waits_then_refuses(self, tmp_path: Path):
        import hashlib
        import threading

        from stream2video.concat.output_lock import acquire_lock_file, release_output_lock
        from stream2video.pipeline_controller import PipelineError

        url = "https://example.com/v"
        cfg = _valid_config(output_dir=tmp_path, input_raw=url)
        cb, calls = _make_callbacks()
        # Another run holds the per-URL-hash lock (16-hex digest —
        # audit round 25 P10).
        url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        holder = acquire_lock_file(tmp_path / f".s2v_url_{url_hash}.lock", what="URL")
        controller = PipelineController(cfg=cfg, cb=cb, cancel_event=threading.Event())
        try:
            with (
                patch.object(PipelineController, "_project_lock_timeout", return_value=0.2),
                pytest.raises(PipelineError, match="URL lock"),
            ):
                controller.run()
            # The refusal was announced, not silent.
            assert any("Another run is processing this source" in m for m in calls["log"])
        finally:
            release_output_lock(holder)
        # After the holder releases, the name is free again.
        lp = acquire_lock_file(tmp_path / f".s2v_url_{url_hash}.lock", what="URL")
        release_output_lock(lp)

    def test_url_run_holds_hash_lock_until_finish(self, tmp_path: Path):
        """While a URL run is in flight, the per-URL-hash lock is held
        (a concurrent acquire is refused); after run() the lock file is
        gone and the name is free."""
        import hashlib
        import threading

        from stream2video.concat.output_lock import acquire_lock_file, release_output_lock
        from stream2video.download import DownloadResult

        url = "https://example.com/v"
        cfg = _valid_config(output_dir=tmp_path, input_raw=url)
        cb, _ = _make_callbacks()
        controller = PipelineController(cfg=cfg, cb=cb, cancel_event=threading.Event())
        fake_video = tmp_path / "vid123-1755000000-a1b2c3d4.mp4"
        fake_video.write_bytes(b"dummy")
        url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        lock_path = tmp_path / f".s2v_url_{url_hash}.lock"

        def fake_download(url, out_dir, **kwargs):
            return DownloadResult(path=fake_video, is_downloaded=True)

        def fake_cut_and_concat(*args, **kwargs):
            args[2].write_bytes(b"output")

        with (
            patch("stream2video.pipeline_controller.download", side_effect=fake_download),
            patch.object(PipelineController, "_downloaded_media_is_valid", return_value=True),
            patch("stream2video.pipeline_controller.detect_silence", return_value=[]),
            patch("stream2video.pipeline_controller.save_silence_cache"),
            patch("stream2video.pipeline_controller.load_silence_cache", return_value=None),
            patch(
                "stream2video.pipeline_controller.cut_and_concat",
                side_effect=fake_cut_and_concat,
            ),
            patch(
                "stream2video.pipeline_controller.generate_keep_segments",
                return_value=[(0.0, 1.0)],
            ),
            patch("stream2video.pipeline_controller.get_video_duration", return_value=10.0),
        ):
            assert lock_path.exists() is False
            controller.run()

        # Locks were released in run()'s finally — the file is gone and
        # the name is free for the next run.
        assert not lock_path.exists()
        assert not list(tmp_path.glob(".s2v_*")), "no project lock files may remain"
        lp = acquire_lock_file(lock_path, what="URL")
        release_output_lock(lp)

    def test_alias_urls_serialize_on_video_id_lock(self, tmp_path: Path):
        """Two DIFFERENT URLs of the same video (alias URLs hash
        differently) collide on the post-download video-id lock — the
        url-hash lock alone cannot see the alias (audit round 24 P4).
        Without an extractor key the identity is host-namespaced
        (audit round 26 P3), so same-host aliases share one lock; the
        lock FILE name is a hash of the readable identity (audit
        round 26 P2)."""
        import threading

        from stream2video.concat.output_lock import acquire_lock_file, release_output_lock
        from stream2video.download import DownloadResult
        from stream2video.paths import project_lock_name
        from stream2video.pipeline_controller import PipelineError

        url = "https://youtu.be/abc123"
        cfg = _valid_config(output_dir=tmp_path, input_raw=url)
        cb, calls = _make_callbacks()
        controller = PipelineController(cfg=cfg, cb=cb, cancel_event=threading.Event())
        fake_video = tmp_path / "vid123-1755000000-a1b2c3d4.mp4"
        fake_video.write_bytes(b"dummy")
        # Another run already holds the project lock for the same
        # host-namespaced video id (its URL hash would be different).
        holder = acquire_lock_file(tmp_path / project_lock_name("youtu.be_vid123"), what="project")

        def fake_download(url, out_dir, **kwargs):
            return DownloadResult(path=fake_video, is_downloaded=True)

        try:
            with (
                patch("stream2video.pipeline_controller.download", side_effect=fake_download),
                patch.object(PipelineController, "_downloaded_media_is_valid", return_value=True),
                patch.object(PipelineController, "_project_lock_timeout", return_value=0.2),
                pytest.raises(PipelineError, match="project lock"),
            ):
                controller.run()
            assert any("Another run is processing this video" in m for m in calls["log"])
        finally:
            release_output_lock(holder)

    def test_completed_download_kept_when_project_lock_refuses(self, tmp_path: Path):
        """The completed download is registered BEFORE the post-download
        project lock is attempted (audit round 25 P3): when that lock
        refuses, the cleanup keeps the fully-downloaded file (partial
        cleanup + was-real flag) and ANNOUNCES it — without the early
        registration the file would be orphaned and silently left
        behind."""
        import threading

        from stream2video.concat.output_lock import acquire_lock_file, release_output_lock
        from stream2video.download import DownloadResult
        from stream2video.paths import project_lock_name
        from stream2video.pipeline_controller import PipelineError

        url = "https://example.com/v"
        cfg = _valid_config(output_dir=tmp_path, input_raw=url)
        cb, calls = _make_callbacks()
        controller = PipelineController(cfg=cfg, cb=cb, cancel_event=threading.Event())
        fake_video = tmp_path / "vid123-1755000000-a1b2c3d4.mp4"
        fake_video.write_bytes(b"dummy")
        # No extractor key → the identity is host-namespaced (audit
        # round 26 P3): example.com_vid123.
        holder = acquire_lock_file(
            tmp_path / project_lock_name("example.com_vid123"), what="project"
        )

        def fake_download(url, out_dir, **kwargs):
            return DownloadResult(path=fake_video, is_downloaded=True)

        try:
            with (
                patch("stream2video.pipeline_controller.download", side_effect=fake_download),
                patch.object(PipelineController, "_downloaded_media_is_valid", return_value=True),
                patch.object(PipelineController, "_project_lock_timeout", return_value=0.2),
                pytest.raises(PipelineError, match="project lock"),
            ):
                controller.run()
            # The completed download is known to the controller and was
            # kept, not unlinked and not orphaned.
            assert fake_video.exists()
            assert any("Keeping completed download for possible reuse" in m for m in calls["log"])
        finally:
            release_output_lock(holder)

    def test_id_lock_namespaced_by_extractor_key(self, tmp_path: Path):
        """The post-download project identity is namespaced by the
        extractor/site when yt-dlp reports one (audit round 25 P2):
        two DIFFERENT sites that happen to use the same video id must
        not serialize/refuse each other. The lock FILE name is a hash
        of the readable identity (audit round 26 P2)."""
        import threading

        from stream2video.concat.output_lock import acquire_lock_file, release_output_lock
        from stream2video.download import DownloadResult
        from stream2video.paths import project_lock_name
        from stream2video.pipeline_controller import PipelineError

        url = "https://example.com/v"
        cfg = _valid_config(output_dir=tmp_path, input_raw=url)
        cb, _ = _make_callbacks()
        controller = PipelineController(cfg=cfg, cb=cb, cancel_event=threading.Event())
        fake_video = tmp_path / "abc123-1755000000-a1b2c3d4.mp4"
        fake_video.write_bytes(b"dummy")
        # A run from a DIFFERENT site whose video id is also "abc123"
        # holds the vimeo-namespaced project lock; our youtube-namespaced
        # identity must NOT collide with it.
        holder = acquire_lock_file(tmp_path / project_lock_name("vimeo_abc123"), what="project")

        def fake_download(url, out_dir, **kwargs):
            return DownloadResult(path=fake_video, is_downloaded=True, extractor_key="youtube")

        try:
            # The namespaced run must NOT hit the foreign lock — it
            # proceeds all the way through (no project lock refusal).
            with (
                patch("stream2video.pipeline_controller.download", side_effect=fake_download),
                patch.object(PipelineController, "_downloaded_media_is_valid", return_value=True),
                patch.object(PipelineController, "_project_lock_timeout", return_value=0.2),
                patch("stream2video.pipeline_controller.detect_silence", return_value=[]),
                patch("stream2video.pipeline_controller.save_silence_cache"),
                patch("stream2video.pipeline_controller.load_silence_cache", return_value=None),
                patch(
                    "stream2video.pipeline_controller.cut_and_concat",
                    side_effect=lambda *a, **k: a[2].write_bytes(b"output"),
                ),
                patch(
                    "stream2video.pipeline_controller.generate_keep_segments",
                    return_value=[(0.0, 1.0)],
                ),
                patch("stream2video.pipeline_controller.get_video_duration", return_value=10.0),
            ):
                controller.run()
        finally:
            release_output_lock(holder)

        # The SAME extractor + id DOES collide: the namespaced identity
        # is what the second run locks.
        holder2 = acquire_lock_file(tmp_path / project_lock_name("youtube_abc123"), what="project")
        try:
            with (
                patch("stream2video.pipeline_controller.download", side_effect=fake_download),
                patch.object(PipelineController, "_downloaded_media_is_valid", return_value=True),
                patch.object(PipelineController, "_project_lock_timeout", return_value=0.2),
                pytest.raises(PipelineError, match="project lock"),
            ):
                controller.run()
        finally:
            release_output_lock(holder2)

    def test_project_lock_timeout_waits_until_cancel(self, tmp_path: Path):
        """The lock wait is UNBOUNDED (audit round 26 P6): a first
        run's real duration has no closed-form ceiling (segment
        encodes, retries, fallbacks multiply), so any computed budget
        under-covers a legitimate run and produces a false refusal
        against a live holder. The wait ends when the holder releases
        or the user cancels (every project lock acquire passes the
        cancel_callback)."""
        import threading

        cfg = _valid_config(output_dir=tmp_path, input_raw="clip.mp4")
        cb, _ = _make_callbacks()
        controller = PipelineController(cfg=cfg, cb=cb, cancel_event=threading.Event())
        assert controller._project_lock_timeout() == float("inf")

    def test_transient_probe_failure_keeps_download(self, tmp_path: Path):
        """A TRANSIENT ffprobe failure means "validation unavailable",
        NOT "media invalid" (audit round 27 P1): the completed download
        must survive (was-real stays True — the cleanup announces and
        keeps it) and the error must say retry."""
        import threading

        from stream2video.download import DownloadResult
        from stream2video.pipeline_controller import PipelineDownloadError

        cfg = _valid_config(output_dir=tmp_path, input_raw="https://example.com/v")
        cb, calls = _make_callbacks()
        controller = PipelineController(cfg=cfg, cb=cb, cancel_event=threading.Event())
        fake_video = tmp_path / "vid123-1755000000-a1b2c3d4.mp4"
        fake_video.write_bytes(b"dummy")

        def fake_download(url, out_dir, **kwargs):
            return DownloadResult(path=fake_video, is_downloaded=True)

        with (
            patch("stream2video.pipeline_controller.download", side_effect=fake_download),
            patch.object(
                PipelineController,
                "_downloaded_media_is_valid",
                side_effect=OSError("transient spawn failure"),
            ),
            pytest.raises(PipelineDownloadError, match="could not run"),
        ):
            controller.run()
        assert fake_video.exists(), "a transient probe failure must not delete the download"
        assert any("Keeping completed download for possible reuse" in m for m in calls["log"])

    def test_invalid_media_deletes_download(self, tmp_path: Path):
        """A GENUINELY invalid download is discarded (was-real drops,
        so the cleanup unlinks it) — the stable source is never
        replaced with garbage (audit round 26 P4)."""
        import threading

        from stream2video.download import DownloadResult
        from stream2video.pipeline_controller import PipelineDownloadError

        cfg = _valid_config(output_dir=tmp_path, input_raw="https://example.com/v")
        cb, _calls = _make_callbacks()
        controller = PipelineController(cfg=cfg, cb=cb, cancel_event=threading.Event())
        fake_video = tmp_path / "vid123-1755000000-a1b2c3d4.mp4"
        fake_video.write_bytes(b"dummy")

        def fake_download(url, out_dir, **kwargs):
            return DownloadResult(path=fake_video, is_downloaded=True)

        with (
            patch("stream2video.pipeline_controller.download", side_effect=fake_download),
            patch.object(PipelineController, "_downloaded_media_is_valid", return_value=False),
            pytest.raises(PipelineDownloadError, match="failed media validation"),
        ):
            controller.run()
        assert not fake_video.exists(), "invalid media must be discarded"

    def test_audio_output_validates_audio_stream(self, tmp_path: Path):
        """An audio-only source is a valid input for audio outputs —
        the fresh-download probe must require the AUDIO stream when
        output_format is mp3/opus/aac/wav/flac (audit round 27 P6)."""
        import threading

        from stream2video.download import DownloadResult
        from stream2video.pipeline_controller import PipelineDownloadError

        cfg = _valid_config(
            output_dir=tmp_path, input_raw="https://example.com/v", output_format="mp3"
        )
        cb, _ = _make_callbacks()
        controller = PipelineController(cfg=cfg, cb=cb, cancel_event=threading.Event())
        fake_video = tmp_path / "vid123-1755000000-a1b2c3d4.mp4"
        fake_video.write_bytes(b"dummy")
        probed: dict = {}

        def fake_probe(path, *, require_video, require_audio):
            probed["require_video"] = require_video
            probed["require_audio"] = require_audio
            return False

        def fake_download(url, out_dir, **kwargs):
            return DownloadResult(path=fake_video, is_downloaded=True)

        with (
            patch("stream2video.pipeline_controller.download", side_effect=fake_download),
            patch.object(PipelineController, "_downloaded_media_is_valid", side_effect=fake_probe),
            pytest.raises(PipelineDownloadError, match="failed media validation"),
        ):
            controller.run()
        assert probed["require_audio"] is True
        assert probed["require_video"] is False

    def test_video_output_validates_video_stream(self, tmp_path: Path):
        import threading

        from stream2video.download import DownloadResult
        from stream2video.pipeline_controller import PipelineDownloadError

        cfg = _valid_config(output_dir=tmp_path, input_raw="https://example.com/v")
        cb, _ = _make_callbacks()
        controller = PipelineController(cfg=cfg, cb=cb, cancel_event=threading.Event())
        fake_video = tmp_path / "vid123-1755000000-a1b2c3d4.mp4"
        fake_video.write_bytes(b"dummy")
        probed: dict = {}

        def fake_probe(path, *, require_video, require_audio):
            probed["require_video"] = require_video
            probed["require_audio"] = require_audio
            return False

        def fake_download(url, out_dir, **kwargs):
            return DownloadResult(path=fake_video, is_downloaded=True)

        with (
            patch("stream2video.pipeline_controller.download", side_effect=fake_download),
            patch.object(PipelineController, "_downloaded_media_is_valid", side_effect=fake_probe),
            pytest.raises(PipelineDownloadError, match="failed media validation"),
        ):
            controller.run()
        assert probed["require_video"] is True

    def test_fresh_download_truncated_audio_body_rejected(self, tmp_path: Path):
        """Audit round 31 P1-4: a fresh video download with a healthy
        video track but a truncated audio body (12 s video / 2 s audio)
        must fail the fresh-download gate BEFORE the stable source is
        replaced. The download phase now requires BOTH streams whenever
        the source carries audio (``has_audio_stream``), and the unified
        ``_media_is_valid`` gate rejects the video/audio duration
        mismatch — so a corrupted audio body can never publish as the
        source."""
        import threading

        from stream2video.download import DownloadResult
        from stream2video.pipeline_controller import PipelineDownloadError

        cfg = _valid_config(output_dir=tmp_path, input_raw="https://example.com/v")
        cb, _ = _make_callbacks()
        controller = PipelineController(cfg=cfg, cb=cb, cancel_event=threading.Event())
        fake_video = tmp_path / "vid123-1755000000-a1b2c3d4.mp4"
        fake_video.write_bytes(b"dummy")

        def fake_download(url, out_dir, **kwargs):
            return DownloadResult(path=fake_video, is_downloaded=True)

        with (
            patch("stream2video.pipeline_controller.download", side_effect=fake_download),
            # The source carries an audio track → the gate must check both.
            patch("stream2video.pipeline_controller.has_audio_stream", return_value=True),
            # Run the REAL validators; only the probe seams are mocked.
            # Durations 12 s video / 2 s audio → unified gate rejects.
            patch("stream2video.concat.probing._ffprobe_media_complete", return_value=True),
            patch("stream2video.concat.probing._ffprobe_is_valid_media", return_value=True),
            patch(
                "stream2video.concat.probing._ffmpeg_decode_timing",
                return_value=(True, 12.0),
            ),
            patch(
                "stream2video.concat.probing._ffprobe_stream_timing",
                side_effect=lambda p, t, **kw: (0.0, 12.0) if t == "v" else (0.0, 2.0),
            ),
            pytest.raises(PipelineDownloadError, match="failed media validation"),
        ):
            controller.run()

    def test_fresh_download_video_only_source_needs_no_audio(self, tmp_path: Path):
        """A video-only source (``has_audio_stream`` False) is validated
        on the video stream ALONE — the fresh-download gate must not
        demand an audio track (audit round 31 P1-4 third bullet)."""
        import threading

        from stream2video.download import DownloadResult
        from stream2video.pipeline_controller import PipelineDownloadError

        cfg = _valid_config(output_dir=tmp_path, input_raw="https://example.com/v")
        cb, _ = _make_callbacks()
        controller = PipelineController(cfg=cfg, cb=cb, cancel_event=threading.Event())
        fake_video = tmp_path / "vid123-1755000000-a1b2c3d4.mp4"
        fake_video.write_bytes(b"dummy")
        probed: dict = {}

        def record(path, *, require_video, require_audio):
            probed["require_video"] = require_video
            probed["require_audio"] = require_audio
            return False  # reject so we can assert the flags without a full run

        def fake_download(url, out_dir, **kwargs):
            return DownloadResult(path=fake_video, is_downloaded=True)

        with (
            patch("stream2video.pipeline_controller.download", side_effect=fake_download),
            patch("stream2video.pipeline_controller.has_audio_stream", return_value=False),
            patch.object(PipelineController, "_downloaded_media_is_valid", side_effect=record),
            pytest.raises(PipelineDownloadError, match="failed media validation"),
        ):
            controller.run()
        assert probed["require_video"] is True
        assert probed["require_audio"] is False, (
            "a video-only source must not require an audio track"
        )

    def test_legacy_project_dir_offered_opt_in_rename(self, tmp_path: Path):
        """A legacy pre-namespace project dir (``<id>/``) is detected,
        the host's callback decides, and the rename is atomic + opt-in
        (audit round 28 P9)."""
        import threading

        from stream2video.download import DownloadResult
        from stream2video.paths import mark_project_dir

        url = "https://example.com/v"
        cfg = _valid_config(output_dir=tmp_path, input_raw=url)
        cb, _ = _make_callbacks()
        offered: list[tuple[Path, Path]] = []
        controller = PipelineController(
            cfg=cfg,
            cb=cb,
            cancel_event=threading.Event(),
            on_legacy_project=lambda legacy, target: offered.append((legacy, target)) or True,
        )
        legacy = tmp_path / "vid123"
        legacy.mkdir()
        mark_project_dir(legacy)
        fake_video = tmp_path / "vid123-1755000000-a1b2c3d4.mp4"
        fake_video.write_bytes(b"dummy")

        def fake_download(url, out_dir, **kwargs):
            return DownloadResult(path=fake_video, is_downloaded=True)

        with (
            patch("stream2video.pipeline_controller.download", side_effect=fake_download),
            patch.object(PipelineController, "_downloaded_media_is_valid", return_value=True),
            patch.object(PipelineController, "_project_lock_timeout", return_value=0.2),
            patch("stream2video.pipeline_controller.detect_silence", return_value=[]),
            patch("stream2video.pipeline_controller.save_silence_cache"),
            patch("stream2video.pipeline_controller.load_silence_cache", return_value=None),
            patch(
                "stream2video.pipeline_controller.cut_and_concat",
                side_effect=lambda *a, **k: a[2].write_bytes(b"output"),
            ),
            patch(
                "stream2video.pipeline_controller.generate_keep_segments",
                return_value=[(0.0, 1.0)],
            ),
            patch("stream2video.pipeline_controller.get_video_duration", return_value=10.0),
        ):
            controller.run()
        assert offered, "the legacy dir must be offered to the host"
        assert offered[0][0] == legacy
        assert offered[0][1] == tmp_path / "example.com_vid123"
        assert not legacy.exists(), "a consented rename moves the legacy dir"
        assert (tmp_path / "example.com_vid123").is_dir()

    def test_legacy_project_dir_declined_is_left_alone(self, tmp_path: Path):
        """Declining the rename keeps the legacy dir untouched; the run
        proceeds with the new layout (audit round 28 P9 — never rename
        user data automatically)."""
        import threading

        from stream2video.download import DownloadResult
        from stream2video.paths import mark_project_dir

        url = "https://example.com/v"
        cfg = _valid_config(output_dir=tmp_path, input_raw=url)
        cb, calls = _make_callbacks()
        controller = PipelineController(
            cfg=cfg,
            cb=cb,
            cancel_event=threading.Event(),
            on_legacy_project=lambda legacy, target: False,
        )
        legacy = tmp_path / "vid123"
        legacy.mkdir()
        mark_project_dir(legacy)
        fake_video = tmp_path / "vid123-1755000000-a1b2c3d4.mp4"
        fake_video.write_bytes(b"dummy")

        def fake_download(url, out_dir, **kwargs):
            return DownloadResult(path=fake_video, is_downloaded=True)

        with (
            patch("stream2video.pipeline_controller.download", side_effect=fake_download),
            patch.object(PipelineController, "_downloaded_media_is_valid", return_value=True),
            patch.object(PipelineController, "_project_lock_timeout", return_value=0.2),
            patch("stream2video.pipeline_controller.detect_silence", return_value=[]),
            patch("stream2video.pipeline_controller.save_silence_cache"),
            patch("stream2video.pipeline_controller.load_silence_cache", return_value=None),
            patch(
                "stream2video.pipeline_controller.cut_and_concat",
                side_effect=lambda *a, **k: a[2].write_bytes(b"output"),
            ),
            patch(
                "stream2video.pipeline_controller.generate_keep_segments",
                return_value=[(0.0, 1.0)],
            ),
            patch("stream2video.pipeline_controller.get_video_duration", return_value=10.0),
        ):
            controller.run()
        assert legacy.is_dir(), "a declined rename must not touch the legacy dir"
        assert any("Keeping the legacy directory as-is" in m for m in calls["log"])
        assert (tmp_path / "example.com_vid123").is_dir()


class TestOutputAtomicPublish:
    """Audit round 30 P0: the concat writes into a STAGING dir and the
    stable output is published with one os.replace AFTER validation —
    a failed rerun can never destroy the previous good result, no
    partial ever appears under the user's filename, and a "success"
    requires full media validation, not just a non-zero size."""

    def _controller(self, tmp_path: Path, **kw):
        import threading

        cfg = _valid_config(output_dir=tmp_path, input_raw=str(tmp_path / "src.mp4"))
        cb, calls = _make_callbacks()
        return (
            PipelineController(cfg=cfg, cb=cb, cancel_event=threading.Event(), **kw),
            calls,
        )

    def _patches(self, tmp_path: Path, cut):
        import contextlib

        from stream2video.download import DownloadResult

        src = tmp_path / "src.mp4"
        src.write_bytes(b"source")
        stack = contextlib.ExitStack()
        stack.enter_context(
            patch(
                "stream2video.pipeline_controller.download",
                return_value=DownloadResult(src, is_downloaded=False),
            )
        )
        stack.enter_context(
            patch("stream2video.pipeline_controller.detect_silence", return_value=[])
        )
        stack.enter_context(patch("stream2video.pipeline_controller.save_silence_cache"))
        stack.enter_context(
            patch("stream2video.pipeline_controller.load_silence_cache", return_value=None)
        )
        stack.enter_context(
            patch(
                "stream2video.pipeline_controller.generate_keep_segments",
                return_value=[(0.0, 1.0)],
            )
        )
        stack.enter_context(
            patch("stream2video.pipeline_controller.get_video_duration", return_value=10.0)
        )
        stack.enter_context(
            patch(
                "stream2video.pipeline_controller.apply_per_video_dir",
                side_effect=lambda o, v, d, per_video_dir=False, namespace=None: (o, v),
            )
        )
        stack.enter_context(
            patch("stream2video.pipeline_controller.cut_and_concat", side_effect=cut)
        )
        return stack

    def _stable_output(self, tmp_path: Path) -> Path:
        return next(p for p in tmp_path.iterdir() if p.suffix == ".mp4" and "compressed" in p.name)

    def test_failed_rerun_keeps_previous_output(self, tmp_path: Path):
        """First run succeeds; the second run's encode fails mid-write
        in STAGING — the previous good output at the stable path was
        never touched (audit round 30 P0)."""
        from stream2video.pipeline_controller import PipelineError

        controller, _ = self._controller(tmp_path)
        good = b"previous good result"

        def first_cut(source, silence_segments, output_video, **kwargs):
            Path(output_video).write_bytes(good)

        with self._patches(tmp_path, first_cut):
            controller.run()
        out = self._stable_output(tmp_path)
        assert out.read_bytes() == good
        assert not list(tmp_path.glob(".*.s2v_staging")), "success removes the staging dir"

        def failing_cut(source, silence_segments, output_video, **kwargs):
            Path(output_video).write_bytes(b"partial garbage")
            raise PipelineError("encode failed")

        with self._patches(tmp_path, failing_cut), pytest.raises(PipelineError):
            controller.run()
        assert out.read_bytes() == good, "failed rerun must leave the previous output intact"
        # The staged partial is gone, but the staging dir survives so
        # the next run can resume from the completed parts (audit round
        # 32 P1-1).
        staging = next(tmp_path.glob(".*.s2v_staging"))
        assert not (staging / out.name).exists(), "staged partial must be deleted"

    def test_invalid_output_keeps_previous_result(self, tmp_path: Path):
        """A finished-but-invalid staged output (media validation
        fails) is discarded — the previous good result at the stable
        path stays (audit round 29 P0-2 / 30 P0)."""
        from stream2video.pipeline_controller import PipelineError

        controller, _ = self._controller(tmp_path)
        good = b"previous good result"

        def first_cut(source, silence_segments, output_video, **kwargs):
            Path(output_video).write_bytes(good)

        with self._patches(tmp_path, first_cut):
            controller.run()
        out = self._stable_output(tmp_path)

        def bad_cut(source, silence_segments, output_video, **kwargs):
            Path(output_video).write_bytes(b"corrupt bytes")

        with (
            self._patches(tmp_path, bad_cut),
            patch.object(PipelineController, "_output_media_is_valid", return_value=False),
            pytest.raises(PipelineError, match="failed media validation"),
        ):
            controller.run()
        assert out.read_bytes() == good
        # The rejected staged output is deleted; the staging dir
        # survives for resume (audit round 32 P1-1).
        staging = next(tmp_path.glob(".*.s2v_staging"))
        assert not (staging / out.name).exists(), "rejected staged output must be deleted"

    def test_staging_removed_on_success(self, tmp_path: Path):
        controller, _ = self._controller(tmp_path)

        def cut(source, silence_segments, output_video, **kwargs):
            Path(output_video).write_bytes(b"fresh result")

        with self._patches(tmp_path, cut):
            controller.run()
        assert not list(tmp_path.glob(".*.s2v_staging"))
        assert self._stable_output(tmp_path).read_bytes() == b"fresh result"

    def test_stale_staging_from_crash_does_not_block_next_run(self, tmp_path: Path):
        """A crashed run leaves the staging dir behind; the NEXT run
        reuses it (stable staging name) and still publishes — the
        stable output is never rolled back to a stale version because
        there is no backup protocol (audit round 30 P0-3)."""
        controller, _ = self._controller(tmp_path)
        good = b"previous good result"

        def first_cut(source, silence_segments, output_video, **kwargs):
            Path(output_video).write_bytes(good)

        with self._patches(tmp_path, first_cut):
            controller.run()
        out = self._stable_output(tmp_path)

        # Simulate crash residue: a staging dir with garbage inside.
        from stream2video.paths import artifact_stem

        stem = artifact_stem(Path(tmp_path / "src.mp4"))
        staging = tmp_path / f".{stem}_compressed.mp4.s2v_staging"
        staging.mkdir(exist_ok=True)
        (staging / "junk.partial").write_bytes(b"junk")

        def cut(source, silence_segments, output_video, **kwargs):
            Path(output_video).write_bytes(b"fresh result")

        with self._patches(tmp_path, cut):
            controller.run()
        assert out.read_bytes() == b"fresh result", "new run publishes a fresh output"
        assert not list(tmp_path.glob(".*.s2v_staging")), "success removes the staging dir"

    def test_final_validation_is_called_on_staged_output_before_publish(self, tmp_path: Path):
        """The autouse conftest fixture swaps ``_output_media_is_valid``
        for a dummy True-returning mock suite-wide. Restore the seam here
        and verify the wiring the fixture hides: final validation must
        run on the STAGED file (not the stable output) before publish,
        and a rejection must block the ``os.replace`` publish."""
        controller, _ = self._controller(tmp_path)
        validated: list[Path] = []

        def recording(self, path, **kw):
            validated.append(path)
            return True

        def cut(source, silence_segments, output_video, **kwargs):
            Path(output_video).write_bytes(b"fresh result")

        with (
            self._patches(tmp_path, cut),
            patch.object(PipelineController, "_output_media_is_valid", recording),
        ):
            controller.run()

        assert len(validated) == 1, "final validation must run exactly once on the output"
        staged = validated[0]
        assert staged.name == self._stable_output(tmp_path).name
        assert ".s2v_staging" in staged.parent.name, (
            "final validation must run on the STAGED file, not the stable output"
        )
        assert self._stable_output(tmp_path).read_bytes() == b"fresh result"

    def test_final_validation_rejection_blocks_publish(self, tmp_path: Path):
        """A rejected staged output must never be moved to the stable
        path (and the previous good result stays intact)."""
        from stream2video.pipeline_controller import PipelineError

        controller, _ = self._controller(tmp_path)
        good = b"previous good result"

        def first_cut(source, silence_segments, output_video, **kwargs):
            Path(output_video).write_bytes(good)

        with self._patches(tmp_path, first_cut):
            controller.run()
        out = self._stable_output(tmp_path)

        def cut(source, silence_segments, output_video, **kwargs):
            Path(output_video).write_bytes(b"corrupt bytes")

        with (
            self._patches(tmp_path, cut),
            patch.object(
                PipelineController,
                "_output_media_is_valid",
                lambda self, path, **kw: False,
            ),
            pytest.raises(PipelineError, match="failed media validation"),
        ):
            controller.run()
        assert out.read_bytes() == good, "rejected output must not reach the stable path"
        staging = next(tmp_path.glob(".*.s2v_staging"))
        assert not (staging / out.name).exists(), "rejected staged output must be deleted"

    def test_output_validation_stream_set_and_audio_duration(self, tmp_path: Path):
        """Audit round 30 P5: a video output whose source had audio must
        carry a fully decodable audio track whose duration is close to
        the video's — a 12 s video with a 2 s audio track passes a
        video-only probe and must NOT pass the publish gate."""
        controller, _ = self._controller(tmp_path)
        path = Path("out.mp4")

        def ok_probe(p, stream_type="v", **kw):
            return True, 12.0

        with (
            patch.object(
                PipelineController,
                "_output_media_is_valid",
                _REAL_OUTPUT_MEDIA_IS_VALID,
            ),
            patch("stream2video.concat.probing._ffprobe_is_valid_media", return_value=True),
            patch(
                "stream2video.concat.probing._ffmpeg_decode_timing",
                side_effect=ok_probe,
            ),
        ):
            # Durations mismatch → invalid.
            with patch(
                "stream2video.concat.probing._ffprobe_stream_timing",
                side_effect=lambda p, t, **kw: (0.0, 12.0) if t == "v" else (0.0, 2.0),
            ):
                assert (
                    controller._output_media_is_valid(path, stream_type="v", expect_audio=True)
                    is False
                )
            # Close durations → valid.
            with patch(
                "stream2video.concat.probing._ffprobe_stream_timing",
                side_effect=lambda p, t, **kw: (0.0, 12.0) if t == "v" else (0.0, 11.8),
            ):
                assert (
                    controller._output_media_is_valid(path, stream_type="v", expect_audio=True)
                    is True
                )
            # Video-only source → audio not required.
            decoded: dict = {}

            def rec(p, stream_type="v", **kw):
                decoded.setdefault(stream_type, 0)
                decoded[stream_type] += 1
                return True, 12.0

            with patch("stream2video.concat.probing._ffmpeg_decode_timing", side_effect=rec):
                assert (
                    controller._output_media_is_valid(path, stream_type="v", expect_audio=False)
                    is True
                )
            assert decoded == {"v": 1}, "video-only validation must not decode audio"

    def test_local_source_lock_keyed_on_artifact_stem(self, tmp_path: Path):
        """A local-file run takes its project lock keyed on the artifact
        stem (the stable project identity for local sources)."""
        import threading

        from stream2video.concat.output_lock import acquire_lock_file, release_output_lock
        from stream2video.paths import artifact_stem
        from stream2video.pipeline_controller import PipelineError

        src = tmp_path / "clip.mp4"
        src.write_bytes(b"dummy")
        cfg = _valid_config(output_dir=tmp_path, input_raw=str(src))
        cb, _ = _make_callbacks()
        controller = PipelineController(cfg=cfg, cb=cb, cancel_event=threading.Event())
        holder = acquire_lock_file(tmp_path / f".s2v_{artifact_stem(src)}.lock", what="source")
        try:
            with (
                patch.object(PipelineController, "_project_lock_timeout", return_value=0.2),
                pytest.raises(PipelineError, match="source lock"),
            ):
                controller.run()
        finally:
            release_output_lock(holder)

    def test_output_lock_handle_passed_to_cut_and_concat(self, tmp_path: Path):
        """The controller takes the OUTPUT lock in the concat phase and
        hands the handle to cut_and_concat (so a DIRECT api caller sees
        the same exclusion); run()'s finally releases it and removes the
        lock file."""
        import threading

        from stream2video.concat.output_lock import lock_path_for
        from stream2video.download import DownloadResult
        from stream2video.paths import artifact_stem

        cfg = _valid_config(output_dir=tmp_path, input_raw=str(tmp_path / "src.mp4"))
        cb, _ = _make_callbacks()
        controller = PipelineController(cfg=cfg, cb=cb, cancel_event=threading.Event())
        fake_video = tmp_path / "src.mp4"
        fake_video.write_bytes(b"dummy")
        captured: dict = {}

        def fake_download(url, out_dir, **kwargs):
            return DownloadResult(path=fake_video, is_downloaded=False)

        def fake_cut_and_concat(*args, **kwargs):
            captured["lock"] = kwargs.get("lock")
            args[2].write_bytes(b"output")

        output_path = (
            tmp_path / artifact_stem(fake_video) / f"{artifact_stem(fake_video)}_compressed.mp4"
        )
        with (
            patch("stream2video.pipeline_controller.download", side_effect=fake_download),
            patch("stream2video.pipeline_controller.detect_silence", return_value=[]),
            patch("stream2video.pipeline_controller.save_silence_cache"),
            patch("stream2video.pipeline_controller.load_silence_cache", return_value=None),
            patch(
                "stream2video.pipeline_controller.cut_and_concat",
                side_effect=fake_cut_and_concat,
            ),
            patch(
                "stream2video.pipeline_controller.generate_keep_segments",
                return_value=[(0.0, 1.0)],
            ),
            patch("stream2video.pipeline_controller.get_video_duration", return_value=10.0),
        ):
            result = controller.run()

        assert isinstance(result, PipelineResult)
        lock = captured["lock"]
        assert lock is not None, "cut_and_concat must receive the output lock handle"
        assert lock.path == lock_path_for(output_path)
        assert lock.fd == -1, "the handle must already be released by run()'s finally"
        assert not lock_path_for(output_path).exists()
        assert not list(tmp_path.glob(".s2v_*")), "no project lock files may remain"


class TestFinalValidationEndToEnd:
    """Audit round 30 test blind spot: the autouse conftest fixture
    stubs ``_output_media_is_valid`` to True for the whole suite, so
    the default pipeline tests never prove the final gate is wired to
    the REAL validator. These tests opt OUT of the stub and exercise
    the real seam end-to-end through ``run()``."""

    @staticmethod
    def _make_real_video(out_path: Path, duration: int = 1) -> None:
        import shutil
        import subprocess

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            pytest.skip("ffmpeg not available")
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=1000:duration={duration}",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=160x120:r=10",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-t",
                str(duration),
                str(out_path),
            ],
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=60,
        )

    def _run_pipeline(self, tmp_path: Path, cut, *, write_src: bytes | None):
        """Full ``run()`` with the non-media seams mocked (download /
        silence / concat) but the REAL output validator installed."""
        import threading

        from stream2video.download import DownloadResult

        cfg = _valid_config(output_dir=tmp_path, input_raw=str(tmp_path / "src.mp4"))
        cb, _ = _make_callbacks()
        controller = PipelineController(cfg=cfg, cb=cb, cancel_event=threading.Event())
        src = tmp_path / "src.mp4"
        if write_src is not None:
            src.write_bytes(write_src)

        with (
            patch(
                "stream2video.pipeline_controller.download",
                return_value=DownloadResult(src, is_downloaded=False),
            ),
            patch("stream2video.pipeline_controller.detect_silence", return_value=[]),
            patch("stream2video.pipeline_controller.save_silence_cache"),
            patch("stream2video.pipeline_controller.load_silence_cache", return_value=None),
            patch(
                "stream2video.pipeline_controller.generate_keep_segments",
                return_value=[(0.0, 1.0)],
            ),
            patch(
                "stream2video.pipeline_controller.apply_per_video_dir",
                side_effect=lambda o, v, d, per_video_dir=False, namespace=None: (o, v),
            ),
            patch("stream2video.pipeline_controller.cut_and_concat", side_effect=cut),
            # Opt out of the autouse True-stub: the REAL validator runs,
            # with real ffmpeg/ffprobe behind it.
            patch.object(
                PipelineController,
                "_output_media_is_valid",
                _REAL_OUTPUT_MEDIA_IS_VALID,
            ),
        ):
            return controller.run()

    def test_e2e_valid_output_passes_real_gate_and_publishes(self, tmp_path: Path):
        """A genuinely decodable staged output passes the REAL gate
        (video full decode + audio full decode + duration match, since
        the real source has audio) and is atomically published."""
        src = tmp_path / "src.mp4"
        self._make_real_video(src, duration=1)

        def cut(source, silence_segments, output_video, **kwargs):
            Path(output_video).write_bytes(src.read_bytes())

        result = self._run_pipeline(tmp_path, cut, write_src=None)
        assert isinstance(result, PipelineResult)
        assert result.output_path.read_bytes() == src.read_bytes()
        assert not list(tmp_path.glob(".*.s2v_staging")), "success removes the staging dir"

    def test_e2e_dummy_output_fails_real_gate_and_is_not_published(self, tmp_path: Path):
        """Dummy bytes fail the REAL validator — the run refuses and no
        ``*_compressed.mp4`` ever appears at the stable path."""

        def cut(source, silence_segments, output_video, **kwargs):
            Path(output_video).write_bytes(b"this is not media")

        with pytest.raises(PipelineConcatError, match="media validation"):
            self._run_pipeline(tmp_path, cut, write_src=b"src placeholder")
        assert not list(tmp_path.glob("*_compressed.mp4"))
        # The invalid staged output is deleted; the staging dir survives
        # for resume (audit round 32 P1-1).
        staging = next(tmp_path.glob(".*.s2v_staging"))
        assert list(staging.glob("*_compressed.mp4")) == []


class TestPipelineControllerTkIsolation:
    """Ни одного Tk call из worker thread.

    PipelineController is the worker-thread surface — if it imported
    Tkinter (or customtkinter), the worker would be able to call widgets
    directly, which is unsafe (Tk widgets are main-thread-only). The
    architectural guard is enforced by static import analysis: the
    module must not import tkinter / customtkinter / PIL.

    A real event-loop test (preview concurrent with pipeline, popup
    close during decode, etc.) would need a running display and is
    deferred — this static check is the cheap regression net that
    catches a careless ``from tkinter import messagebox`` added during
    a refactor.
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
        """``_pipeline_worker`` runs on a background
        thread; reading Tk widgets (``self.combo_*``, ``self.entry_*``,
        ``self.chk_*``, ``self.spin_*``) from there is unsafe because
        Tk widgets are main-thread-only. The GUI snapshots widget
        values in ``_start_pipeline`` (main thread) and passes them as
        args; the worker reads only these local copies + ``self.settings``
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
            "thread. Snapshot the value in _start_pipeline "
            "(main thread) and pass it as a positional arg:\n  " + "\n  ".join(violations)
        )


class TestCleanupIncompleteOnClose:
    """The GUI's on-close cleanup used to chase its OWN
    ``_output_path`` / ``_download_path`` fields — which are NEVER
    populated (the real paths live in the PipelineController, stamped
    by the download/concat phases). ``cleanup_incomplete_on_close`` is
    the controller-side replacement the GUI now calls; it must remove
    truncated sinks, keep a fully-downloaded source for reuse, and
    never raise while the app is tearing down."""

    def _make_callbacks(self) -> tuple[PipelineCallbacks, dict]:
        calls: dict = {"log": []}
        cb = PipelineCallbacks(
            on_progress=lambda f: None,
            on_status=lambda s, force=False: None,
            on_log=calls["log"].append,
            on_info=lambda s: None,
            on_overall=lambda e, r, m: None,
            on_total=lambda t: None,
            on_download_progress=lambda p: None,
            on_pipeline_complete=lambda d: None,
        )
        return cb, calls

    def _controller_with_paths(self, tmp_path: Path):
        cb, calls = _make_callbacks()
        cfg = _valid_config(output_dir=tmp_path, input_raw=str(tmp_path / "src.mp4"))
        controller = PipelineController(
            cfg=cfg, cb=cb, cancel_event=__import__("threading").Event()
        )
        # Stamp the same fields the download/concat phases set.
        partial = tmp_path / "partial_src.mp4.part"
        partial.write_bytes(b"partial data")
        controller._download_path = partial
        controller._download_was_real = True
        # Staged publish (audit round 30 P0): the concat phase writes
        # into a STAGING dir — the truncated output lives there, and
        # the stable path is never touched.
        staging = tmp_path / ".out_compressed.mp4.s2v_staging"
        staging.mkdir()
        output = staging / "out_compressed.mp4"
        output.write_bytes(b"output header only")
        controller._staging_dir = staging
        controller._output_path = output
        return controller, calls, partial, staging

    def test_removes_truncated_output_and_clears_slots(self, tmp_path: Path):
        controller, calls, partial, staging = self._controller_with_paths(tmp_path)

        controller.cleanup_incomplete_on_close()

        # The staged OUTPUT file is removed; the staging dir survives —
        # its work dirs are the resume source for the next run (audit
        # round 32 P1-1). The stable output was never created in the
        # first place.
        assert staging.exists(), "staging dir with resume parts must survive on-close cleanup"
        assert not (staging / "out_compressed.mp4").exists(), (
            "staged truncated output survived on-close cleanup"
        )
        # A completed download is KEPT for reuse (partial_only design —
        # mid-download fragments are handled by download()'s own sweep,
        # which _on_close triggers via the cancel event).
        assert partial.exists(), "completed download must be kept for reuse"
        # Both slots cleared so a later call can't chase stale paths.
        assert controller._download_path is None
        assert controller._output_path is None
        assert any("Deleted incomplete output" in m for m in calls["log"])

    def test_keeps_completed_download_for_reuse(self, tmp_path: Path):
        controller, calls, partial, staging = self._controller_with_paths(tmp_path)
        # Download phase finished successfully — the source is complete;
        # on close (mid concat) we must NOT delete a usable file the user
        # may want to reuse on the next run.
        controller._download_was_real = True

        controller.cleanup_incomplete_on_close()

        assert partial.exists(), "completed download was deleted on close"
        assert staging.exists(), "staging dir with resume parts must survive on-close cleanup"
        assert not (staging / "out_compressed.mp4").exists(), (
            "staged truncated output survived on-close cleanup"
        )
        assert any("Keeping completed download" in m for m in calls["log"])

    def test_never_raises_on_cleanup_failure(self, tmp_path: Path):
        controller, calls, _partial, _output = self._controller_with_paths(tmp_path)

        def _boom(*args, **kwargs):
            raise OSError("locked by another process")

        with patch.object(type(controller), "_cleanup_partial_output", side_effect=_boom):
            controller.cleanup_incomplete_on_close()

        # The download slot was still processed (kept for reuse) and the
        # failure was swallowed into a log line — closing the app must
        # never raise out of the teardown path.
        assert controller._download_path is None
        assert any("Could not clean up output" in m for m in calls["log"])


class TestFinishOutputValidation:
    """Audit #4/#8: ``_finish`` must refuse to report success without a
    real, non-empty output — and must never run delete_after (which
    would destroy the only source copy) when there is no result."""

    def _controller(self, tmp_path: Path, *, delete_after: bool):
        import threading
        import time

        from stream2video.pipeline_controller import _build_progress_plan

        completed: list[dict] = []
        logs: list[str] = []
        cb = PipelineCallbacks(
            on_progress=lambda f: None,
            on_status=lambda s: None,
            on_log=logs.append,
            on_info=lambda s: None,
            on_overall=lambda e, r, m: None,
            on_total=lambda t: None,
            on_download_progress=lambda p: None,
            on_pipeline_complete=completed.append,
        )
        cfg = _valid_config(
            output_dir=tmp_path,
            input_raw=str(tmp_path / "src.mp4"),
            delete_after=delete_after,
        )
        controller = PipelineController(cfg=cfg, cb=cb, cancel_event=threading.Event())
        controller._pipeline_start = time.monotonic() - 30.0
        controller._progress_plan = _build_progress_plan(
            is_downloaded=True, src_duration=100.0, silence_cache_hit=False
        )
        return controller, completed, logs

    def test_missing_output_raises_and_keeps_source(self, tmp_path: Path):
        controller, completed, _logs = self._controller(tmp_path, delete_after=True)
        source = tmp_path / "src.mp4"
        source.write_bytes(b"source data")
        output = tmp_path / "out.mp4"  # never created
        controller._download_path = source
        # Staged publish (audit round 30 P0): concat writes into the
        # staging dir; simulate a run that reached _finish with no
        # staged file at all.
        staging = tmp_path / ".out.mp4.s2v_staging"
        staging.mkdir()
        controller._staging_dir = staging

        with pytest.raises(PipelineConcatError, match="missing"):
            controller._finish(
                video_path=source,
                output_path=output,
                src_size_bytes=len(b"source data"),
                src_duration=None,
                keep_dur=10.0,
            )
        assert not completed, "no success callback may fire without an output"
        assert source.exists(), "delete_after must not destroy the source on failure"

    def test_empty_output_raises_and_keeps_source(self, tmp_path: Path):
        controller, completed, _logs = self._controller(tmp_path, delete_after=True)
        source = tmp_path / "src.mp4"
        source.write_bytes(b"source data")
        output = tmp_path / "out.mp4"
        staging = tmp_path / ".out.mp4.s2v_staging"
        staging.mkdir()
        (staging / "out.mp4").write_bytes(b"")  # zero bytes
        controller._download_path = source
        controller._staging_dir = staging

        with pytest.raises(PipelineConcatError, match="empty"):
            controller._finish(
                video_path=source,
                output_path=output,
                src_size_bytes=len(b"source data"),
                src_duration=None,
                keep_dur=10.0,
            )
        assert not completed, "no success callback may fire for a zero-byte output"
        assert source.exists(), "delete_after must not destroy the source on failure"

    def test_valid_output_reports_success_and_clears_slots(self, tmp_path: Path):
        controller, completed, logs = self._controller(tmp_path, delete_after=True)
        source = tmp_path / "src.mp4"
        source.write_bytes(b"source data")
        output = tmp_path / "out.mp4"
        staging = tmp_path / ".out.mp4.s2v_staging"
        staging.mkdir()
        staged = staging / "out.mp4"
        staged.write_bytes(b"0123456789")
        controller._download_path = source
        controller._staging_dir = staging
        controller._output_path = staged

        result = controller._finish(
            video_path=source,
            output_path=output,
            src_size_bytes=len(b"source data"),
            src_duration=None,
            keep_dur=10.0,
        )
        assert result.dst_size_bytes == 10
        # Staged publish: the validated file was atomically moved to the
        # stable path, and the staging dir was removed.
        assert output.read_bytes() == b"0123456789"
        assert not staging.exists()
        assert len(completed) == 1
        assert completed[0]["dst_size_bytes"] == 10
        # The output is no longer considered an incomplete artifact.
        assert controller._output_path is None
        assert not source.exists(), "delete_after unlinks the source on success"
        assert any("Deleted source" in m for m in logs)
