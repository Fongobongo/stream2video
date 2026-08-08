"""Tests for stream2video.pipeline_worker (extracted from gui.py — Этап 10).

Covers:
  * ``PipelineWorkerParams`` — frozen dataclass, snapshots widget reads.
  * ``PipelineGuiCallbacks`` Protocol — what the worker expects from
    the GUI adapter.
  * ``build_pipeline_config_from_snapshot`` — config dict → PipelineConfig
    construction (field mapping + default fallbacks).
  * ``build_download_progress_callback`` — DownloadProgress → UI calls,
    percentage math, overall-bar fractions.
  * ``build_completion_callback`` — summary dict → status / log lines /
    popup text.
  * ``PipelineWorker.run`` — wires up callbacks, instantiates controller,
    maps ``Pipeline*Error`` subclasses to status / log lines.

The PipelineController itself is heavily tested separately; here we just
verify the GUI-side wiring that wraps it.
"""

from __future__ import annotations

import threading
import time
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from stream2video.download import DownloadProgress
from stream2video.pipeline_worker import (
    PipelineWorker,
    PipelineWorkerParams,
    build_completion_callback,
    build_download_progress_callback,
    build_pipeline_config_from_snapshot,
)


class TestPipelineWorkerParams:
    def test_is_frozen(self):
        # Frozen dataclass so the worker can't mutate it mid-run
        # (P1.10: the GUI snapshots widgets in the main thread and the
        # worker reads only local copies — mutability would defeat that).
        params = PipelineWorkerParams(
            input_raw="vid.mp4",
            output_dir=Path("./out"),
            method="segment",
            encoder="libx264",
            video_quality="medium",
            audio_quality="high",
            download_quality="best",
            force=False,
        )
        with pytest.raises(FrozenInstanceError):
            params.encoder = "h264_nvenc"  # type: ignore[misc]

    def test_defaults_zero_or_false(self):
        # ``per_video_dir`` / ``delete_after`` default to False; the
        # GUI's _start_pipeline passes them explicitly but tests need
        # sane defaults.
        params = PipelineWorkerParams(
            input_raw="vid.mp4",
            output_dir=Path("./out"),
            method="segment",
            encoder="libx264",
            video_quality="medium",
            audio_quality="high",
            download_quality="best",
            force=False,
        )
        assert params.per_video_dir is False
        assert params.delete_after is False


class TestBuildPipelineConfigFromSnapshot:
    def _params(self) -> PipelineWorkerParams:
        return PipelineWorkerParams(
            input_raw="vid.mp4",
            output_dir=Path("./out"),
            method="segment",
            encoder="libx264",
            video_quality="medium",
            audio_quality="high",
            download_quality="best",
            force=True,
            per_video_dir=True,
            delete_after=True,
        )

    def test_maps_params_fields_verbatim(self):
        params = self._params()
        cfg = build_pipeline_config_from_snapshot(
            params, {"threshold": -30.0, "min_silence": 2.0, "margin": 0.5}
        )
        assert cfg.input_raw == "vid.mp4"
        assert cfg.method == "segment"
        assert cfg.encoder == "libx264"
        assert cfg.video_quality == "medium"
        assert cfg.audio_quality == "high"
        assert cfg.download_quality == "best"
        assert cfg.force is True
        assert cfg.per_video_dir is True
        assert cfg.delete_after is True

    def test_pulls_threshold_from_config_dict(self):
        # Slider values are read from the GUI's own ``self.config`` (which
        # is synced from the slider widgets at _start_pipeline time), not
        # from a widget read here.
        params = self._params()
        cfg = build_pipeline_config_from_snapshot(
            params, {"threshold": -42.0, "min_silence": 5.0, "margin": 0.25}
        )
        assert cfg.threshold == -42.0
        assert cfg.min_silence == 5.0
        assert cfg.margin == 0.25

    def test_uses_default_when_key_missing(self):
        # The GUI's config dict may not have every optional key
        # (e.g. ``software_fallback`` was added after initial release;
        # existing user settings.json won't have it). The factory
        # falls back to the same defaults PipelineConfig itself expects.
        params = self._params()
        cfg = build_pipeline_config_from_snapshot(
            params, {"threshold": -30.0, "min_silence": 2.0, "margin": 0.0}
        )
        assert cfg.software_fallback == "ask"
        assert cfg.x264_preset == "medium"
        assert cfg.encoder_threads == "auto"
        assert cfg.output_fps == "source"
        assert cfg.output_format == "video"
        assert cfg.memory_limit_mb == "auto"
        assert cfg.memory_reserve_mb == 2048
        assert cfg.x264_low_memory is False
        assert cfg.use_crf is False
        # gapless_concat defaults to True (A/V drift fix) since 0.3.
        assert cfg.gapless_concat is True
        # Timeout / batch size / min-part defaults
        assert cfg.download_timeout == 28800
        assert cfg.connect_timeout == 300
        assert cfg.no_progress_timeout == 1800
        assert cfg.proxy == ""
        assert cfg.segment_encode_timeout == 600
        assert cfg.final_concat_timeout == 86400
        assert cfg.silence_timeout == 36000
        assert cfg.stall_kill_timeout == 300
        assert cfg.stall_warning_timeout == 120
        assert cfg.waveform_timeout == 300
        assert cfg.batch_chunk_size == 40
        assert cfg.min_part_bytes == 1024

    def test_overrides_used_when_present(self):
        # If the GUI's config has explicit values for keys (e.g. user
        # set a custom batch_chunk_size), the factory passes them
        # through unchanged.
        params = self._params()
        cfg = build_pipeline_config_from_snapshot(
            params,
            {
                "threshold": -30.0,
                "min_silence": 2.0,
                "margin": 0.0,
                "software_fallback": "disabled",
                "x264_preset": "ultrafast",
                "encoder_threads": 4,
                "memory_limit_mb": 1024,
                "batch_chunk_size": 10,
                "output_format": "mp3",
                "use_crf": True,
                "gapless_concat": True,
            },
        )
        assert cfg.software_fallback == "disabled"
        assert cfg.x264_preset == "ultrafast"
        assert cfg.encoder_threads == 4
        assert cfg.memory_limit_mb == 1024
        assert cfg.batch_chunk_size == 10
        assert cfg.output_format == "mp3"
        assert cfg.use_crf is True
        assert cfg.gapless_concat is True

    def test_proxy_used_only_when_active(self):
        # The GUI keeps the proxy address even when the proxy is
        # disabled (so the dialog can re-open prefilled); the factory
        # must forward it to yt-dlp ONLY while proxy_active is on.
        params = self._params()
        cfg_off = build_pipeline_config_from_snapshot(
            params,
            {
                "threshold": -30.0,
                "min_silence": 2.0,
                "margin": 0.0,
                "proxy": "http://127.0.0.1:8080",
                "proxy_active": False,
            },
        )
        assert cfg_off.proxy == ""
        cfg_on = build_pipeline_config_from_snapshot(
            params,
            {
                "threshold": -30.0,
                "min_silence": 2.0,
                "margin": 0.0,
                "proxy": "http://127.0.0.1:8080",
                "proxy_active": True,
            },
        )
        assert cfg_on.proxy == "http://127.0.0.1:8080"


class TestBuildDownloadProgressCallback:
    class _FakeGui:
        def __init__(self):
            self.progress_values: list[float] = []
            self.status_texts: list[str] = []
            self.overall_calls: list[tuple[float, float, bool]] = []

        def ui_progress(self, value: float) -> None:
            self.progress_values.append(value)

        def ui_phase_progress(self, fraction: float) -> None: ...

        def ui_status(self, text: str, *, force: bool = False) -> None:
            self.status_texts.append(text)

        def ui_overall(
            self, phase_elapsed: float, phase_remaining: float | None, more_phases: bool
        ) -> None:
            self.overall_calls.append((phase_elapsed, phase_remaining or 0.0, more_phases))

    def test_shows_zero_percent_at_zero_bytes(self):
        gui = self._FakeGui()
        cb = build_download_progress_callback(gui, start_monotonic=time.monotonic())
        cb(DownloadProgress(downloaded_bytes=0, total_bytes=100_000_000, speed=0, eta=0))
        assert gui.progress_values[0] == 0.0
        assert "0.0%" in gui.status_texts[0]
        assert "Step 1/4: Downloading" in gui.status_texts[0]

    def test_scales_progress_bar_to_5_percent_max(self):
        # Download lives in 0..5% of the overall bar (silence + concat
        # fill the rest). At 50% downloaded, the bar should be ~2.5%.
        gui = self._FakeGui()
        cb = build_download_progress_callback(gui, start_monotonic=time.monotonic())
        cb(
            DownloadProgress(
                downloaded_bytes=50_000_000,
                total_bytes=100_000_000,
                speed=10_000_000,
                eta=5.0,
            )
        )
        # 50% * 0.05 = 0.025 (2.5%)
        assert pytest.approx(gui.progress_values[0], abs=1e-6) == 0.025
        # Status line uses the shared build_download_status format
        # (one-decimal percent, synced with the CLI's description line).
        assert "50.0%" in gui.status_texts[0]
        # Speed formatting flows through; ETA too.
        # Just assert the line contains the ETA marker we configured.
        assert "ETA" in gui.status_texts[0]

    def test_unknown_total_falls_back_to_tiny_elapsed_fraction(self):
        # When yt-dlp doesn't know the total size, the bar should
        # not divide by zero — instead it climbs slowly with elapsed
        # (capped at 0.04 = the 4% mark).
        gui = self._FakeGui()
        cb = build_download_progress_callback(gui, start_monotonic=time.monotonic())
        cb(DownloadProgress(downloaded_bytes=10_000, total_bytes=0, speed=0, eta=0))
        assert 0.0 <= gui.progress_values[0] <= 0.04

    def test_overall_called_with_more_phases_true(self):
        # The download callback always sets ``more_phases=True`` because
        # silence/concat follow it; the overall-bar label shows
        # ``+ ? `` so the user knows more steps remain.
        gui = self._FakeGui()
        cb = build_download_progress_callback(gui, start_monotonic=time.monotonic())
        cb(DownloadProgress(downloaded_bytes=0, total_bytes=100, speed=0, eta=10))
        assert gui.overall_calls[0][2] is True


class TestBuildCompletionCallback:
    class _FakeGui:
        def __init__(self):
            self.status_texts: list[str] = []
            self.log_lines: list[str] = []
            self.cleared_overall = False
            self.popup_texts: list[str] = []
            self.total_calls: list[tuple[float, float | None]] = []
            self.info_texts: list[str] = []
            self.success_styled = False
            self.failure_styled = False

        def ui_status(self, text: str, *, force: bool = False) -> None:
            self.status_texts.append(text)

        def log(self, message: str) -> None:
            self.log_lines.append(message)

        def ui_info(self, text: str) -> None:
            self.info_texts.append(text)

        def clear_overall_label(self) -> None:
            self.cleared_overall = True

        def show_complete_popup(self, text: str) -> None:
            self.popup_texts.append(text)

        def ui_set_success_style(self) -> None:
            self.success_styled = True

        def ui_set_failure_style(self) -> None:
            self.failure_styled = True

        def ui_total(self, elapsed: float, *, overall_est: float | None = None) -> None:
            self.total_calls.append((elapsed, overall_est))

    def _summary(self) -> dict[str, Any]:
        return {
            "src_size_bytes": 100_000_000,
            "src_duration": 60.0,
            "dst_size_bytes": 50_000_000,
            "keep_duration": 30.0,
            "pipeline_seconds": 12.5,
            "output_path": Path("/out/vid.mp4"),
        }

    def test_status_log_lines_popup_total_all_called(self):
        gui = self._FakeGui()
        with patch("stream2video.pipeline_worker.play_completion_sound", return_value=None) as sound:
            cb = build_completion_callback(gui, completion_sound=True)
            cb(self._summary())
        assert gui.status_texts  # one status line set
        assert gui.log_lines  # summary's log lines forwarded
        assert gui.popup_texts  # one popup shown
        assert gui.cleared_overall is True  # bar's eta line cleared
        # ui_total fired with the final figure (no ETA — the pipeline
        # is done, so an estimate would be vacuous).
        assert gui.total_calls == [(12.5, 12.5)]  # (elapsed, overall_est)
        # Green bar + Done line under the log (points 5 and 6).
        assert gui.success_styled is True
        assert any(line.startswith("Done:") for line in gui.info_texts)
        sound.assert_called_once_with(enabled=True)

    def test_completion_sound_warning_is_logged(self):
        gui = self._FakeGui()
        with patch(
            "stream2video.pipeline_worker.play_completion_sound",
            return_value="no audio device",
        ):
            cb = build_completion_callback(gui, completion_sound=True)
            cb(self._summary())
        assert any("[WARN] no audio device" in line for line in gui.log_lines)


class _FakePipelineCallbacks:
    """Stand-in for ``stream2video.pipeline_controller.PipelineCallbacks``:
    records the construction kwargs so a test can assert wiring."""

    last_instance: _FakePipelineCallbacks | None = None

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        _FakePipelineCallbacks.last_instance = self


class _FakePipelineController:
    """Stand-in for ``stream2video.pipeline_controller.PipelineController``:
    records the args and runs side-effect-free (don't call callbacks)."""

    last_instance: _FakePipelineController | None = None
    instantiations: int = 0
    _download_path: Path | None = None

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        _FakePipelineController.last_instance = self
        _FakePipelineController.instantiations += 1

    def run(self) -> None:
        # No-op — the real controller drives the callbacks; here we just
        # verify the worker constructed it with the right config / cb /
        # event / hooks.
        return None


class _FakeGuiCallbacks:
    """Minimal :class:`PipelineGuiCallbacks` Protocol implementation
    for ``PipelineWorker.run`` — records every call so a test can
    assert post-run state."""

    def __init__(self) -> None:
        self.cancel_event = threading.Event()
        self.logs: list[str] = []
        self.status_calls: list[tuple[str, bool]] = []
        self.running_state_changes: list[bool] = []
        self.live_segments_sets: list[tuple[Path, int]] = []
        self.live_segments_pops: list[Path] = []
        self.recent_added: list[Path] = []
        self.encoder_labels: list[tuple[str, str]] = []
        self.failure_style_count = 0

    def log(self, message: str) -> None:
        self.logs.append(message)

    def ui_progress(self, value: float) -> None: ...

    def ui_status(self, text: str, *, force: bool = False) -> None:
        self.status_calls.append((text, force))

    def ui_info(self, text: str) -> None: ...

    def ui_overall(
        self, phase_elapsed: float, phase_remaining: float | None, more_phases: bool
    ) -> None: ...

    def ui_total(self, total_elapsed: float, *, overall_est: float | None = None) -> None: ...

    def ui_phase_progress(self, fraction: float) -> None: ...

    def ui_progress_plan(self, bounds: tuple[float, float, float, float]) -> None: ...

    def ui_set_success_style(self) -> None: ...

    def ui_set_failure_style(self) -> None:
        self.failure_style_count += 1

    def ui_update_output(self, out_dir: Path) -> None: ...

    def ui_update_file_info(self, path: Path) -> None: ...

    def add_to_recent_projects(self, project_path: Path) -> None:
        self.recent_added.append(project_path)

    def set_encoder_label(self, encoder: str, video_quality: str) -> None:
        self.encoder_labels.append((encoder, video_quality))

    def clear_overall_label(self) -> None: ...

    def show_complete_popup(self, text: str) -> None: ...

    def set_running(self, running: bool) -> None:
        self.running_state_changes.append(running)

    def set_live_segments(self, video_path: Path, segments: list[Any]) -> None:
        self.live_segments_sets.append((video_path, len(segments)))

    def pop_live_segments(self, video_path: Path) -> list[Any] | None:
        self.live_segments_pops.append(video_path)
        return None

    def ask_fallback_consent(self) -> bool:
        # Tests: always refuse so the ``ask`` policy raises (we'd need a
        # real Tk to click the dialog). A consent-yes path would need a
        # dedicated fake subclass.
        return False


class TestPipelineWorkerRun:
    def _params(self) -> PipelineWorkerParams:
        return PipelineWorkerParams(
            input_raw="vid.mp4",
            output_dir=Path("./out"),
            method="segment",
            encoder="libx264",
            video_quality="medium",
            audio_quality="high",
            download_quality="best",
            force=False,
        )

    def test_runs_controller_with_built_config_and_callbacks(self):
        # Happy path: the worker constructs the PipelineController with
        # the config, callbacks, the GUI's cancel event, and the two
        # extra hooks (on_live_segment, on_output_resolved).
        gui = _FakeGuiCallbacks()
        worker = PipelineWorker(gui, {"threshold": -30, "min_silence": 2, "margin": 0})

        with (
            patch(
                "stream2video.pipeline_controller.PipelineCallbacks",
                new=_FakePipelineCallbacks,
            ),
            patch(
                "stream2video.pipeline_controller.PipelineController",
                new=_FakePipelineController,
            ),
        ):
            # Guard against stale state from earlier tests.
            _FakePipelineCallbacks.last_instance = None
            _FakePipelineController.last_instance = None
            worker.run(self._params())

        assert _FakePipelineController.last_instance is not None
        assert "cfg" in _FakePipelineController.last_instance.kwargs
        assert "cb" in _FakePipelineController.last_instance.kwargs
        # cancel_event passed straight through from the GUI.
        assert _FakePipelineController.last_instance.kwargs["cancel_event"] is gui.cancel_event
        # on_live_segment and on_output_resolved are wired.
        assert callable(_FakePipelineController.last_instance.kwargs["on_live_segment"])
        assert callable(_FakePipelineController.last_instance.kwargs["on_output_resolved"])
        # Button state restored to "not running" in finally.
        assert gui.running_state_changes[-1] is False
        # The fake controller never resolves a video path, so the worker
        # must not pop Path('.') from the live-segment store.
        assert gui.live_segments_pops == []

    def test_pipeline_cancelled_sets_status_and_logs(self):
        # Cancellation: the user hit Cancel; the controller raised
        # ``PipelineCancelled`` (a subclass of ``PipelineError`` but
        # separately caught so the GUI's "Cancelled" status text isn't
        # displayed as a generic failure).
        gui = _FakeGuiCallbacks()
        worker = PipelineWorker(
            gui,
            {"threshold": -30, "min_silence": 2, "margin": 0, "completion_sound": True},
        )

        class _CancellingController(_FakePipelineController):
            def run(self) -> None:
                from stream2video.pipeline_controller import PipelineCancelled

                raise PipelineCancelled("user cancelled")

        with (
            patch(
                "stream2video.pipeline_controller.PipelineCallbacks",
                new=_FakePipelineCallbacks,
            ),
            patch(
                "stream2video.pipeline_controller.PipelineController",
                new=_CancellingController,
            ),
            patch("stream2video.pipeline_worker.play_completion_sound", return_value=None) as sound,
        ):
            worker.run(self._params())

        assert any("Pipeline cancelled" in m for m in gui.logs)
        assert any(text == "Cancelled" and force is True for text, force in gui.status_calls)
        sound.assert_called_once_with(enabled=True, kind="attention")
        # Button restored even on cancel.
        assert gui.running_state_changes[-1] is False

    def test_pipeline_download_error_logs_failure_message(self):
        gui = _FakeGuiCallbacks()
        worker = PipelineWorker(gui, {"threshold": -30, "min_silence": 2, "margin": 0})

        class _FailingController(_FakePipelineController):
            def run(self) -> None:
                from stream2video.pipeline_controller import PipelineDownloadError

                raise PipelineDownloadError("network impossible")

        with (
            patch(
                "stream2video.pipeline_controller.PipelineCallbacks",
                new=_FakePipelineCallbacks,
            ),
            patch(
                "stream2video.pipeline_controller.PipelineController",
                new=_FailingController,
            ),
            patch("stream2video.pipeline_worker.play_completion_sound", return_value=None) as sound,
        ):
            worker.run(self._params())
        assert any("Download failed" in m for m in gui.logs)
        assert any(
            "Failed: network impossible" in text and force is True
            for text, force in gui.status_calls
        )
        sound.assert_called_once_with(enabled=False, kind="attention")

    def test_pipeline_concat_error_logs_concat_failure(self):
        gui = _FakeGuiCallbacks()
        worker = PipelineWorker(gui, {"threshold": -30, "min_silence": 2, "margin": 0})

        class _FailingController(_FakePipelineController):
            def run(self) -> None:
                from stream2video.pipeline_controller import PipelineConcatError

                raise PipelineConcatError("bad mp4 moov")

        with (
            patch(
                "stream2video.pipeline_controller.PipelineCallbacks",
                new=_FakePipelineCallbacks,
            ),
            patch(
                "stream2video.pipeline_controller.PipelineController",
                new=_FailingController,
            ),
        ):
            worker.run(self._params())
        assert any("bad mp4 moov" in m for m in gui.logs)

    def test_pipeline_unexpected_error_logs_and_status(self):
        gui = _FakeGuiCallbacks()
        worker = PipelineWorker(gui, {"threshold": -30, "min_silence": 2, "margin": 0})

        class _CrashingController(_FakePipelineController):
            def run(self) -> None:
                from stream2video.pipeline_controller import PipelineUnexpectedError

                raise PipelineUnexpectedError("boom")

        with (
            patch(
                "stream2video.pipeline_controller.PipelineCallbacks",
                new=_FakePipelineCallbacks,
            ),
            patch(
                "stream2video.pipeline_controller.PipelineController",
                new=_CrashingController,
            ),
        ):
            worker.run(self._params())
        assert any("Unexpected" in m and "boom" in m for m in gui.logs)
        assert any(text == "Error: boom" and force is True for text, force in gui.status_calls)

    def test_invalid_config_aborts_before_controller_is_created(self):
        # Guard: ``validate_pipeline_config`` must run BEFORE the
        # controller is instantiated, so a bad value (threshold outside
        # [-60, -5], unknown method, ...) surfaces as a clear status +
        # log line rather than a mid-pipeline ``PipelineConcatError``
        # or ffmpeg crash. The controller must never be constructed.
        gui = _FakeGuiCallbacks()
        worker = PipelineWorker(gui, {"threshold": -999, "min_silence": 2, "margin": 0})
        _FakePipelineController.instantiations = 0

        with (
            patch(
                "stream2video.pipeline_controller.PipelineCallbacks",
                new=_FakePipelineCallbacks,
            ),
            patch(
                "stream2video.pipeline_controller.PipelineController",
                new=_FakePipelineController,
            ),
            patch("stream2video.pipeline_worker.play_completion_sound", return_value=None) as sound,
        ):
            worker.run(self._params())

        assert _FakePipelineController.instantiations == 0
        assert any("Invalid configuration" in m and "threshold" in m for m in gui.logs)
        assert any("threshold" in text and force is True for text, force in gui.status_calls)
        assert gui.failure_style_count == 1
        sound.assert_called_once_with(enabled=False, kind="attention")
        # ``finally`` still releases the Start button.
        assert gui.running_state_changes[-1] is False

    def test_delete_after_unlinks_download_path(self, tmp_path: Path):
        # ``delete_after=True`` — the controller's ``_finish`` owns the
        # source-file unlink on success (it also clears
        # ``_download_path`` to None). The worker's run() must NOT
        # re-attempt deletion: after ``_finish`` the attribute is None,
        # so any worker-side unlink block would silently never fire (the
        # historical version did exactly that — dead code masked by this
        # test's previous fake whose ``run()`` was a no-op and left
        # ``_download_path`` set). This test now exercises the REAL
        # ``_finish`` via a thin subclass that fills a fake summary
        # (download path + output file) and delegates to the controller,
        # so the deletion is the controller's, not the worker's.
        gui = _FakeGuiCallbacks()
        worker = PipelineWorker(gui, {"threshold": -30, "min_silence": 2, "margin": 0})
        source = tmp_path / "src.mp4"
        source.write_bytes(b"data")
        output = tmp_path / "out" / "video_compressed.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"out")

        from stream2video.pipeline_controller import PipelineController

        class _FinishingController(PipelineController):
            """Mimic a controller that already reached the finish
            phase: seed the download path + the (post-finish) None so
            the real ``_finish`` path runs the unlink."""

            _download_path: Path | None = source  # type: ignore[misc]

            def run(self) -> None:
                # Skip the full pipeline; just exercise _finish, which
                # is the real owner of the delete-after unlink.
                self._finish(
                    video_path=source,
                    output_path=output,
                    src_size_bytes=len(b"data"),
                    src_duration=None,
                    keep_dur=10.0,
                )

        with (
            patch(
                "stream2video.pipeline_controller.PipelineController",
                new=_FinishingController,
            ),
        ):
            params = PipelineWorkerParams(
                input_raw="vid.mp4",
                output_dir=tmp_path,
                method="segment",
                encoder="libx264",
                video_quality="medium",
                audio_quality="high",
                download_quality="best",
                force=False,
                delete_after=True,
            )
            worker.run(params)
        assert not source.exists()
        assert any("Deleted source" in m for m in gui.logs)
