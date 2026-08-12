"""Thread-safe store of in-memory live silence segments for the GUI —
extracted from ``gui.py`` (Этап 10 incremental refactor).

The pipeline worker's ``on_live_segment`` callback receives segments as
``detect_silence`` discovers them and stores them under the resolved
video path. The waveform popup's poller reads them back every second
so the overlay tracks the running detection. They communicate through a
plain ``dict[Path, list[SilenceSegment]]`` guarded by a single
``threading.Lock`` — the lock matters because *the producer (the
pipeline's stderr drain thread) and the consumer (the popup's poller
thread) are different.* Without it the consumer could iterate the list
while the producer swaps it.

Extracted here as :class:`LiveSegmentsStore` so:

  * The two consumers (pipeline worker + waveform popup) share a tiny,
    unit-testable object instead of carrying the GUI's ``self.*`` state
    around.
  * The semantics (shallow-copy on read, pop-or-no-op semantics, lock
    held inside the read / pop, owner-by-path keying) are pinned by
    tests without needing the GUI's Tk event loop.
  * The GUI shrinks — ``__init__``'s ``self._live_segments = {}`` /
    ``self._live_segments_lock = threading.Lock()`` pair becomes one
    owned instance, and the 4 inline lock-and-dict patterns
    (``_take_live_snapshot``, the worker's ``_on_live_segment`` /
    ``pop``, the waveform popup's ``apply_view`` fallback) all delegate
    here.

The class is intentionally small: a Python ``dict`` guarded by a
``Lock`` covers the use cases; a fancier concurrent data structure
(queue.Queue, multiprocessing.Manager) would either over-serialize or
add IPC overhead without buying anything for a 2-thread producer-
consumer relationship under the GIL.
"""

from __future__ import annotations

import threading
from pathlib import Path

from stream2video.silence import SilenceSegment

# Cap on live-segment entries kept between runs. A URL-pipeline run
# publishes under the *resolved download path* — a new key every time —
# so an unbounded dict grows by one entry (~a few KB of SilenceSegment
# lists) per URL processed with a popup open. 32 generations is far
# beyond any realistic session (popup key-miss only matters when the
# popup stays open across runs); evicting the oldest entry keeps the
# store at a few hundred KB worst-case.
_MAX_LIVE_KEYS = 32


class LiveSegmentsStore:
    """Thread-safe ``Path → list[SilenceSegment]`` store.

    The lock is held only around the collect / put / pop — never around
    the actual manager callbacks the pipeline invokes. The lock duration
    is short (one dict lookup + a shallow copy on read, a list copy on
    put); the only memory cost is the snapshot copy the consumer gets
    back, which is fine because the consumer (the waveform renderer)
    needs a stable list anyway — iterating while the producer appends
    would otherwise race.

    Insertion order (``dict`` preserves it) is used as a poor-man's LRU:
    the oldest keys are evicted past ``_MAX_LIVE_KEYS``.
    """

    def __init__(self, max_keys: int = _MAX_LIVE_KEYS) -> None:
        self._segments: dict[Path, list[SilenceSegment]] = {}
        self._lock = threading.Lock()
        self._max_keys = max_keys

    def set(self, video_path: Path, segments: list[SilenceSegment]) -> None:
        """Replace the segment list for ``video_path``.

        Called from the pipeline worker's stderr drain thread when a
        new ``silence_*`` line arrives; the caller is responsible for
        passing the *full* latest list (not an incremental append) —
        the controller publishes the cumulative set as detect runs.
        """
        with self._lock:
            # Defensive copy: the controller might keep mutating its
            # own list; ours must be stable until the next set / pop.
            self._segments[video_path] = list(segments)
            # Unbounded growth guard: drop the oldest keys past the cap.
            while len(self._segments) > self._max_keys:
                oldest = next(iter(self._segments))
                del self._segments[oldest]

    def take_snapshot(self, video_path: Path) -> list[SilenceSegment] | None:
        """Return a shallow copy of the current segments for
        ``video_path``, or ``None`` if the producer has never published
        state for it.

        ``None`` is distinct from ``[]``: ``[]`` means "the producer has
        started detection and detected nothing yet", whereas ``None``
        means "the producer hasn't started" — the waveform popup uses
        the difference to decide whether to show "No silence cache —
        run detect" or "0 silences loaded".

        The copy happens inside the lock — a future in-place mutation
        (``.extend(...)``) on the producer side would otherwise race
        the iteration on the consumer side.
        """
        with self._lock:
            segs = self._segments.get(video_path)
            return list(segs) if segs is not None else None

    def pop(self, video_path: Path) -> list[SilenceSegment] | None:
        """Like :meth:`take_snapshot` but also removes the entry.

        Called by the pipeline worker after a successful run so a
        re-open of the waveform popup for the same path doesn't show
        the previous run's stale mid-detect state. Returns ``None`` if
        no state has ever been published for the path.
        """
        with self._lock:
            return self._segments.pop(video_path, None)
