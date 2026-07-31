from __future__ import annotations

import io
from pathlib import Path
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
        args = ["ffmpeg"]

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
