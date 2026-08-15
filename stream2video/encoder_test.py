"""Encoder test orchestration extracted from ``gui.py`` (incremental
refactor).

The GUI's ``Test encoder`` button spawns a worker thread that runs
``check_encoder`` (a real ffmpeg smoke test — see
``stream2video.concat.check_encoder``) and logs the result. The work has
no Tk dependencies other than dispatching log lines and a final
"restore button state" call back to the Tk main loop. Extracting it to
its own module:

  * makes it unit-testable without driving the Tk main loop — the test
    monkeypatches ``check_encoder`` and asserts the worker logs the
    expected lines / restores the button state through a fake
    callbacks object.
  * shrinks ``gui.py`` by another ~25 lines and pulls the
    ``ENCODER_DESCRIPTIONS`` constant (the user-facing encoder blurbs
    the ``Encoder: ...`` info label cycles through) next to the code
    that references it.

The GUI keeps thin wrappers so the button binding and the encoder-
change handler stay on the class. Pure meta data (descriptions)
lives here; the worker thread's behaviour (run, capture, log,
restore) is wrapped in the ``EncoderTestCallbacks`` protocol.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any, Protocol

logger = logging.getLogger("stream2video.encoder_test")


# User-facing description for each supported encoder, shown in the GUI's
# ``Encoder: ...`` info label when the user picks the encoder in the
# combobox. Kept as a dict so the encoder-test / encoder-change handlers
# can look the description up by the same key the combobox emits.
ENCODER_DESCRIPTIONS: dict[str, str] = {
    "h264_nvenc": "NVIDIA NVENC (GTX 1000+, RTX)",
    "h264_amf": "AMD AMF (RX 400+, Ryzen APU)",
    "h264_mf": "Media Foundation (any GPU, Windows only)",
    "libx264": "CPU software encode (most compatible)",
}


def get_encoder_description(encoder: str) -> str:
    """Return the user-facing description for ``encoder`` (idempotent
    fall-back to empty string if the encoder isn't in
    ``ENCODER_DESCRIPTIONS``). Pure, side-effect-free.
    """
    return ENCODER_DESCRIPTIONS.get(encoder, "")


class EncoderTestCallbacks(Protocol):
    """Tiny interface the GUI implements for ``EncoderTester``.

    Centralised as a Protocol (structural typing) so the test suite can
    build a fake with the four methods and pass it without inheriting
    from any base class. The same interface keeps the worker's Tk
    surface small and explicit — the GUI is the only implementer in
    production but the contract is now visible and tested.
    """

    def log(self, message: str) -> None: ...

    def schedule_on_main(self, ms: int, func: Callable[..., Any]) -> None: ...

    def set_test_button_state(self, *, running: bool) -> None: ...


class EncoderTester:
    """Orchestrates the background ``check_encoder`` smoke test.

    Single-flight: a second request while the first is still running is
    logged and ignored (matches the legacy GUI behaviour). The class
    holds its own ``_running`` flag so the GUI doesn't need to rent a
    boolean slot for it — but ``set_test_button_state`` is called on
    every transition so the GUI can disable / re-enable the button.
    """

    def __init__(self, callbacks: EncoderTestCallbacks):
        self._cb = callbacks
        # A ``threading.Event`` would also work, but a plain bool is
        # enough — only one test can run at a time and the GUI's
        # caller (the button callback) doesn't race itself.
        self._running = False
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._running

    def test(self, encoder: str) -> None:
        """Run the encoder smoke test on a background thread.

        Returns immediately; results surface through the callbacks
        object (log lines + button state restoration). If a test is
        already running, the request is logged and dropped.
        """
        with self._lock:
            if self._running:
                self._cb.log("Test already running")
                return
            self._running = True
        # Delay-import so the test suite can monkeypatch
        # ``check_encoder`` on ``stream2video.concat`` and have the
        # change visible to the worker thread. If either callback raises
        # (e.g. TclError from a destroyed window during app close — only
        # AttributeError is guarded in the adapter), reset the flag so
        # every later click doesn't hit "Test already running" forever
        # with no worker ever started.
        try:
            self._cb.log(f"Testing encoder: {encoder} ...")
            # We deliberately set the button state while holding the
            # ``_running`` flag transition so the GUI's re-enable on test
            # completion matches the actual lifecycle.
            self._cb.set_test_button_state(running=True)
        except Exception:
            with self._lock:
                self._running = False
            raise

        def _run() -> None:
            from stream2video.concat import check_encoder

            try:
                try:
                    ok = check_encoder(encoder)
                    self._cb.schedule_on_main(
                        0, lambda: self._cb.log(f"  {encoder}: {'[OK]' if ok else 'NO'}")
                    )
                except FileNotFoundError:
                    self._cb.schedule_on_main(
                        0, lambda: self._cb.log(f"  {encoder}: ffmpeg not found in PATH")
                    )
                except Exception as e:
                    self._cb.schedule_on_main(
                        0, lambda e=e: self._cb.log(f"  {encoder}: ERROR ({e})")
                    )
                    logger.exception("Encoder test crashed")
            finally:
                with self._lock:
                    self._running = False
                # Defer the button restoration to the Tk main loop so
                # all widget writes happen from the same thread.
                # If the GUI is being torn down right now (scheduler
                # raises), don't crash the daemon thread on top of that —
                # the button state is moot when its window is gone.
                try:
                    self._cb.schedule_on_main(
                        0, lambda: self._cb.set_test_button_state(running=False)
                    )
                except Exception:
                    logger.debug("schedule_on_main raised during shutdown", exc_info=True)

        threading.Thread(target=_run, daemon=True).start()
