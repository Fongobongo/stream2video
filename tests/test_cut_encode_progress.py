"""Regression test for the cut_then_encode stall-kill bug (audit #2).

The phase-3 final encode previously ran WITHOUT ``-progress pipe:1``. The
``_run_ffmpeg`` stall watchdog resets its timer only on ``out_time_us=``
lines read from stdout, so a silent-but-healthy encode (any run longer than
``stall_kill`` seconds, default 300s) was killed mid-encode as "stalled".
"""

from pathlib import Path
from unittest.mock import patch

from stream2video.concat import _run_cut_then_encode


def _run_and_capture_ffmpeg_cmd(tmp_path: Path) -> list[str]:
    video = tmp_path / "src.mp4"
    video.write_bytes(b"source")
    output = tmp_path / "out.mp4"
    keep = [(0.0, 1.0)]
    captured: list[list[str]] = []

    def fake_run_ffmpeg(cmd, *args, **kwargs):
        captured.append(list(cmd))

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
            None,
            None,
            encoder="libx264",
            source_has_audio=False,
        )
    assert len(captured) == 1, f"expected exactly one _run_ffmpeg call, got {len(captured)}"
    return captured[0]


def test_final_encode_requests_progress_pipe(tmp_path: Path) -> None:
    cmd = _run_and_capture_ffmpeg_cmd(tmp_path)
    assert "-progress" in cmd, f"-progress missing from final encode cmd: {cmd}"
    idx = cmd.index("-progress")
    assert cmd[idx + 1] == "pipe:1", f"-progress target must be pipe:1, got: {cmd[idx:]}"


def test_progress_flag_precedes_input(tmp_path: Path) -> None:
    """-progress is a global/output option; it must appear before the first -i
    so ffmpeg wires the pipe before opening the output."""
    cmd = _run_and_capture_ffmpeg_cmd(tmp_path)
    assert cmd.index("-progress") < cmd.index("-i"), (
        f"-progress must come before -i: {cmd}"
    )
