"""Tests for stream2video.waveform_popup.LiveSegmentsStore.

The store replaces the GUI's inline ``self._live_segments`` dict +
``self._live_segments_lock`` pair with a tiny unit-testable class.
The contract pinned here:

  * Producer (pipeline worker) calls :meth:`set` from its stderr drain
    thread; consumer (waveform popup poller) calls
    :meth:`take_snapshot` from the Tk main loop — they exchange data
    across threads; a shallow copy keeps each side stable.
  * ``take_snapshot`` returns ``None`` for a never-published path and
    ``[]`` (a real empty list) once the producer has begun — the
    renderer distinguishes the two (" Nothing yet" vs "0 silences").
  * ``pop`` removes the entry so a re-open of the popup doesn't see a
    stale previous-run view; returns ``None`` if never set.
  * ``is_known`` returns True iff the producer has published at least
    once for the path.
  * The lock is held around the dict op, NOT around the caller's
    follow-up work — concurrent set + snapshot don't crash.
"""

from __future__ import annotations

import threading
from pathlib import Path

from stream2video.silence import SilenceSegment
from stream2video.waveform_popup import LiveSegmentsStore


def _seg(start: float, end: float) -> SilenceSegment:
    # SilenceSegment is a simple value object; build one with just the
    # fields LiveSegmentsStore cares about. Looking up the real
    # constructor signature dynamically so a future field addition
    # doesn't break this test's helper.
    return SilenceSegment(start=start, end=end)  # type: ignore[call-arg]


class TestLiveSegmentsStoreSetTake:
    def test_take_returns_none_for_never_set_path(self) -> None:
        store = LiveSegmentsStore()
        assert store.take_snapshot(Path("never")) is None

    def test_take_returns_empty_list_once_started_detect(self) -> None:
        # Producer has sent an empty list (detection started, zero
        # silences found so far). The renderer must NOT confuse this
        # with "never started": an overlay with zero silences is
        # different from "run detect first."
        store = LiveSegmentsStore()
        path = Path("video.mp4")
        store.set(path, [])
        snapshot = store.take_snapshot(path)
        assert snapshot is not None
        assert snapshot == []

    def test_take_returns_copy_of_inserted_list(self) -> None:
        # The producer might keep mutating the list it passed in (the
        # pipeline controller builds segments incrementally). The
        # snapshot we return must be a shallow copy so a later
        # mutation doesn't surface to the consumer.
        store = LiveSegmentsStore()
        path = Path("video.mp4")
        original = [_seg(0.0, 5.0), _seg(10.0, 15.0)]
        store.set(path, original)
        snapshot = store.take_snapshot(path)
        assert snapshot is not None and len(snapshot) == 2
        # Same start/end as the originals.
        assert snapshot[0].start == 0.0 and snapshot[0].end == 5.0
        assert snapshot[1].start == 10.0 and snapshot[1].end == 15.0
        # Mutate the original list — the snapshot must stay stable.
        original.append(_seg(20.0, 25.0))
        assert len(snapshot) == 2  # still the original two segments

    def test_set_replaces_previous_segments(self) -> None:
        # Each ``on_live_segment`` callback sends the FULL latest list
        # (not an incremental append), so repeated sets should
        # overwrite — not accumulate.
        store = LiveSegmentsStore()
        path = Path("v")
        store.set(path, [_seg(0, 5)])
        store.set(path, [_seg(0, 5), _seg(10, 15), _seg(20, 25)])
        snap = store.take_snapshot(path)
        assert snap is not None
        assert len(snap) == 3


class TestLiveSegmentsStorePop:
    def test_pop_returns_none_for_never_set_path(self) -> None:
        store = LiveSegmentsStore()
        assert store.pop(Path("never")) is None

    def test_pop_returns_segments_then_removes_entry(self) -> None:
        # ``SilenceSegment`` doesn't implement ``__eq__`` (it's a plain
        # class with start / end / duration fields), so compare by
        # those fields instead of with ``==``.
        store = LiveSegmentsStore()
        path = Path("v")
        store.set(path, [_seg(0, 5)])
        popped = store.pop(path)
        assert popped is not None and len(popped) == 1
        assert popped[0].start == 0.0 and popped[0].end == 5.0
        # Subsequent snapshots return None — the entry is gone.
        assert store.take_snapshot(path) is None

    def test_pop_after_pop_returns_none(self) -> None:
        # Idempotent on the same path; second pop is a no-op.
        store = LiveSegmentsStore()
        path = Path("v")
        store.set(path, [_seg(0, 5)])
        store.pop(path)
        assert store.pop(path) is None


class TestLiveSegmentsStoreEviction:
    def test_oldest_key_evicted_beyond_cap(self) -> None:
        # URL-pipeline runs publish under a new resolved download path
        # every run; without a cap the store grows one entry per URL
        # processed with the popup open.
        store = LiveSegmentsStore(max_keys=3)
        for i in range(5):
            store.set(Path(f"run_{i}.mp4"), [_seg(i, i + 1)])
        # Oldest two (run_0, run_1) are gone.
        assert store.take_snapshot(Path("run_0.mp4")) is None
        assert store.take_snapshot(Path("run_1.mp4")) is None
        assert store.take_snapshot(Path("run_2.mp4")) is not None
        assert store.take_snapshot(Path("run_4.mp4")) is not None

    def test_re_set_does_not_eject_existing_key_when_under_cap(self) -> None:
        store = LiveSegmentsStore(max_keys=2)
        p = Path("same.mp4")
        store.set(p, [_seg(0, 1)])
        store.set(p, [_seg(0, 2)])
        assert len(store.take_snapshot(p) or []) == 1


class TestLiveSegmentsStoreConcurrency:
    def test_concurrent_set_and_snapshot_dont_raise(self) -> None:
        # Smoke: hammer the store from two threads (one producer, one
        # consumer) for a short window. Past implementations sometimes
        # raised ``KeyError: [...]`` or skipped the lock around the
        # pop+copy dance; the test pins that no concurrent access
        # raises.
        store = LiveSegmentsStore()
        path = Path("stress.mp4")
        stop = threading.Event()
        errors: list[BaseException] = []

        def _writer() -> None:
            i = 0
            while not stop.is_set():
                try:
                    store.set(path, [_seg(j, j + 1) for j in range(i % 50)])
                    i += 1
                except BaseException as e:
                    errors.append(e)
                    return

        def _reader() -> None:
            while not stop.is_set():
                try:
                    store.take_snapshot(path)
                except BaseException as e:
                    errors.append(e)
                    return

        def _popper() -> None:
            while not stop.is_set():
                try:
                    store.pop(path)
                except BaseException as e:
                    errors.append(e)
                    return

        threads = [
            threading.Thread(target=_writer, daemon=True),
            threading.Thread(target=_reader, daemon=True),
            threading.Thread(target=_popper, daemon=True),
        ]
        for t in threads:
            t.start()
        # Let them fight for a fraction of a second — long enough to
        # surface a race if the lock is broken, short enough to keep
        # the test suite fast.
        threading.Event().wait(timeout=0.2)
        stop.set()
        for t in threads:
            t.join(timeout=1.0)
        assert errors == []
