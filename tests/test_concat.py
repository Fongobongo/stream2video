from __future__ import annotations

import io
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import pytest

from stream2video.concat import (
    FFmpegOutOfMemoryError,
    _run_subprocess_cmd,
    cut_and_concat,
)


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

    factory = received["memory_monitor_factory"]
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


def test_gapless_uses_tree_when_cmdline_would_exceed_windows_limit(
    tmp_path: Path, monkeypatch
):
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

    ffmpeg = subprocess.check_output(
        ["where", "ffmpeg"], text=True, shell=False
    ).splitlines()[0].strip()
    cmd = [ffmpeg, "-y", "-v", "error", *inputs,
           "-filter_complex", graph, "-map", "[outv]", "-map", "[outa]",
           "-f", "null", "-"]
    assert len(subprocess.list2cmdline(cmd)) > 32767, (
        f"test setup should exceed the Windows limit (got "
        f"{len(subprocess.list2cmdline(cmd))})"
    )

    with pytest.raises(FileNotFoundError) as exc_info:
        subprocess.Popen(cmd, creationflags=subprocess.CREATE_NO_WINDOW)
    assert exc_info.value.winerror == 206, (
        f"expected ERROR_FILENAME_EXCED_RANGE but got {exc_info.value.winerror}"
    )
