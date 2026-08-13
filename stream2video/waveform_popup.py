"""Thread-safe store of in-memory live silence segments for the GUI —
extracted from ``gui.py`` (Этап 10 incremental refactor).

The pipeline worker's ``on_live_segment`` callback receives segments as
``detect_silence`` discovers them and publishes them to whoever is
listening; the waveform popup's poller reads them back every second so
the overlay tracks the running detection.

Historically the store was keyed by *resolved video path* — but the
popup is opened against whatever path the user typed into the input
entry, while the pipeline worker publishes under the *resolved
download path* (``<id>-<epoch>.mp4`` after ``apply_per_video_dir``).
The two keys never matched for URL-runs, so the live overlay stayed
frozen. The store is therefore keyed by **run id** instead: a
monotonically-increasing counter that the pipeline worker bumps at the
start of every run. The popup subscribes to "the current run" via
:meth:`LiveSegmentsStore.take_snapshot` without naming any path —
URL and local-file runs behave identically, and a second Start can't
collide with a stale first-run snapshot (the new run_id invalidates
the old entry).
"""

from __future__ import annotations

import threading

from stream2video.silence import SilenceSegment


class LiveSegmentsStore:
    """Single-slot, run-keyed, thread-safe silence-segment store.

    Instead of mapping ``Path → segments``, the store holds ONE entry
    per pipeline run, tagged with a monotonically increasing
    ``run_id``. The worker calls :meth:`begin_run` at the start of
    every pipeline; any previous run's segments are discarded on that
    call. The waveform popup polls :meth:`take_snapshot` — no path
    argument — so a popup opened on a *typed* input path still picks up
    segments published under the *resolved* download path of a URL-run
    (the historical path-keyed dict could never match those two).

    The lock is held only around the read / write — never around the
    actual pipeline callbacks. The lock duration is tiny (one
    attribute swap + a list copy on the read side); the copy the
    consumer gets back is stable because the producer only ever calls
    :meth:`set` (which replaces the list), never mutates it in place.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # ``_current`` is None when no run has published anything yet.
        self._current: tuple[int, list[SilenceSegment]] | None = None
        self._next_run_id = 0

    def begin_run(self) -> int:
        """Allocate the next run id and clear any previous run's data.

        Called by the pipeline worker exactly once per ``Start``
        button press (before the controller starts publishing
        segments). Returns the new ``run_id`` so the worker can pass
        it to subsequent :meth:`set` calls — a stale worker finishing
        after a newer run has begun is then unable to clobber the
        newer run's data with out-of-date segments.
        """
        with self._lock:
            self._next_run_id += 1
            self._current = None
            return self._next_run_id

    def set(self, run_id: int, segments: list[SilenceSegment]) -> bool:
        """Publish ``segments`` as the current state of run ``run_id``.

        Returns ``True`` when the store accepted the publish —
        i.e. ``run_id`` is still the latest run. A stale ``run_id``
        (a previous pipeline finishing after the user pressed Start
        again) is dropped silently; the popup polls *the* current run
        without naming an id, so showing the old run's data would just
        be confusing.
        """
        with self._lock:
            if run_id != self._next_run_id:
                return False
            # Defensive copy: the controller may keep mutating its own
            # list; ours must be stable until the next set / clear.
            self._current = (run_id, list(segments))
            return True

    def take_snapshot(self) -> list[SilenceSegment] | None:
        """Return the current run's segments, or None if none published.

        ``None`` is distinct from ``[]``: ``[]`` means "a run is in
        flight and has detected nothing yet", whereas ``None`` means
        "no run has published anything" — the popup uses the
        difference to decide between "0 silences so far" and "run the
        pipeline to detect".
        """
        with self._lock:
            if self._current is None:
                return None
            return list(self._current[1])

    def clear(self, run_id: int | None = None) -> None:
        """Drop the current run's data without starting a new run.

        Called by the pipeline worker's ``finally``: the run that just
        finished (success / cancel / error) shouldn't leave its
        half-populated segments to resurface the next time the user
        opens the waveform popup on an unrelated run. The next
        :meth:`begin_run` would clear it anyway, but doing it here
        keeps the store small between runs.

        ``run_id`` gates the clear (B10 audit): a STALE worker (its run
        already superseded by a newer ``Start``) must not wipe the newer
        run's freshly published segments. With ``run_id=None`` the clear
        is unconditional (historical behaviour, used by tests / popup
        close paths that don't track a run).
        """
        with self._lock:
            if run_id is not None and run_id != self._next_run_id:
                return
            self._current = None

    def current_run_id(self) -> int | None:
        """Return the most recently allocated run id, or None before the
        first :meth:`begin_run`. Used by the worker to tell whether it
        is still the current run before mutating shared UI state
        (B10 audit)."""
        with self._lock:
            return self._next_run_id if self._next_run_id > 0 else None
