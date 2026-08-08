"""EncoderPanelMixin — encoder combobox + Test encoder button (Этап 10 mixin).

Extracted from ``Stream2VideoGUI``: ``_test_encoders`` (the button
handler that runs a real ffmpeg smoke test on the picked encoder) and
``_on_encoder_change`` (combobox selection → updates the description
label).
"""

from __future__ import annotations

from stream2video.encoder_test import EncoderTester, get_encoder_description


class EncoderPanelMixin:
    """The encoder combobox + Test encoder button surface."""

    def _init_encoder_panel(self) -> None:
        # ``EncoderTester`` worker (see ``stream2video.encoder_test``). Held
        # on ``__init__`` so repeated "Test encoder" clicks share state —
        # the worker's ``test()`` is single-flight; a second request while
        # the first is running is logged and dropped. Built lazily because
        # ``EncoderTester`` needs the adapter, and the adapter needs the
        # GUI's ``btn_test_encoders`` (built in ``_build_ui``, which runs
        # after this ``__init__``).
        self._encoder_tester: EncoderTester | None = None

    def _on_encoder_change(self, choice: str) -> None:
        self.config["encoder"] = choice
        self.lbl_encoder_desc.configure(text=get_encoder_description(choice))

    def _test_encoders(self) -> None:
        # The actual smoke-test threading is handled by
        # :class:`stream2video.encoder_test.EncoderTester` so the worker
        # thread and its error mapping can be unit-tested without
        # driving the Tk main loop. This method is the tiny adapter:
        # lazily build the tester (if first run) bound to a small
        # callbacks adapter (_EncoderTesterAdapter) that funnels log
        # lines and button-state changes back to this GUI through the
        # main-thread dispatcher, then forward the current encoder.
        enc = self.combo_encoder.get()
        if self._encoder_tester is None:
            # Lazy import so module load order doesn't create a cycle
            # (gui.py imports this mixin; this mixin imports gui.py
            # only at call-time, which is fine).
            from stream2video.gui import _EncoderTesterAdapter

            self._encoder_tester = EncoderTester(_EncoderTesterAdapter(self))
        self._encoder_tester.test(enc)
