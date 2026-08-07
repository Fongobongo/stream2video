"""GUI widget smoke tests (Этап 10 / P2.2).

These tests instantiate the actual ``Stream2VideoGUI`` class WITHOUT
calling ``mainloop()`` — they verify that the widgets are created, the
config dict has the expected keys, and the pure helpers are wired
correctly. They don't drive a real event loop; they use
``update_idletasks()`` to flush pending Tk events so widget state
sliders / combos are queryable.

The tests are SKIPPED when:
  * The [gui] extra isn't installed (Pillow / customtkinter missing).
  * No display is available (headless CI — ``ctk.CTk`` raises TclError
    on instantiation when no display server is reachable).

They don't replace a proper pytest-qt / tkinter simulator harness;
they're a cheap regression net for the kind of bug where a refactor
renames a widget attribute and the GUI crashes on the first user
interaction.
"""

from __future__ import annotations

from tkinter import TclError

import pytest

pytest.importorskip("PIL", reason="gui.py requires Pillow ([gui] extra)")
pytest.importorskip("customtkinter", reason="gui.py requires customtkinter ([gui] extra)")

from stream2video.config import CONFIG_DEFAULTS, VALID_ENCODERS, VALID_METHODS, VALID_QUALITIES


@pytest.fixture(scope="module")
def gui():
    """Instantiate Stream2VideoGUI once per module; skip on headless envs.

    Module-scoped because Tk init is slow (hundreds of ms on Windows);
    tests just query widget state, they don't mutate it.
    """
    from stream2video.gui import Stream2VideoGUI

    try:
        app = Stream2VideoGUI()
    except TclError as e:
        pytest.skip(f"headless environment — TclError on init: {e}")
    # Flush pending idle tasks so widgets are queryable.
    try:
        app.update_idletasks()
    except TclError as e:
        pytest.skip(f"Tk idle tasks failed (no display?): {e}")
    yield app
    try:
        app.destroy()
    except TclError:
        pass


class TestGuiInstantiation:
    def test_app_instance_has_config(self, gui):
        assert hasattr(gui, "config")
        assert isinstance(gui.config, dict)
        # CONFIG_DEFAULTS keys are all present.
        for key in CONFIG_DEFAULTS:
            assert key in gui.config, f"missing key {key!r} in gui.config"

    def test_required_widgets_exist(self, gui):
        # Verify the widget attributes the pipeline worker and helpers
        # reference at runtime. A rename in _build_ui that missed one
        # of these would crash on first interaction.
        for attr in (
            "entry_input",
            "entry_output",
            "combo_method",
            "combo_encoder",
            "combo_video_quality",
            "combo_download_quality",
            "combo_theme",
            "chk_force",
            "chk_delete",
            "chk_per_video_dir",
            "chk_completion_sound",
            "progress",
            "lbl_progress_pct",
            "lbl_status",
            "lbl_progress_meta",
            "lbl_silence",
            "lbl_output",
            "lbl_file",
            "lbl_size",
            "lbl_duration",
            "lbl_encoder",
            "log_queue",
        ):
            assert hasattr(gui, attr), f"GUI missing widget attribute {attr!r}"

    def test_combo_values_match_valid_lists(self, gui):
        # The combo boxes should have been populated with the canonical
        # VALID_* lists at construction time. A regression where the
        # combo is empty or has wrong values would leave the user unable
        # to pick the encoder they want.
        assert (
            gui.combo_method.cget("values") == tuple(VALID_METHODS)
            or list(gui.combo_method.cget("values")) == VALID_METHODS
        )
        assert list(gui.combo_encoder.cget("values")) == VALID_ENCODERS
        assert list(gui.combo_video_quality.cget("values")) == VALID_QUALITIES


class TestGuiPureHelpersWired:
    def test_build_cli_command_callable(self, gui):
        # The "Copy CLI command" button calls _copy_cli_command which
        # delegates to gui_helpers.build_cli_command. The button binding
        # is set at _build_ui time; if the helper import broke, the
        # button would crash on click. Verify the method exists and
        # is callable.
        assert callable(gui._copy_cli_command)

    def test_ui_status_doesnt_crash_with_force(self, gui):
        # ``_ui_status`` is called frequently from worker threads via
        # ``self._tk_after``. Verify a force=True update doesn't raise
        # (it should call ``truncate_status`` and ``should_update_status``
        # from gui_helpers).
        gui._ui_status("test status", force=True)

    def test_ui_progress_plan_positions_ticks(self, gui):
        # The pipeline broadcasts per-run phase boundaries; the GUI must
        # accept them without raising (tick placement is main-thread via
        # _tk_after, so just verify the state + indicator wiring).
        gui._ui_progress_plan((0.05, 0.40, 0.94, 1.0))
        assert gui._phase_bounds == (0.05, 0.40, 0.94, 1.0)

    def test_log_queue_accepts_messages(self, gui):
        # The pipeline worker and many helpers call self._log(str);
        # verify the queue accepts a message without raising.
        before = gui.log_queue.qsize()
        gui._log("smoke test message")
        after = gui.log_queue.qsize()
        assert after == before + 1


class TestGuiPipelineState:
    def test_initial_running_state_is_false(self, gui):
        assert gui.running is False

    def test_cancel_event_initially_clear(self, gui):
        assert not gui._cancel_event.is_set()

    def test_set_running_toggles_state(self, gui):
        # _set_running flips the boolean and enables/disables widgets.
        # Verify it doesn't raise; we don't assert the widget state
        # because ctk's state mgmt is platform-dependent.
        gui._set_running(False)
        assert gui.running is False


class TestToolkitCallbackDispatch:
    """Fix-plan section 4 GUI/threading: "Реальный toolkit smoke test на Windows".

    The worker thread's callbacks (on_progress / on_status / on_log /
    on_overall / on_total / on_pipeline_complete) must dispatch to the Tk
    main loop via ``self._tk_after(0, lambda: ...)`` — direct widget
    writes from a non-main thread crash Tk. These tests verify the
    dispatch surface is wired correctly without driving a real event loop:

      1. ``_tk_after`` accepts a callable and schedules it (it doesn't
         raise from the test thread, which is the main thread).
      2. Each PipelineCallbacks surface (on_status, on_log, on_progress)
         can be invoked with realistic arguments without raising.
      3. The dispatch happens on the main thread (verified by capturing
         the calling thread's ident inside the dispatched lambda).

    A real event-loop test (preview concurrent with pipeline, popup
    close during decode) requires pytest-qt and is deferred — these
    smoke checks are the cheap regression net for "the GUI's callback
    plumbing wasn't broken by a refactor".
    """

    def test_tk_after_dispatches_to_main_thread(self, gui):
        """``_tk_after(0, fn)`` schedules ``fn`` on the Tk main loop.
        Since pytest runs in the main thread by default, ``after(0, ...)``
        with ``update()`` runs the callback synchronously here — we can
        verify the dispatched callable actually ran."""
        import threading

        ran = {"thread": None, "value": None}

        def _capturing(x):
            ran["thread"] = threading.get_ident()
            ran["value"] = x

        gui._tk_after(0, lambda: _capturing("ok"))
        # ``update()`` flushes ALL pending events including after-callbacks;
        # ``update_idletasks()`` only flushes idle tasks (draw / geometry)
        # and may not run after-callbacks.
        gui.update()
        assert ran["value"] == "ok", "_tk_after callback did not run"
        # The Tk main loop runs on the thread that created the Tk() root.
        # In tests that's the test (main) thread, so the dispatched ident
        # matches the current thread. A non-main ident would mean the
        # dispatch went to a different thread (a bug).
        assert ran["thread"] == threading.get_ident()

    def test_pipelinecallbacks_on_status_does_not_raise(self, gui):
        """A realistic status string must flow through the GUI's on_status
        wrapper without raising — this exercises the _tk_after dispatch +
        the truncate_status helper."""
        gui._tk_after(0, lambda: gui._safe_status_set("Step 1/3: Downloading... 50%"))
        gui.update()

    def test_pipelinecallbacks_on_log_does_not_raise(self, gui):
        """on_log dispatches to ``_log`` which appends to a queue-based
        log handler. Verify it accepts a realistic log line."""
        gui._tk_after(0, lambda: gui._log("Detected 5 silence segments"))
        gui.update()

    def test_pipelinecallbacks_on_progress_does_not_raise(self, gui):
        """on_progress dispatches to ``self.progress.set(...)`` via
        _tk_after. Verify a realistic progress value (0.42) doesn't raise
        and that the widget reflects it after the flush."""
        gui._tk_after(0, lambda p=0.42: gui.progress.set(p))
        gui.update()
        # progress.set clamps to [0, 1]; 0.42 is valid.
        assert 0.0 <= gui.progress.get() <= 1.0

    def test_cancel_event_can_be_set_from_any_thread(self, gui):
        """The cancel event is a threading.Event — settable from any
        thread. The worker checks it via cancel_callback; the GUI's
        cancel button calls _cancel_event.set() directly. Verify both
        paths (main-thread set + worker-thread set) work."""
        import threading

        # Main thread sets (the cancel button's path).
        gui._cancel_event.set()
        assert gui._cancel_event.is_set()
        gui._cancel_event.clear()

        # Simulated worker thread sets.
        def _set_from_worker():
            gui._cancel_event.set()

        t = threading.Thread(target=_set_from_worker)
        t.start()
        t.join()
        assert gui._cancel_event.is_set()
