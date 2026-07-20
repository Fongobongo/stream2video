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
            "progress",
            "lbl_status",
            "lbl_overall",
            "lbl_total",
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
