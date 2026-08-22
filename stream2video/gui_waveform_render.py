"""Waveform render pipeline: peaks download + overlay application +
live-segments poller."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import customtkinter as ctk

from stream2video.download import redact_input_url
from stream2video.formatters import fmt_clock_time, fmt_time, fmt_zoom_text
from stream2video.paths import artifact_stem
from stream2video.silence import (
    SilenceDetectionError,
    SilenceSegment,
    apply_margin,
    detect_silence_stream,
    load_silence_cache,
)
from stream2video.utils import cancel_process
from stream2video.waveform import (
    read_peaks_from_stream,
    render_waveform_image,
    slice_peaks_by_time,
)

_logger = logging.getLogger("stream2video.gui")


class WaveformRenderMixin:
    """Waveform render pipeline + live-segments poller.

    Expects the window/interactions mixins to provide the rest of the
    state (``_waveform_peaks``, ``_waveform_view_*``, ``_apply_view``
    delegates used by the interactions, etc.).
    """

    # Attributes owned by ``WaveformWindowMixin._init_waveform_state``
    # that this mixin writes to from outside that initializer. Declaring
    # them here with matching types keeps mypy's per-mixin inference
    # consistent across the composed ``WaveformMixin`` (without these,
    # mypy infers ``int`` from ``img.size[0]`` in ``_apply_view`` and
    # flags the resulting attribute as conflicting with the
    # ``int | None`` declaration in ``WaveformWindowMixin``).
    if TYPE_CHECKING:
        _waveform_last_render_w: int | None
        _waveform_last_render_h: int | None
        _waveform_video_path: Path | None
        _waveform_output_dir: Path | None
        _waveform_last_segments: list[SilenceSegment]
        _waveform_render_token: int
        _waveform_run_token: int
        _waveform_poll_token: int
        _waveform_running: bool

    def _render_waveform_preview(self) -> None:
        """Stream audio + silence from the source video via ffmpeg pipes
        and render the waveform with overlay. No file is written.

        Runs on a background thread so the GUI stays responsive during
        the (potentially long) first decode. Re-runs are debounced by
        ``_waveform_run_token`` — if the user clicks "Render" again
        before the previous run finishes, the older one is invalidated.
        This token is DISTINCT from ``_waveform_render_token``, which
        ``_apply_view`` bumps on every view re-render (drag/zoom/pan):
        view re-renders must never cancel or invalidate an in-flight
        preview run — they only coalesce PIL work among themselves.

        Phase 1 streams the audio peaks directly from ffmpeg, stores
        them in self._waveform_peaks/duration, and shows the bare
        waveform for the current view (initially the full timeline).
        Phase 2 reads silence segments from the in-memory live store
        (the pipeline worker's ``on_segment`` callback keeps it up to
        date while detect is running) or, if no live state is
        available, from the final silence cache on disk. When the
        pipeline is still running, a 1-second poller keeps the overlay
        in sync with new segments as they are detected; it stops when
        ``self.running`` flips to False.

        The current view (view_start/view_end) lives in self and is
        re-rendered by the shared ``_apply_view`` helper that all
        paths (initial, poller, zoom/pan buttons, slider) call.
        """
        if self._waveform_running:
            self._log("Waveform render already running")
            return

        # Cancel any previous preview process so two renders don't
        # compete for audio decode bandwidth. ``cancel_process`` blocks
        # up to ``timeout`` seconds waiting for the killed process to
        # reap; on a wedged ffmpeg that would freeze the Tk main loop
        # here, so defer the actual wait to the worker thread started
        # below.
        def _cancel_previous() -> None:
            try:
                cancel_process("preview", timeout=2.0)
            except Exception:
                _logger.debug("cancel preview failed", exc_info=True)

        # Need an input file (must be a local file — previewing a fresh
        # download would be a separate flow). Local file → reuse it.
        input_raw = self.entry_input.get().strip()
        if not input_raw:
            self._log("Set an input video (local file) first")
            return
        in_path = Path(input_raw)
        # Normalise BEFORE the is_file() check — and to the same form the
        # pipeline_worker uses as the live-segments store key (expanded,
        # resolved, symlinks forward). Resolving first means a ``~/``
        # path is accepted exactly like the pipeline accepts it (the old
        # order rejected every tilde path with "Input not a local file"),
        # and a raw ``./video.mp4`` store key would never match the
        # pipeline's *resolved* one, leaving the overlay permanently
        # empty and the final "Waveform updated" log silent. Matches the
        # pre-existing normalization in gui_waveform_window.py:81-84.
        try:
            in_path = in_path.expanduser().resolve()
        except OSError as e:
            self._log(f"Could not resolve input path ({e}); preview disabled")
            return
        if not in_path.is_file():
            self._log(
                f"Input not a local file (downloads not previewable): {redact_input_url(input_raw)}"
            )
            return

        # Read current slider values (sync first in case FocusOut didn't fire).
        self._sync_slider_entries()
        config = {
            "threshold": float(self.settings["threshold"]),
            "min_silence": float(self.settings["min_silence"]),
            "margin": float(self.settings["margin"]),
        }

        # Resolve the same output dir the pipeline uses — the final
        # silence cache lives there as a fallback when the in-memory
        # live store is empty (popup opened after pipeline finished).
        out_raw = self.entry_output.get().strip() or "./processed_videos"
        out_dir = Path(out_raw).expanduser().resolve()
        if bool(self.chk_per_video_dir.get()):
            # Same keyed project-dir naming the pipeline uses (stem +
            # source-path hash), so the popup finds the pipeline's WAV /
            # silence cache even for same-named sources in other folders.
            out_dir = out_dir / artifact_stem(in_path)

        token = self._waveform_run_token + 1
        self._waveform_run_token = token
        # Start a new poller session: any previously-running live poller
        # (started by an earlier Render click) must retire — it would
        # otherwise keep re-rendering the overlay on top of this fresh
        # render cycle.
        self._waveform_poll_token += 1
        self._waveform_running = True
        self._safe_status_set("Loading...")
        self._log("Waveform preview: loading audio from source video...")

        def _run() -> None:
            _cancel_previous()
            try:
                # Phase 1: read peaks. When the pipeline has already run,
                # the cached {stem}_audio.wav (16 kHz mono PCM) holds the
                # same waveform data but decodes ~10x faster than the
                # full video (~0.5s vs ~25s for a 6h stream). Fall back
                # to the original video decode when the cache is missing
                # or stale (mtime older than the source).
                from stream2video.silence.cache import (
                    _is_wav_cache_valid,
                    build_wav_cache_path,
                )

                self._tk_after(0, lambda: self._safe_status_set("Loading..."))
                wav_cache = build_wav_cache_path(in_path, out_dir)
                if _is_wav_cache_valid(wav_cache, in_path):
                    self._log(f"  Waveform preview: using cached audio ({wav_cache.name})")
                    peaks, duration = read_peaks_from_stream(
                        wav_cache,
                        target_buckets=800,
                        timeout=self.settings.get("waveform_timeout", 300),
                    )
                else:
                    peaks, duration = read_peaks_from_stream(
                        in_path,
                        target_buckets=800,
                        timeout=self.settings.get("waveform_timeout", 300),
                    )
                if token != self._waveform_run_token:
                    return
                if not peaks or duration <= 0:
                    self._tk_after(
                        0,
                        lambda: self._safe_status_set("No audio stream found"),
                    )
                    self._log("  Waveform preview: no audio in source")
                    return

                # Commit the audio to state.
                self._waveform_peaks = peaks
                self._waveform_duration = duration
                self._waveform_video_name = in_path.name
                self._waveform_video_path = in_path
                self._waveform_view_start = 0.0
                self._waveform_view_end = duration
                self._waveform_cursor_frac = 0.5
                self._waveform_cursor_known = False

                # Phase 1.5: render the bare waveform (no overlay yet)
                self._tk_after(
                    0,
                    lambda: self._safe_status_set("Rendering peaks... (detecting silence)"),
                )
                self._tk_after(0, lambda: self._apply_view([]))
                if token != self._waveform_run_token:
                    return

                # Phase 2: pull silence segments.
                margin = float(config["margin"])
                self._waveform_margin = margin
                self._waveform_output_dir = out_dir
                live_segs = self._take_live_snapshot()
                cached_segs = load_silence_cache(in_path, out_dir, config)
                raw_segments = live_segs if live_segs is not None else cached_segs
                if raw_segments is None:
                    from stream2video.silence.cache import build_silence_cache_path

                    cache_path = build_silence_cache_path(in_path, out_dir)
                    # Dry-run detection.
                    self._tk_after(
                        0,
                        lambda: self._safe_status_set(
                            "No silence cache — running dry-run detect..."
                        ),
                    )
                    self._log(
                        f"  Waveform preview: no segments in live store and no cache at "
                        f"{cache_path} for threshold={config['threshold']}dB, "
                        f"min_silence={config['min_silence']}s, "
                        f"margin={config['margin']}s — running dry-run detect"
                    )
                    try:
                        raw_dry = detect_silence_stream(
                            in_path,
                            threshold=float(config["threshold"]),
                            min_silence=float(config["min_silence"]),
                            # The dry-run detect used the
                            # module-level 10h silence_timeout fallback —
                            # a preview decode could sit on a hung ffmpeg
                            # for hours. The user-configured waveform
                            # timeout bounds the preview exactly like the
                            # peak-read paths above.
                            timeout=self.settings.get("waveform_timeout", 300),
                            # Stale-render cancels (new render started /
                            # window closed) kill the ffmpeg child via
                            # cancel_process("preview"); without this the
                            # kill surfaces as rc=-9 and the user sees
                            # "ffmpeg silencedetect OOM" instead of a
                            # clean cancel.
                            cancel_callback=(lambda: token != self._waveform_run_token),
                        )
                    except SilenceDetectionError as e:
                        _logger.warning(f"Dry-run detect failed: {e}")
                        raw_dry = []
                    raw_segments = apply_margin(raw_dry, margin)
                    self._log(
                        f"  Dry-run detected {len(raw_segments)} silence segments "
                        f"(not cached — run the pipeline to commit)"
                    )
                # Apply margin so the overlay matches cut_and_concat.
                if live_segs is not None:
                    segments = apply_margin(raw_segments, margin)
                else:
                    segments = raw_segments
                if live_segs is not None:
                    self._log(
                        f"  Loaded {len(live_segs)} silences from live store "
                        f"(threshold={config['threshold']}dB, "
                        f"min_silence={config['min_silence']}s, margin={config['margin']}s)"
                    )
                else:
                    self._log(
                        f"  Loaded {len(cached_segs or [])} silences from final cache "
                        f"(threshold={config['threshold']}dB, "
                        f"min_silence={config['min_silence']}s, margin={config['margin']}s)"
                    )
                if token != self._waveform_run_token:
                    return

                # Phase 3: render the overlay for the current view.
                self._tk_after(0, lambda: self._safe_status_set("Rendering overlay..."))
                self._tk_after(0, lambda: self._apply_view(segments))
                if token != self._waveform_run_token:
                    return
                self._log(
                    f"  Waveform ready: {len(segments)} silence segments, "
                    f"{fmt_time(duration)} duration"
                )

                # Phase 4: if the pipeline is still running, start a
                # poller that re-renders the overlay as new segments
                # arrive in the in-memory store.
                if self.running:
                    poll_state = {
                        "last_count": len(segments),
                        "last_view": (self._waveform_view_start, self._waveform_view_end),
                        # Flipped the first time the pipeline is observed
                        # not-running: gates the "waveform locked" log so
                        # a cache count == live count coincidence doesn't
                        # swallow the finished notice.
                        "stopped_logged": False,
                    }
                    self._tk_after(
                        1000,
                        lambda: self._poll_live_segments(
                            in_path, margin, self._waveform_poll_token, poll_state
                        ),
                    )
            except Exception as e:
                _logger.exception("Waveform render failed")
                self._tk_after(0, lambda err=e: self._safe_status_set(f"Error: {err}"))
                self._log(f"[ERROR] Waveform render failed: {e}")
            finally:
                self._waveform_running = False

        threading.Thread(target=_run, daemon=True).start()

    def _take_live_snapshot(self) -> list[SilenceSegment] | None:
        """Poll the run-keyed store.

        The popup polls "the current run" without naming a path: the
        pipeline worker publishes under a ``run_id`` allocated at
        Start, so a URL-run's resolved download path no longer needs
        to match the input-entry path the popup was opened against.
        """
        return self._live_segments_store.take_snapshot()

    def _apply_view(self, segments: list[SilenceSegment] | None = None) -> None:
        """Render the waveform for the current view (view_start → view_end)
        and apply it to the image label. No-op if the popup is closed or
        the audio hasn't been loaded yet.
        """
        if self.lbl_wave_image is None or self.lbl_wave_status is None:
            return
        if not self._waveform_peaks or self._waveform_duration <= 0:
            return

        # Every drag/zoom/pan calls _apply_view; without a
        # token bump here each call captured the SAME token and every
        # render result was accepted, queuing dozens of PIL renders on
        # worker threads per mouse gesture. Invalidate prior renders
        # immediately — only the latest view-state's image lands on the
        # widget.
        self._waveform_render_token += 1
        token = self._waveform_render_token

        view_start = self._waveform_view_start
        view_end = self._waveform_view_end
        view_duration = view_end - view_start
        if view_duration <= 0 or view_duration > self._waveform_duration + 1e-6:
            view_start = 0.0
            view_end = self._waveform_duration
            view_duration = view_end - view_start
            self._waveform_view_start = view_start
            self._waveform_view_end = view_end

        view_peaks = slice_peaks_by_time(
            self._waveform_peaks, self._waveform_duration, view_start, view_end
        )

        if segments is None and self._waveform_video_path is not None:
            raw = self._take_live_snapshot()
            if raw is not None:
                segments = apply_margin(raw, self._waveform_margin)
            elif self._waveform_last_segments:
                segments = list(self._waveform_last_segments)
        if segments is None:
            segments = []
        self._waveform_last_segments = segments
        view_segments = [s for s in segments if s.end > view_start and s.start < view_end]

        render_w, render_h = self._compute_waveform_render_size()

        zoom_level = self._waveform_duration / view_duration
        zoom_text = fmt_zoom_text(zoom_level)
        title = (
            f"{self._waveform_video_name}  |  {len(view_segments)} silences"
            f"  |  {fmt_clock_time(view_start)}"
            f"-{fmt_clock_time(view_end)}  |  {zoom_text}"
        )

        # The PIL render is CPU-bound (800x200 canvas, hundreds of
        # silence rectangles + bars). Doing it synchronously on the Tk
        # main thread freezes the UI for every 1s live-poll tick while a
        # pipeline runs, so build the image on a worker thread and only
        # marshal the final widget ``configure`` back to the main loop.
        threshold_db = float(self.settings["threshold"])

        def _render_then_apply() -> None:
            try:
                img = render_waveform_image(
                    view_peaks,
                    width=render_w,
                    height=render_h,
                    total_duration=view_duration,
                    silence_segments=view_segments,
                    title=title,
                    view_start=view_start,
                    threshold_db=threshold_db,
                )
            except Exception as e:
                _logger.exception("Waveform render failed")
                self._tk_after(0, lambda err=e: self._log(f"[ERROR] Waveform render failed: {err}"))
                return

            def _apply() -> None:
                if token != self._waveform_run_token:
                    return
                if self.lbl_wave_image is None or not self._wave_window_alive():
                    return
                ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=img.size)
                self._waveform_ctk_image = ctk_img
                self.lbl_wave_image.configure(image=ctk_img, text="")
                self._waveform_image_width = img.size[0]
                self._waveform_last_render_w = int(img.size[0])
                self._waveform_last_render_h = int(img.size[1])

                self.lbl_wave_status.configure(text=title)
                self._update_waveform_controls()
                self._update_intervals_list(view_segments, segments, view_start, view_end)

            self._tk_after(0, _apply)

        threading.Thread(target=_render_then_apply, daemon=True).start()

    def _update_intervals_list(
        self,
        view_segments: list[SilenceSegment],
        all_segments: list[SilenceSegment],
        view_start: float,
        view_end: float,
    ) -> None:
        """Update the cut/keep intervals textbox.

        Shows a compact list of silence (cut) segments in the current
        view, with keep intervals derived between them. Format::

            CUT  0.05 - 0.25s  (0.20s)
            KEEP 0.25 - 0.35s  (0.10s)
            CUT  0.35 - 0.55s  (0.20s)
            ...

        Only the visible segments (those overlapping the current view)
        are listed — keeps the list readable when zoomed in.
        """
        widget = getattr(self, "_waveform_intervals_text", None)
        if widget is None:
            return
        if not view_segments:
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            widget.insert("1.0", "(no silence segments in view)")
            widget.configure(state="disabled")
            return

        lines: list[str] = []
        prev_end = view_start
        for seg in view_segments:
            # Keep interval before this silence (if non-trivial).
            if seg.start > prev_end + 0.01:
                keep_dur = seg.start - prev_end
                lines.append(f"  KEEP {prev_end:7.2f} - {seg.start:7.2f}s  ({keep_dur:.2f}s)")
            cut_dur = seg.end - seg.start
            lines.append(f"  CUT  {seg.start:7.2f} - {seg.end:7.2f}s  ({cut_dur:.2f}s)")
            prev_end = seg.end
        # Trailing keep after the last silence.
        if prev_end < view_end - 0.01:
            keep_dur = view_end - prev_end
            lines.append(f"  KEEP {prev_end:7.2f} - {view_end:7.2f}s  ({keep_dur:.2f}s)")

        # Header: total counts (view + all).
        header = (
            f"  {len(view_segments)} silences in view"
            f"  |  {len(all_segments)} total"
            f"  |  view {view_start:.1f}s-{view_end:.1f}s\n"
        )
        text = header + "\n".join(lines)

        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _poll_live_segments(
        self,
        in_path: Path,
        margin: float,
        token: int,
        state: dict,
    ) -> None:
        """Re-read the in-memory live store every second and re-render
        the overlay if the segment count or visible window changed."""
        # Compare against the POLL session token, not the
        # render token. ``_apply_view`` bumps the render token on every
        # render to retire in-flight PIL renders; if this
        # poller checked that one it would die right after its first
        # overlay update. The poll token only moves when a new render
        # cycle starts (Render click), which is what should retire a
        # stale poller.
        if token != self._waveform_poll_token:
            return
        if not self._wave_window_alive():
            return

        current_view = (self._waveform_view_start, self._waveform_view_end)
        if not self.running:
            # Keep the poller alive: the user can press Start again
            # (self.running flips back to True) while the popup is open,
            # and a dead poller would freeze the overlay on whatever the
            # previous run concluded. The worker's own polls see the new
            # ``running`` state on the next tick.
            raw = self._take_live_snapshot()
            if raw is not None:
                segments = apply_margin(raw, margin)
                # Snapshot changed? Reflect it (a new run's detect is
                # already writing to the same path).
                if len(segments) != state.get("last_count") or current_view != state.get(
                    "last_view"
                ):
                    self._apply_view(segments)
                    state["last_count"] = len(segments)
                    state["last_view"] = current_view
                if not state.get("stopped_logged"):
                    state["stopped_logged"] = True
                    self._log(f"  Pipeline finished — waveform locked at {len(segments)} silences")
            else:
                out_dir = self._waveform_output_dir
                if out_dir is None:
                    # No live state and no output dir -> terminal.
                    if not state.get("stopped_logged"):
                        state["stopped_logged"] = True
                        self._safe_status_set("Cancelled / no segments detected")
                else:
                    config = {
                        "threshold": float(self.settings["threshold"]),
                        "min_silence": float(self.settings["min_silence"]),
                        "margin": margin,
                    }
                    cached = load_silence_cache(in_path, out_dir, config)
                    if cached is not None:
                        if len(cached) != state.get("last_count") or current_view != state.get(
                            "last_view"
                        ):
                            self._apply_view(list(cached))
                            state["last_count"] = len(cached)
                            state["last_view"] = current_view
                        if not state.get("stopped_logged"):
                            state["stopped_logged"] = True
                            self._log(
                                f"  Pipeline finished — waveform locked at {len(cached)} silences"
                            )
                    elif not state.get("stopped_logged"):
                        state["stopped_logged"] = True
                        self._safe_status_set("Cancelled / no segments detected")
            # Reschedule: the popup stays "live" so a new Start flips
            # back into the active branch above.
            try:
                self.after(
                    1000,
                    lambda: self._poll_live_segments(in_path, margin, token, state),
                )
            except Exception:
                pass
            return

        # running: reset the "stopped" latch so a *subsequent* stop
        # gets its own log/summary line.
        state["stopped_logged"] = False

        raw = self._take_live_snapshot()
        if raw is not None:
            segments = apply_margin(raw, margin)
            count_changed = len(segments) != state["last_count"]
            view_changed = current_view != state["last_view"]
            if count_changed or view_changed:
                self._apply_view(segments)
                state["last_count"] = len(segments)
                state["last_view"] = current_view
                if count_changed:
                    self._log(f"  Waveform updated: {len(segments)} silences so far")

        # Re-schedule the next poll. ``after()`` raises TclError if the
        # window is already destroyed (e.g. user closed the main window
        # while the pipeline is running) - catch it so the exception
        # doesn't propagate to the Tk event loop as an unhandled error.
        try:
            self.after(
                1000,
                lambda: self._poll_live_segments(in_path, margin, token, state),
            )
        except Exception:
            pass
