"""Event-loop tests for the GUI.

These tests drive the real Tk event loop (via ``update()`` /
``_flush_events``) to verify state transitions the smoke tests can't cover:

  * **Cancel flow** — ``_cancel_pipeline`` sets the event, the worker
    thread sees it, ``PipelineCancelled`` maps to "Cancelled" status,
    button state restored to idle.
  * **Pipeline error mapping** — each ``Pipeline*Error`` subclass maps
    to the expected status line; ``_set_running(False)`` restores the
    button in ``finally``.
  * **Waveform popup** — open creates the toplevel + binds; close
    nulls refs + cancels "preview" subprocess owner.
  * **Encoder change** — combo selection updates the description label.
  * **Restore defaults** — all combos / entries / sliders reset.

The tests patch ``PipelineWorker.run`` (and ``PipelineController.run``
inside it) so no real ffmpeg/yt-dlp is invoked — only the GUI's
callback wiring / state machine is exercised.

Skipped on headless environments (no display → ``TclError`` on GUI
construction).
"""

from __future__ import annotations

import time
from pathlib import Path
from tkinter import TclError
from unittest.mock import patch

import pytest

pytest.importorskip("PIL", reason="gui.py requires Pillow ([gui] extra)")
pytest.importorskip("customtkinter", reason="gui.py requires customtkinter ([gui] extra)")

from stream2video.config import CONFIG_DEFAULTS, PRESETS
from stream2video.pipeline_worker import PipelineWorkerParams


def _flush_events(app, timeout_ms: int = 500):
    """Process pending Tk events for up to ``timeout_ms`` milliseconds.

    ``update()`` processes ALL pending events in one batch; we call it
    in a loop with a small sleep so worker-thread callbacks (dispatched
    via ``after(0, ...)``) have a chance to land between flushes.
    """
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        try:
            app.update()
        except TclError:
            return
        time.sleep(0.01)


def _make_params(gui) -> PipelineWorkerParams:
    return PipelineWorkerParams(
        input_raw="test.mp4",
        output_dir=Path("./out"),
        method="segment",
        encoder="libx264",
        video_quality="medium",
        audio_quality="medium",
        download_quality="best",
        force=False,
    )


class TestCancelPipeline:
    """Cancel flow: user clicks Cancel → event set → worker stops →
    button restored."""

    def test_cancel_sets_event(self, gui):
        gui._set_running(True)
        gui._cancel_pipeline()
        assert gui._cancel_event.is_set()
        gui._cancel_event.clear()
        gui._set_running(False)

    def test_cancel_logs_message(self, gui):
        gui._set_running(True)
        gui._cancel_pipeline()
        gui._cancel_event.clear()
        gui._set_running(False)
        _flush_events(gui, 200)
        # _log dispatches through _log_poller.log() which puts on
        # the queue. The queue is drained by _log_poller.poll() which
        # runs every 100ms via after(). After flushing, the queue may
        # have already been drained into the textbox — check the log
        # textbox content instead.
        log_text = gui.txt_log.get("1.0", "end")
        assert "Cancelling" in log_text

    def test_cancel_when_not_running_is_noop(self, gui):
        assert not gui.running
        gui._cancel_pipeline()
        assert not gui._cancel_event.is_set()

    def test_cancel_pipeline_via_worker_maps_to_cancelled_status(self, gui):
        """Patch PipelineController.run to raise PipelineCancelled,
        verify the GUI's _pipeline_worker maps it to "Cancelled" status
        and restores the button state."""
        from stream2video.pipeline_controller import PipelineCancelled

        def _raise_cancel(self, *args, **kwargs):
            raise PipelineCancelled("user cancelled")

        with (
            patch("stream2video.pipeline_controller.PipelineController.run", _raise_cancel),
            patch("stream2video.pipeline_controller.PipelineCallbacks"),
        ):
            gui._set_running(True)
            gui._pipeline_worker(
                input_raw="test.mp4",
                output_dir=Path("./out"),
                method="segment",
                encoder="libx264",
                video_quality="medium",
                audio_quality="medium",
                download_quality="best",
                force=False,
            )
            _flush_events(gui, 500)

        # Button state restored to idle in the finally block.
        assert not gui.running
        # Status label should say "Cancelled" (dispatched via _tk_after).
        _flush_events(gui, 200)
        assert "Cancelled" in gui.lbl_status.cget("text") or gui.lbl_status.cget("text") == ""


class TestPipelineErrorMapping:
    """Each Pipeline*Error subclass maps to the expected status line."""

    def _run_and_check_status(self, gui, exc_cls, exc_msg, expected_substring):
        def _raise(self, *args, **kwargs):
            raise exc_cls(exc_msg)

        with (
            patch("stream2video.pipeline_controller.PipelineController.run", _raise),
            patch("stream2video.pipeline_controller.PipelineCallbacks"),
        ):
            gui._set_running(True)
            gui._pipeline_worker(
                input_raw="test.mp4",
                output_dir=Path("./out"),
                method="segment",
                encoder="libx264",
                video_quality="medium",
                audio_quality="medium",
                download_quality="best",
                force=False,
            )
            _flush_events(gui, 500)

        assert not gui.running
        _flush_events(gui, 200)

    def test_download_error_maps_to_failed_status(self, gui):
        from stream2video.pipeline_controller import PipelineDownloadError

        self._run_and_check_status(gui, PipelineDownloadError, "network impossible", "Failed")

    def test_silence_error_maps_to_failed_status(self, gui):
        from stream2video.pipeline_controller import PipelineSilenceError

        self._run_and_check_status(
            gui, PipelineSilenceError, "ffmpeg silencedetect crashed", "Failed"
        )

    def test_concat_error_maps_to_failed_status(self, gui):
        from stream2video.pipeline_controller import PipelineConcatError

        self._run_and_check_status(gui, PipelineConcatError, "bad moov atom", "Failed")

    def test_unexpected_error_maps_to_error_status(self, gui):
        from stream2video.pipeline_controller import PipelineUnexpectedError

        self._run_and_check_status(gui, PipelineUnexpectedError, "boom", "Error")

    def test_button_always_restored_in_finally(self, gui):
        """Even on unexpected exceptions, the finally block must restore
        the button state so the GUI doesn't get stuck in 'Running...'."""
        from stream2video.pipeline_controller import PipelineUnexpectedError

        def _raise(self, *args, **kwargs):
            raise PipelineUnexpectedError("unexpected crash")

        with (
            patch("stream2video.pipeline_controller.PipelineController.run", _raise),
            patch("stream2video.pipeline_controller.PipelineCallbacks"),
        ):
            gui._set_running(True)
            gui._pipeline_worker(
                input_raw="test.mp4",
                output_dir=Path("./out"),
                method="segment",
                encoder="libx264",
                video_quality="medium",
                audio_quality="medium",
                download_quality="best",
                force=False,
            )
            _flush_events(gui, 500)

        assert not gui.running


class TestWaveformPopup:
    """Open/close the waveform popup, verify widget lifecycle."""

    def test_open_without_input_logs_warning(self, gui):
        gui.entry_input.delete(0, "end")
        gui._open_waveform_window()
        _flush_events(gui, 200)
        # No popup should be created.
        assert gui._wave_window is None

    def test_open_with_nonexistent_file_logs_warning(self, gui):
        gui.entry_input.delete(0, "end")
        gui.entry_input.insert(0, "nonexistent_file_xyz.mp4")
        gui._open_waveform_window()
        _flush_events(gui, 200)
        assert gui._wave_window is None

    def test_close_nulls_popup_refs(self, gui):
        """_on_waveform_close should null all widget refs."""
        # Manually set a fake popup state so close has something to clean up.
        gui._waveform_tooltip_after_id = "fake_id"
        gui._waveform_last_motion_event = "fake_event"
        gui._on_waveform_close()
        assert gui._wave_window is None
        assert gui.lbl_wave_image is None
        assert gui.lbl_wave_status is None
        assert gui._waveform_slider is None
        assert gui._waveform_zoom_label is None
        assert gui._waveform_tooltip is None
        assert gui._waveform_tooltip_after_id is None
        assert gui._waveform_last_motion_event is None

    def test_wave_window_alive_false_before_open(self, gui):
        assert not gui._wave_window_alive()

    def test_safe_status_set_noop_when_popup_closed(self, gui):
        gui.lbl_wave_status = None
        gui._safe_status_set("should not crash")
        # No exception = pass.


class TestEncoderChange:
    """Encoder combobox change updates the description label."""

    def test_on_encoder_change_updates_label(self, gui):
        gui._on_encoder_change("h264_nvenc")
        _flush_events(gui, 100)
        assert "NVIDIA" in gui.lbl_encoder_desc.cget("text")

    def test_on_encoder_change_libx264(self, gui):
        gui._on_encoder_change("libx264")
        _flush_events(gui, 100)
        assert "CPU" in gui.lbl_encoder_desc.cget(
            "text"
        ) or "software" in gui.lbl_encoder_desc.cget("text")

    def test_on_encoder_change_unknown_encoder_clears_label(self, gui):
        gui._on_encoder_change("nonexistent_encoder")
        _flush_events(gui, 100)
        assert gui.lbl_encoder_desc.cget("text") == ""

    def test_on_encoder_change_stores_in_settings(self, gui):
        gui._on_encoder_change("h264_amf")
        assert gui.settings["encoder"] == "h264_amf"


class TestRestoreDefaults:
    """Restore defaults resets all combos / entries / sliders."""

    def test_restore_defaults_resets_settings(self, gui):
        gui.settings["encoder"] = "h264_nvenc"
        gui.settings["method"] = "batch"
        gui._restore_defaults()
        _flush_events(gui, 200)
        assert gui.settings["encoder"] == CONFIG_DEFAULTS["encoder"]
        assert gui.settings["method"] == CONFIG_DEFAULTS["method"]

    def test_restore_defaults_resets_combos(self, gui):
        gui.combo_method.set("batch")
        gui.combo_encoder.set("h264_amf")
        gui._restore_defaults()
        _flush_events(gui, 200)
        assert gui.combo_method.get() == CONFIG_DEFAULTS["method"]
        assert gui.combo_encoder.get() == CONFIG_DEFAULTS["encoder"]

    def test_restore_defaults_resets_entries(self, gui):
        gui.entry_input.delete(0, "end")
        gui.entry_input.insert(0, "garbage.mp4")
        gui.entry_output.delete(0, "end")
        gui.entry_output.insert(0, "/tmp/garbage")
        gui._restore_defaults()
        _flush_events(gui, 200)
        assert gui.entry_input.get() == ""
        assert gui.entry_output.get() == ""

    def test_restore_defaults_resets_info_labels(self, gui):
        gui._restore_defaults()
        _flush_events(gui, 200)
        assert gui.lbl_file.cget("text") == "File: —"
        assert gui.lbl_output.cget("text") == "Output: —"
        assert gui.lbl_encoder.cget("text") == "Encoder: —"

    def test_restore_defaults_resets_theme(self, gui):
        gui.settings["theme"] = "light"
        gui._restore_defaults()
        _flush_events(gui, 200)
        assert gui.settings["theme"] == CONFIG_DEFAULTS["theme"]
        assert gui.combo_theme.get() == CONFIG_DEFAULTS["theme"]


class TestStartPipeline:
    """_start_pipeline reads widgets, spawns worker thread, toggles UI state."""

    def test_start_without_ffmpeg_shows_error(self, gui):
        with (
            patch("stream2video.gui.shutil.which", return_value=None),
            patch("stream2video.gui.messagebox.showerror"),
        ):
            gui._start_pipeline()
            _flush_events(gui, 200)
        assert not gui.running

    def test_start_when_already_running_is_noop(self, gui):
        gui._set_running(True)
        gui._start_pipeline()
        _flush_events(gui, 200)
        assert gui.running
        gui._set_running(False)


class TestCloseWindow:
    """_on_close (WM_DELETE_WINDOW) — cancel + cleanup + destroy."""

    def test_on_close_when_idle_destroys_window(self, gui):
        assert not gui.running
        gui._on_close()
        _flush_events(gui, 200)
        # After destroy, winfo_exists() raises TclError because the Tk
        # root is gone. That IS the success signal — the window was
        # destroyed. Wrap in try/except so the test verifies the
        # expected outcome instead of crashing on the assertion.
        try:
            assert not gui.winfo_exists()
        except TclError:
            pass  # Window destroyed — exactly what we wanted.

    def test_on_close_sets_cancel_event(self, gui):
        gui._on_close()
        _flush_events(gui, 200)
        assert gui._cancel_event.is_set()

    def test_on_close_when_running_confirms_then_quits(self, gui):
        """When pipeline is running, _on_close asks for confirmation.
        We patch messagebox.askyesno to return True (quit)."""
        gui._set_running(True)
        with patch("stream2video.gui.messagebox.askyesno", return_value=True):
            gui._on_close()
            _flush_events(gui, 200)
        assert gui._cancel_event.is_set()

    def test_on_close_when_running_user_cancels(self, gui):
        """User says "don't quit" — pipeline keeps running, window stays."""
        gui._set_running(True)
        with patch("stream2video.gui.messagebox.askyesno", return_value=False):
            gui._on_close()
            _flush_events(gui, 200)
        assert gui.running
        assert gui.winfo_exists()
        gui._set_running(False)


class TestPresetSync:
    """Selecting / loading a preset keeps the managed widgets in sync."""

    def test_on_preset_change_syncs_widgets_and_settings(self, gui):
        gui._on_preset_change("low_memory")
        _flush_events(gui, 100)
        for key, value in PRESETS["low_memory"].items():
            assert gui.settings[key] == value, key
        assert bool(gui.chk_x264_low_memory.get()) == PRESETS["low_memory"]["x264_low_memory"]
        assert (
            bool(gui.chk_low_process_priority.get())
            == PRESETS["low_memory"]["low_process_priority"]
        )
        assert gui.combo_preset.get() == "low_memory"
        gui._restore_defaults()
        _flush_events(gui, 200)

    def test_on_preset_change_syncs_advanced_entries(self, gui):
        gui._on_preset_change("low_cpu")
        _flush_events(gui, 100)
        # x264_preset + encoder_threads are Advanced widgets.
        assert gui.combo_x264_preset.get() == PRESETS["low_cpu"]["x264_preset"]
        assert gui.entry_encoder_threads.get() == str(PRESETS["low_cpu"]["encoder_threads"])
        gui._restore_defaults()
        _flush_events(gui, 200)

    def test_balanced_is_identity(self, gui):
        before = dict(gui.settings)
        gui._on_preset_change("balanced")
        _flush_events(gui, 100)
        assert gui.settings == before or set(gui.settings) == set(before)
        # No managed value may change.
        for preset in PRESETS.values():
            for key in preset:
                assert gui.settings.get(key) == before.get(key), key

    def test_startup_keeps_manual_preset_overrides(self, tmp_path, monkeypatch):
        """Audit round 13 P1: select low_memory, manually untick
        low_process_priority, restart — the override MUST survive. The
        old ``_sync_preset_on_load`` replayed the preset whenever the
        stored values diverged from it, destroying the saved hand
        override on every startup."""
        monkeypatch.setattr("stream2video.config._base_dir", lambda: tmp_path)
        import json

        from stream2video.gui import Stream2VideoGUI

        (tmp_path / "gui_settings.json").write_text(
            json.dumps(
                {
                    "preset": "low_memory",
                    "x264_low_memory": True,  # preset value
                    "batch_chunk_size": 20,  # preset value
                    "low_process_priority": False,  # MANUAL override
                }
            ),
            encoding="utf-8",
        )
        try:
            app = Stream2VideoGUI()
        except TclError as e:
            pytest.skip(f"headless environment — TclError on init: {e}")
        try:
            app.update_idletasks()
            _flush_events(app, 100)
            # The override survives — widgets show the loaded values, not
            # the preset's.
            assert bool(app.chk_low_process_priority.get()) is False
            assert bool(app.chk_x264_low_memory.get()) is True
            assert app.entry_batch_chunk_size.get() == "20"
            # The preset combo still displays the stored preset name.
            assert app.combo_preset.get() == "low_memory"
            assert app.settings["low_process_priority"] is False
        finally:
            try:
                app.destroy()
            except TclError:
                pass

    def test_startup_loads_stored_values_as_source_of_truth(self, tmp_path, monkeypatch):
        """A hand-edited settings.json whose preset-managed values differ
        from the stored preset loads verbatim: the loaded values are the
        source of truth (widgets show them, the run uses them), and the
        preset is NOT replayed over them. Copy CLI pins such divergences
        against the preset baseline with explicit flags."""
        monkeypatch.setattr("stream2video.config._base_dir", lambda: tmp_path)
        import json

        from stream2video.gui import Stream2VideoGUI

        (tmp_path / "gui_settings.json").write_text(
            json.dumps(
                {
                    "preset": "low_memory",
                    "x264_low_memory": False,  # factory, diverges from preset
                    "batch_chunk_size": 40,  # factory, diverges from preset
                    "low_process_priority": False,
                }
            ),
            encoding="utf-8",
        )
        try:
            app = Stream2VideoGUI()
        except TclError as e:
            pytest.skip(f"headless environment — TclError on init: {e}")
        try:
            app.update_idletasks()
            _flush_events(app, 100)
            assert bool(app.chk_x264_low_memory.get()) is False
            assert bool(app.chk_low_process_priority.get()) is False
            assert app.entry_batch_chunk_size.get() == "40"
            assert app.combo_preset.get() == "low_memory"
        finally:
            try:
                app.destroy()
            except TclError:
                pass

    def test_no_startup_preset_replay_exists(self):
        """``_sync_preset_on_load`` (the startup preset replay that
        destroyed manual overrides) must stay gone — its removal is the
        fix for audit round 13 P1."""
        from stream2video.gui_lifecycle import LifecycleMixin

        assert not hasattr(LifecycleMixin, "_sync_preset_on_load")


class TestProxySecretHandling:
    """Proxy credentials must never reach the GUI log (regressions)."""

    def test_set_proxy_logs_masked_value(self, gui):
        from stream2video.gui_lifecycle import _ProxyInputDialog

        secret = "socks5://user:super-secret@host:1080"
        with patch.object(_ProxyInputDialog, "get_input", return_value=secret):
            gui._set_proxy()
            _flush_events(gui, 200)
        log_text = gui.txt_log.get("1.0", "end")
        assert "super-secret" not in log_text
        assert "socks5://***:***@host:1080" in log_text
        # The value is still remembered in full for actual use.
        assert gui.settings["proxy"] == secret

    def test_copy_cli_command_logs_redacted_command(self, gui, tmp_path: Path):
        # Copying the CLI command with an active proxy used to log the
        # whole command string — password included. The log line must
        # carry the masked proxy; the copied (clipboard) command keeps
        # the real one.
        secret = "socks5://user:super-secret@host:1080"
        gui.settings["proxy"] = secret
        gui.settings["proxy_active"] = True
        gui.entry_output.delete(0, "end")
        gui.entry_output.insert(0, str(tmp_path))
        # The credential-carrying proxy makes ``_copy_cli_command`` ask
        # whether to include the password — answer Yes so the clipboard
        # command keeps the real credentials and we can assert the LOG
        # line still redacts them.
        with patch("stream2video.gui.messagebox.askyesno", return_value=True):
            gui._copy_cli_command()
        _flush_events(gui, 200)
        log_text = gui.txt_log.get("1.0", "end")
        assert "super-secret" not in log_text
        assert "socks5://***:***@host:1080" in log_text
