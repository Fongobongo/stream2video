"""Tests for stream2video.waveform_popup.LiveSegmentsStore.

The store replaced the GUI's inline ``self._live_segments`` dict +
``self._live_segments_lock`` pair back in Этап 10 with a tiny
unit-testable class. In v0.3+ it was *path-keyed*; that let a popup
opened on the typed input path miss every live publish from a
URL-pipeline run (which is keyed by the *resolved download path*).
The store is now keyed by a monotonically-increasing **run id**: each
pipeline Start bumps the id, the popup polls "the current run"
without naming any path.

Contract pinned here:

  * Producer (pipeline worker) calls :meth:`set` from its stderr drain
    thread; consumer (waveform popup poller) calls
    :meth:`take_snapshot` from the Tk main loop — they exchange data
    across threads; a shallow copy keeps each side stable.
  * ``take_snapshot`` returns ``None`` when nothing has been
    published yet and ``[]`` (a real empty list) once a run has
    begun — the renderer distinguishes the two ("run detect first"
    vs "0 silences so far").
  * ``clear`` drops the current run's data so a re-open of the popup
    doesn't see a stale previous-run view.
  * :meth:`set` with a stale ``run_id`` is refused — a slow worker
    finishing after the user pressed Start again cannot clobber the
    newer run's state.
  * The lock is held around the slot op, NOT around the caller's
    follow-up work — concurrent set + snapshot don't crash.
"""

from __future__ import annotations

import threading

from stream2video.silence import SilenceSegment
from stream2video.waveform_popup import LiveSegmentsStore


def _seg(start: float, end: float) -> SilenceSegment:
    # SilenceSegment is a simple value object; build one with just the
    # fields LiveSegmentsStore cares about. Looking up the real
    # constructor signature dynamically so a future field addition
    # doesn't break this test's helper.
    return SilenceSegment(start=start, end=end)  # type: ignore[call-arg]


class TestLiveSegmentsStoreSetTake:
    def test_take_returns_none_before_any_run(self) -> None:
        store = LiveSegmentsStore()
        assert store.take_snapshot() is None

    def test_take_returns_empty_list_once_run_started(self) -> None:
        # Producer has sent an empty list for the current run
        # (detection started, zero silences found so far). The
        # renderer must NOT confuse this with "never started": an
        # overlay with zero silences is different from "run detect
        # first."
        store = LiveSegmentsStore()
        run_id = store.begin_run()
        store.set(run_id, [])
        snapshot = store.take_snapshot()
        assert snapshot is not None
        assert snapshot == []

    def test_take_returns_copy_of_inserted_list(self) -> None:
        # The producer might keep mutating the list it passed in (the
        # pipeline controller builds segments incrementally). The
        # snapshot we return must be a shallow copy so a later
        # mutation doesn't surface to the consumer.
        store = LiveSegmentsStore()
        run_id = store.begin_run()
        original = [_seg(0.0, 5.0), _seg(10.0, 15.0)]
        store.set(run_id, original)
        snapshot = store.take_snapshot()
        assert snapshot is not None and len(snapshot) == 2
        # Same start/end as the originals.
        assert snapshot[0].start == 0.0 and snapshot[0].end == 5.0
        assert snapshot[1].start == 10.0 and snapshot[1].end == 15.0
        # Mutate the original list — the snapshot must stay stable.
        original.append(_seg(20.0, 25.0))
        assert len(snapshot) == 2  # still the original two segments

    def test_set_replaces_previous_segments_same_run(self) -> None:
        # Each ``on_live_segment`` callback sends the FULL latest list
        # (not an incremental append), so repeated sets should
        # overwrite — not accumulate.
        store = LiveSegmentsStore()
        run_id = store.begin_run()
        store.set(run_id, [_seg(0, 5)])
        store.set(run_id, [_seg(0, 5), _seg(10, 15), _seg(20, 25)])
        snap = store.take_snapshot()
        assert snap is not None
        assert len(snap) == 3


class TestLiveSegmentsStoreClear:
    def test_clear_before_publish_leaves_none(self) -> None:
        store = LiveSegmentsStore()
        store.clear()
        assert store.take_snapshot() is None

    def test_clear_after_publish_resets_to_none(self) -> None:
        store = LiveSegmentsStore()
        run_id = store.begin_run()
        store.set(run_id, [_seg(0, 5)])
        store.clear()
        assert store.take_snapshot() is None

    def test_clear_is_idempotent(self) -> None:
        store = LiveSegmentsStore()
        store.clear()
        store.clear()
        assert store.take_snapshot() is None


class TestLiveSegmentsStoreRunIds:
    def test_begin_run_increments(self) -> None:
        store = LiveSegmentsStore()
        assert store.begin_run() == 1
        assert store.begin_run() == 2
        assert store.begin_run() == 3

    def test_set_with_stale_run_id_refused(self) -> None:
        # A slow worker finishing after the user pressed Start again
        # must not clobber the newer run's state.
        store = LiveSegmentsStore()
        old_id = store.begin_run()
        store.set(old_id, [_seg(0, 5)])
        # User pressed Start again — run 2 begins and clears the slot.
        new_id = store.begin_run()
        assert new_id == old_id + 1
        assert store.take_snapshot() is None
        # The old worker tries to publish late: refused.
        assert store.set(old_id, [_seg(1, 2)]) is False
        # The new run's set works.
        assert store.set(new_id, [_seg(3, 4)]) is True
        snap = store.take_snapshot()
        assert snap is not None and snap[0].start == 3.0

    def test_set_with_future_run_id_refused(self) -> None:
        # Defensive: a run id greater than the current one can't
        # legitimately exist (the worker only ever uses the id it was
        # given by begin_run).
        store = LiveSegmentsStore()
        run_id = store.begin_run()
        store.set(run_id, [_seg(0, 5)])
        assert store.set(run_id + 1, [_seg(9, 10)]) is False
        snap = store.take_snapshot()
        assert snap is not None and snap[0].start == 0.0


class TestLiveSegmentsStoreConcurrency:
    def test_concurrent_set_and_snapshot_dont_raise(self) -> None:
        # Smoke: hammer the store from two threads (one producer, one
        # consumer) for a short window. Past implementations sometimes
        # raised ``KeyError: [...]`` or skipped the lock around the
        # set+copy dance; the test pins that no concurrent access
        # raises.
        store = LiveSegmentsStore()
        run_id = store.begin_run()
        stop = threading.Event()
        errors: list[BaseException] = []

        def _writer() -> None:
            i = 0
            while not stop.is_set():
                try:
                    store.set(run_id, [_seg(j, j + 1) for j in range(i % 50)])
                    i += 1
                except BaseException as e:
                    errors.append(e)
                    return

        def _reader() -> None:
            while not stop.is_set():
                try:
                    store.take_snapshot()
                except BaseException as e:
                    errors.append(e)
                    return

        def _clearer() -> None:
            while not stop.is_set():
                try:
                    store.clear()
                except BaseException as e:
                    errors.append(e)
                    return

        threads = [
            threading.Thread(target=_writer, daemon=True),
            threading.Thread(target=_reader, daemon=True),
            threading.Thread(target=_clearer, daemon=True),
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
