"""Regression tests for cut_then_encode progress reporting (audit review).

The cut phase used to compute every segment's progress against a
CONSTANT base, so the bar rolled back to zero at the start of each new
segment (0 → 0.1 → 0 → 0.2 → 0 …). Phase 3 (the mux-to-output pass) ran
through ``_run_subprocess_cmd`` which discards stdout, so it reported no
progress at all and the bar jumped from the 0.45 concat slice straight
to 1.0.

Both are fixed: the per-segment base advances with the cumulative
encoded duration (same scheme as segment.py), and the mux pass goes
through ``_run_ffmpeg`` which parses ``-progress pipe:1`` into the
0.5..1.0 span.
"""

from pathlib import Path
from unittest.mock import patch

from stream2video.concat import _run_cut_then_encode


def _has_stream_copy(cmd: list[str]) -> bool:
    if "-c" not in cmd:
        return False
    return cmd[cmd.index("-c") + 1] == "copy"


def _run_and_capture(
    tmp_path: Path,
    keep: list[tuple[float, float]],
    seen: list[float],
    drive_mux: bool = False,
) -> list[tuple[list[str], object]]:
    """Run ``_run_cut_then_encode`` with ``_run_ffmpeg`` mocked, feeding
    each cut segment's progress callback like a real ``-progress``
    stream (``out_time`` from 0 to the segment's ``-t`` duration)."""
    video = tmp_path / "src.mp4"
    video.write_bytes(b"source")
    output = tmp_path / "out.mp4"
    calls: list[tuple[list[str], object]] = []

    def fake_run_ffmpeg(cmd, progress_callback=None, *args, **kwargs):
        cmd = list(cmd)
        calls.append((cmd, progress_callback))
        if progress_callback is None:
            return
        if _has_stream_copy(cmd):
            if drive_mux:
                progress_callback(4.0)  # half of the 8s total in the tests
        else:
            dur = float(cmd[cmd.index("-t") + 1])
            for frac in (0.0, 0.5, 1.0):
                progress_callback(frac * dur)

    with (
        patch("stream2video.concat._run_subprocess_cmd"),
        patch("stream2video.concat._run_final_concat"),
        patch("stream2video.concat._ffprobe_is_valid_mp4", return_value=True),
        patch("stream2video.concat._run_ffmpeg", side_effect=fake_run_ffmpeg),
        patch("stream2video.concat._ensure_fresh_work_dir"),
    ):
        _run_cut_then_encode(
            video,
            keep,
            output,
            "libx264",
            ["-preset", "medium"],
            seen.append,
            None,
            encoder="libx264",
            source_has_audio=False,
        )
    return calls


def test_cut_phase_progress_is_monotonic_no_rollback(tmp_path: Path) -> None:
    # Regression: the per-segment base used to be constant 0.0, so the
    # bar rolled back to zero at the start of every segment
    # (0 → 0.1 → 0 → 0.2 → 0 …). The cumulative-duration base must make
    # the observed progress non-decreasing through the whole cut phase.
    keep = [(0.0, 2.0), (5.0, 7.0), (9.0, 13.0)]  # 2s + 2s + 4s = 8s
    seen: list[float] = []
    calls = _run_and_capture(tmp_path, keep, seen)
    cut_calls = [(cmd, cb) for cmd, cb in calls if not _has_stream_copy(cmd)]
    assert len(cut_calls) == len(keep)
    assert all(cb is not None for _, cb in cut_calls)

    assert seen == sorted(seen), f"progress rolled back: {seen}"
    assert seen[-1] == 1.0
    # Phase 1 stays inside its 0..0.4 span (the trailing 1.0 is the
    # run-complete report) and reaches exactly 0.4 at its end.
    assert max(seen[:-1]) <= 0.4 + 1e-9
    assert 0.4 in seen


def test_mux_phase_progress_maps_into_tail_span(tmp_path: Path) -> None:
    # Regression: phase 3 reported no progress at all (the old
    # _run_subprocess_cmd discarded stdout), so the bar jumped from the
    # 0.45 concat slice straight to 1.0. The mux pass must expose a
    # progress callback mapping out_time into 0.5..1.0, and the run must
    # end at exactly 1.0.
    keep = [(0.0, 4.0), (6.0, 10.0)]  # 4s + 4s = 8s
    seen: list[float] = []
    calls = _run_and_capture(tmp_path, keep, seen, drive_mux=True)
    mux_calls = [(cmd, cb) for cmd, cb in calls if _has_stream_copy(cmd)]
    assert len(mux_calls) == 1
    assert mux_calls[0][1] is not None

    assert seen == sorted(seen), f"progress rolled back: {seen}"
    assert 0.75 in seen  # 0.5 + (4.0 / 8.0) * 0.5
    assert seen[-1] == 1.0


def test_every_ffmpeg_call_requests_progress_pipe(tmp_path: Path) -> None:
    # Both the cut encode and the mux pass must run with
    # -progress pipe:1 before the first -i: _run_ffmpeg parses out_time
    # lines from stdout to drive the progress callback and to reset its
    # stall watchdog (a silent-but-healthy run must never be killed).
    keep = [(0.0, 1.0)]
    seen: list[float] = []
    calls = _run_and_capture(tmp_path, keep, seen)
    assert calls, "expected at least one _run_ffmpeg call"
    for cmd, _cb in calls:
        assert "-progress" in cmd, f"-progress missing from cmd: {cmd}"
        idx = cmd.index("-progress")
        assert cmd[idx + 1] == "pipe:1", f"-progress target must be pipe:1, got: {cmd[idx:]}"
        assert idx < cmd.index("-i"), f"-progress must come before -i: {cmd}"
