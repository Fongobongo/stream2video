from __future__ import annotations

import io
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import pytest

from stream2video.concat import (
    FFmpegOutOfMemoryError,
    _run_final_concat,
    _run_subprocess_cmd,
    cut_and_concat,
)


def _final_concat_cmd(tmp_path: Path, **kwargs) -> list[str]:
    """Run ``_run_final_concat`` with mocked ffmpeg and return its args."""

    captured: dict = {}

    def fake_run_ffmpeg(args, **kw):
        captured["args"] = args

    work = tmp_path / "work"
    work.mkdir()
    parts = []
    for i in range(2):
        p = work / f"seg_{i:06d}.mp4"
        p.write_bytes(b"\x00")
        parts.append(p)
    with patch("stream2video.concat._run_ffmpeg", side_effect=fake_run_ffmpeg):
        _run_final_concat(
            work,
            tmp_path / "out.mp4",
            parts,
            total_duration=4.0,
            progress_callback=None,
            cancel_callback=None,
            label="test",
            **kwargs,
        )
    return captured["args"]


def test_final_concat_fresh_set_stream_copies(tmp_path: Path):
    args = _final_concat_cmd(tmp_path, audio_resync=False)
    assert args[args.index("-f") + 1] == "concat"
    assert "-c" in args and args[args.index("-c") + 1] == "copy"
    assert "aresample" not in args


def test_final_concat_mixed_set_resyncs_audio(tmp_path: Path):
    args = _final_concat_cmd(tmp_path, audio_resync=True, audio_quality="high")
    # Video stays lossless; only the audio is re-encoded through the
    # async resampler with the caller's quality bitrate.
    assert args[args.index("-c:v") + 1] == "copy"
    af = args[args.index("-af") + 1]
    assert af == "aresample=async=1:first_pts=0"
    assert args[args.index("-c:a") + 1] == "aac"
    assert args[args.index("-b:a") + 1] == "256k"


def test_cut_and_concat_builds_memory_monitor_factory(tmp_path: Path):
    video = tmp_path / "src.mp4"
    video.write_bytes(b"source")
    output = tmp_path / "out.mp4"
    received: dict = {}

    def fake_run_with_fallback(*args, **kwargs):
        received.update(kwargs)

    with (
        patch("stream2video.concat.generate_keep_segments", return_value=[(0.0, 1.0)]),
        patch("stream2video.concat.get_video_encoder", return_value=("libx264", [])),
        patch("stream2video.concat.has_audio_stream", return_value=True),
        patch("stream2video.concat._run_with_fallback", side_effect=fake_run_with_fallback),
    ):
        cut_and_concat(video, [], output, memory_limit_mb=1024, memory_reserve_mb=512)

    factory = received["options"].memory_monitor_factory
    monitor = factory("unit")
    assert monitor is not None
    assert monitor.memory_limit_mb == 1024
    assert monitor.memory_reserve_mb == 512


def test_run_subprocess_cmd_waits_for_stderr_drain_before_oom_classification():
    class _FakeProcess:
        args: ClassVar[list[str]] = ["ffmpeg"]

        def __init__(self):
            self.stderr = io.BytesIO()
            self.returncode = 137

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    def fake_drain(pipe, sink, on_line=None):
        def wait_for_drain():
            sink.append("Cannot allocate memory\n")

        return wait_for_drain

    with (
        patch("stream2video.concat.subprocess.Popen", return_value=_FakeProcess()),
        patch("stream2video.concat.drain_stderr_lines", side_effect=fake_drain),
        pytest.raises(FFmpegOutOfMemoryError),
    ):
        _run_subprocess_cmd(["ffmpeg"], timeout=5, label="cut phase")


def test_gapless_uses_tree_when_cmdline_would_exceed_windows_limit(tmp_path: Path, monkeypatch):
    """After the 2026-08-02/03 incident the gapless path never falls back
    to the concat demuxer: it splits many inputs into groups joined via
    intermediates (binary tree) and runs the final encode once.

    This test drives ``_run_gapless_segment_concat`` with a shrunken
    ``_GAPLESS_MAX_INPUTS_PER_CALL`` so the tree logic is exercised with
    just a handful of synthetic parts. The flat helper is replaced with a
    recorder; every "leaf" call must receive ≤ the cap.
    """
    from stream2video import concat as concat_mod

    recorded_leaf_sizes: list[int] = []
    recorded_outputs: list[Path] = []

    def fake_one_pass(part_paths, output_path, vcodec, vcodec_opts, **kwargs):
        recorded_leaf_sizes.append(len(part_paths))
        recorded_outputs.append(Path(output_path))

    monkeypatch.setattr(concat_mod, "_concat_filter_one_pass", fake_one_pass)
    monkeypatch.setattr(concat_mod, "_GAPLESS_MAX_INPUTS_PER_CALL", 2)

    parts = [tmp_path / f"seg_{i:06d}.mp4" for i in range(5)]
    for p in parts:
        p.write_bytes(b"\x00" * 2048)
    out = tmp_path / "out.mp4"

    concat_mod._run_gapless_segment_concat(
        out,
        parts,
        "libx264",
        [],
        audio_quality="medium",
        total_duration=4.5,
    )

    # 5 parts, cap 2 → tree: 2+2+1 joins at L0 (3 leaves), then 1+1 join
    # at L1 (1 leaf of 2 intermediates), then final. Every leaf ≤ 2.
    assert recorded_leaf_sizes, "no concat calls recorded"
    assert max(recorded_leaf_sizes) <= 2, recorded_leaf_sizes
    # Final call target is the real output path.
    assert recorded_outputs[-1] == out


def test_gapless_real_ffmpeg_long_command_line_fails(tmp_path: Path):
    """Regression test: prove that ffmpeg's concat filter with N inputs and
    an inline filtergraph exceeds the Win32 32K cmdline limit for large N
    (the actual failure: winerror=206). Uses a real ffmpeg spawn.

    Skipped on non-Windows hosts (the limit doesn't apply there).
    """
    import subprocess
    import sys

    if sys.platform != "win32":
        pytest.skip("Windows-specific cmdline length limit")

    # Build a pathologically long ffmpeg command: many -i inputs + a filter
    # graph. Using exist_ok dummy paths is fine — we only care that
    # CreateProcess itself fails with 206, before ffmpeg gets to read anything.
    n = 600  # ≈ 64K chars of -i args + filter, definitively > 32767
    fake_dir = tmp_path / "segs"
    fake_dir.mkdir()
    inputs: list[str] = []
    graph_parts: list[str] = []
    for i in range(n):
        p = fake_dir / f"s_{i:06d}.mp4"
        p.write_bytes(b"\x00")
        inputs.extend(["-i", str(p)])
        graph_parts.append(f"[{i}:v][{i}:a]")
    graph = "".join(graph_parts) + f"concat=n={n}:v=1:a=1[outv][outa]"

    ffmpeg = (
        subprocess.check_output(["where", "ffmpeg"], text=True, shell=False).splitlines()[0].strip()
    )
    cmd = [
        ffmpeg,
        "-y",
        "-v",
        "error",
        *inputs,
        "-filter_complex",
        graph,
        "-map",
        "[outv]",
        "-map",
        "[outa]",
        "-f",
        "null",
        "-",
    ]
    assert len(subprocess.list2cmdline(cmd)) > 32767, (
        f"test setup should exceed the Windows limit (got {len(subprocess.list2cmdline(cmd))})"
    )

    with pytest.raises(FileNotFoundError) as exc_info:
        subprocess.Popen(cmd, creationflags=subprocess.CREATE_NO_WINDOW)
    assert exc_info.value.winerror == 206, (
        f"expected ERROR_FILENAME_EXCED_RANGE but got {exc_info.value.winerror}"
    )


class TestFfprobeDurationOkFailClosed:
    """The resume-part duration gate must fail CLOSED.

    A part whose duration ffprobe cannot read (rc != 0, empty output,
    non-numeric output, timeout, missing binary) must NOT be accepted —
    a truncated-but-readable resume part (valid moov, short body) would
    otherwise pass the integrity gate and inject a hole into the final
    output (static-audit finding).
    """

    def _probe(self, tmp_path: Path, result, *, raise_exc=None):
        from stream2video.concat.probing import _ffprobe_duration_ok

        exc = raise_exc

        def _run(cmd, **kwargs):
            if exc is not None:
                raise exc("ffprobe", 30)
            return result

        with (
            patch("stream2video.concat.probing.ffprobe_path", return_value="ffprobe"),
            patch("stream2video.concat.probing.run_with_retry", side_effect=_run),
        ):
            return _ffprobe_duration_ok(tmp_path / "part.mp4", expected_seconds=10.0)

    def test_rc_nonzero_is_rejected(self, tmp_path: Path):
        from subprocess import CompletedProcess

        result = CompletedProcess(args=[], returncode=1, stdout="", stderr="error")
        assert self._probe(tmp_path, result) is False

    def test_empty_stdout_is_rejected(self, tmp_path: Path):
        from subprocess import CompletedProcess

        result = CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        assert self._probe(tmp_path, result) is False

    def test_non_numeric_duration_is_rejected(self, tmp_path: Path):
        from subprocess import CompletedProcess

        result = CompletedProcess(args=[], returncode=0, stdout="N/A\n", stderr="")
        assert self._probe(tmp_path, result) is False

    def test_timeout_is_rejected(self, tmp_path: Path):
        from subprocess import TimeoutExpired

        assert self._probe(tmp_path, None, raise_exc=TimeoutExpired) is False

    def test_matching_duration_is_accepted(self, tmp_path: Path):
        from subprocess import CompletedProcess

        result = CompletedProcess(args=[], returncode=0, stdout="10.5\n", stderr="")
        assert self._probe(tmp_path, result) is True

    def test_mismatched_duration_is_rejected(self, tmp_path: Path):
        from subprocess import CompletedProcess

        result = CompletedProcess(args=[], returncode=0, stdout="4.0\n", stderr="")
        assert self._probe(tmp_path, result) is False


class TestFfmpegDecodeProbe:
    """The publish-gate decode probe must fail CLOSED on truncation.

    A truncated payload does not always fail the ffmpeg process: with
    a moov-at-start file it warns ``partial file`` on stderr and still
    exits 0 (observed live, audit round 28 P1). The probe treats every
    truncation marker on stderr as a failed decode — a healthy file
    decodes with a clean stderr under ``-v error``."""

    def _probe(self, result, *, raise_exc=None):
        from stream2video.concat.probing import _ffmpeg_decode_probe

        exc = raise_exc

        def _run(cmd, **kwargs):
            if exc is not None:
                raise exc("ffmpeg", 60)
            return result

        with (
            patch("stream2video.concat.probing.ffmpeg_path", return_value="ffmpeg"),
            patch("stream2video.concat.probing.run_with_retry", side_effect=_run),
        ):
            return _ffmpeg_decode_probe(Path("part.mp4"), 5.0, "v")

    def test_clean_decode_accepted(self):
        from subprocess import CompletedProcess

        result = CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        assert self._probe(result) is True

    def test_rc_nonzero_rejected(self):
        from subprocess import CompletedProcess

        result = CompletedProcess(args=[], returncode=1, stdout="", stderr="")
        assert self._probe(result) is False

    def test_partial_file_marker_rejected(self):
        from subprocess import CompletedProcess

        result = CompletedProcess(
            args=[], returncode=0, stdout="", stderr="stream 0, offset 0x24a7: partial file"
        )
        assert self._probe(result) is False

    def test_packet_corrupt_marker_rejected(self):
        from subprocess import CompletedProcess

        result = CompletedProcess(args=[], returncode=0, stdout="", stderr="[mov] packet corrupt")
        assert self._probe(result) is False

    def test_spawn_fault_propagates(self):
        import pytest

        with pytest.raises(FileNotFoundError):
            self._probe(None, raise_exc=FileNotFoundError)


class TestMakePhaseProgress:
    """_make_phase_progress — the single shared progress funnel (audit
    round 14 P3: the wrapper used to be built twice in _run_locked and
    the video path threw the first instance away). Pins the mapping and
    the monotonic clamp on the one instance both paths now share."""

    def test_legacy_callback_passthrough_when_no_on_phase(self):
        from stream2video.concat.api import _make_phase_progress

        seen: list[float] = []
        cb = _make_phase_progress(seen.append, None)
        assert cb is not None
        cb(0.4)
        cb(0.95)
        assert seen == [0.4, 0.95]

    def test_phase_mapping_splits_90_10(self):
        from stream2video.concat.api import _make_phase_progress

        phases: list[tuple[str, float]] = []
        cb = _make_phase_progress(None, lambda phase, frac: phases.append((phase, frac)))
        cb(0.45)  # cutting: 0.45 / 0.9
        cb(0.99)  # concatenating: (0.99 - 0.9) / 0.1
        cb(1.0)  # capped to concatenating 1.0
        assert phases == [
            ("cutting", 0.5),
            ("concatenating", pytest.approx(0.9)),
            ("concatenating", 1.0),
        ]

    def test_monotonic_clamp_drops_backwards_reports(self):
        from stream2video.concat.api import _make_phase_progress

        seen: list[float] = []
        cb = _make_phase_progress(seen.append, None)
        cb(0.9)
        cb(0.8955)  # ffmpeg out_time_us dip near the tail
        cb(0.95)
        assert seen == [0.9, 0.95]

    def test_none_when_no_callbacks(self):
        from stream2video.concat.api import _make_phase_progress

        assert _make_phase_progress(None, None) is None
