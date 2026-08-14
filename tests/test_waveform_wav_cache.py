"""Test that the waveform popup reads peaks from the cached
{stem}_audio.wav when available instead of re-decoding the video.

Verifies:
  - When a valid {stem}_audio.wav exists, read_peaks_from_stream is
    called with the WAV path, not the source video path.
  - When the WAV cache is missing or stale, the source video is used.
  - The fallback behaviour matches the old pipeline behaviour exactly.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import stream2video.gui_waveform_render as wfr


def _gui_stub(
    tmp_path: Path,
    wav_exists: bool = False,
    wav_newer: bool = True,
) -> tuple[MagicMock, Path, Path]:
    """Build a minimal GUI stub with just enough attrs for _run() to work.

    Returns a MagicMock whose instance attributes mimic the real GUI
    (entry_input, config, _log, _tk_after, ...). The `_run` closure is
    invoked inline so we can assert the read_peaks_from_stream args.
    """
    in_path = tmp_path / "video.mp4"
    in_path.write_bytes(b"\x00" * 1000)
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    g = MagicMock()
    g.entry_input.get.return_value = str(in_path)
    g.entry_output.get.return_value = str(out_dir)
    g.chk_per_video_dir.get.return_value = False  # keep paths flat
    g.config = {"threshold": -30.0, "min_silence": 2.0, "margin": 0.5, "waveform_timeout": 300}
    g._waveform_render_token = 0
    g._waveform_running = False
    g.running = False  # pipeline not running -> no post-render poller
    g._safe_status_set = MagicMock()
    g._log = MagicMock()
    # Synchronous _tk_after: call the callback immediately.
    g._tk_after = lambda _ms, cb: cb() if callable(cb) else None
    # ``_take_live_snapshot`` returns None -> fallback to final cache;
    # ``load_silence_cache`` returns None -> dry-run detect path.
    g._take_live_snapshot = MagicMock(return_value=None)

    if wav_exists:
        from stream2video.silence.cache import _mark_wav_verified, build_wav_cache_path

        wav = build_wav_cache_path(in_path, out_dir)
        wav.write_bytes(b"\x00" * 100)
        # The render mixin only trusts a WAV that passed the pipeline's
        # broken-PTS sample-verify (the ``.verified`` sidecar).
        _mark_wav_verified(wav)
        if not wav_newer:
            # Make the WAV older than the video so _is_wav_cache_valid fails.
            import os

            os.utime(wav, (0, 0))
            os.utime(in_path, (2**30, 2**30))

    return g, in_path, out_dir


def _make_render_thread_sync(g: MagicMock) -> None:
    """Make threading.Thread synchronous on this stub so the render runs
    inline (test-friendly; a real GUI defers to a background thread)."""
    import threading as _threading_mod

    real_thread_init = _threading_mod.Thread.__init__

    def _sync_thread_init(self, *args, **kwargs):
        # Run the target immediately instead of deferring.
        target = kwargs.get("target")
        if target:
            target()
        # Still call the real __init__ so daemon= etc. don't complain.
        kwargs["target"] = lambda: None  # no-op so the real start() is safe
        real_thread_init(self, *args, **kwargs)

    return _sync_thread_init


class TestWaveformWavCache:
    def test_uses_cached_wav_when_available(self, tmp_path: Path) -> None:
        """When {stem}_audio.wav exists and is fresh, peaks come from it."""
        g, in_path, out_dir = _gui_stub(tmp_path, wav_exists=True, wav_newer=True)
        captured: dict = {}

        def _capture(path, **kw):
            captured["path"] = path
            return ([0.5] * 800, 10.0)

        import threading

        with (
            patch.object(wfr, "read_peaks_from_stream", side_effect=_capture),
            patch.object(wfr, "load_silence_cache", return_value=[]),
            patch.object(wfr, "detect_silence_stream", return_value=[]),
            patch.object(
                threading,
                "Thread",
                side_effect=lambda target=None, daemon=None: _run_synchronously(target),
            ),
        ):
            g._waveform_peaks = []
            render = wfr.WaveformRenderMixin._render_waveform_preview.__get__(g)
            render()

        from stream2video.silence.cache import build_wav_cache_path

        assert captured.get("path") == build_wav_cache_path(in_path, out_dir), (
            f"Expected WAV path {build_wav_cache_path(in_path, out_dir)}, "
            f"got {captured.get('path')}"
        )

    def test_falls_back_to_video_when_wav_missing(self, tmp_path: Path) -> None:
        """No WAV cache -> peaks come from the source video."""
        g, in_path, _out_dir = _gui_stub(tmp_path, wav_exists=False)
        captured: dict = {}

        def _capture(path, **kw):
            captured["path"] = path
            return ([0.5] * 800, 10.0)

        import threading

        with (
            patch.object(wfr, "read_peaks_from_stream", side_effect=_capture),
            patch.object(wfr, "load_silence_cache", return_value=[]),
            patch.object(wfr, "detect_silence_stream", return_value=[]),
            patch.object(
                threading,
                "Thread",
                side_effect=lambda target=None, daemon=None: _run_synchronously(target),
            ),
        ):
            g._waveform_peaks = []
            render = wfr.WaveformRenderMixin._render_waveform_preview.__get__(g)
            render()

        assert captured.get("path") == in_path, (
            f"Expected video path {in_path}, got {captured.get('path')}"
        )

    def test_stale_wav_falls_back_to_video(self, tmp_path: Path) -> None:
        """Stale WAV cache (older than video) is not trusted -> video used."""
        g, in_path, _out_dir = _gui_stub(tmp_path, wav_exists=True, wav_newer=False)
        captured: dict = {}

        def _capture(path, **kw):
            captured["path"] = path
            return ([0.5] * 800, 10.0)

        import threading

        with (
            patch.object(wfr, "read_peaks_from_stream", side_effect=_capture),
            patch.object(wfr, "load_silence_cache", return_value=[]),
            patch.object(wfr, "detect_silence_stream", return_value=[]),
            patch.object(
                threading,
                "Thread",
                side_effect=lambda target=None, daemon=None: _run_synchronously(target),
            ),
        ):
            g._waveform_peaks = []
            render = wfr.WaveformRenderMixin._render_waveform_preview.__get__(g)
            render()

        assert captured.get("path") == in_path, (
            f"Expected video path {in_path} (stale cache), got {captured.get('path')}"
        )


def _run_synchronously(target):
    """Stand-in for threading.Thread that runs the target inline (tests)."""
    if target is not None:
        target()
    # Return a no-op object with the Thread API used by the render path.
    no_op = MagicMock()
    no_op.start = MagicMock()
    return no_op
