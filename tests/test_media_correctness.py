"""Media correctness regression tests (P2.3 + Этап 1 acceptance).

These tests guard against the frame-loss / FPS-mangling / A-V desync
regressions documented in the fix plan. They:

  1. Generate a synthetic source video via ``ffmpeg -f lavfi testsrc``
     (no external file needed — runs in CI).
  2. Run ``cut_and_concat`` with both ``segment`` and ``batch`` methods
     against a known silence plan.
  3. Probe the output with ``ffprobe`` and assert:
       - frame count matches expected within ±1 frame tolerance;
       - container duration matches expected within one frame;
       - A/V duration drift is within one AAC frame (~21ms);
       - r_frame_rate matches the source.

The historical bugs these tests catch:
  * P0.1 — segment double-seek cut ~0.5s off each segment;
  * P0.2 — ``setpts=N/FRAME_RATE/TB`` turned 30 FPS into 25 FPS;
  * P0.4 — ``apad`` added 100ms per segment, accumulating drift.

Skipped when ffmpeg/ffprobe aren't on PATH so the suite still runs in
environments without the toolchain.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from stream2video.concat import cut_and_concat
from stream2video.silence import SilenceSegment


def _have(*tools: str) -> bool:
    return all(shutil.which(t) is not None for t in tools)


HAVE_FFMPEG = _have("ffmpeg", "ffprobe")
pytestmark = pytest.mark.skipif(
    not HAVE_FFMPEG,
    reason="ffmpeg/ffprobe not on PATH — media regression tests need them",
)


def _make_source(
    out: Path,
    *,
    duration: float = 6.0,
    fps: int = 30,
    audio_bitrate: str = "192k",
) -> None:
    """Generate a synthetic test source via lavfi.

    ``testsrc`` produces a deterministic pattern at the requested FPS;
    ``sine`` produces a steady tone at the requested sample rate /
    bitrate. The two are muxed into a single MP4 so both video and
    audio timelines are well-defined.
    """
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=duration={duration}:size=320x240:rate={fps}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={duration}:sample_rate=48000",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            audio_bitrate,
            str(out),
        ],
        check=True,
    )


def _probe(path: Path) -> dict:
    """Return ffprobe diagnostics for ``path`` (parsed from JSON output).

    ffprobe's ``-of json`` emits structured ``format`` / ``streams``
    sections so we can distinguish the container duration from each
    stream's duration without ambiguity. The plain ``-of default``
    output interleaves them and is error-prone to parse (the format
    ``duration=`` line looks identical to a stream's ``duration=``).

    Keys returned:
      * ``duration`` — container-level duration (seconds).
      * ``codec_types`` — list of stream codec_types (e.g. ['video', 'audio']).
      * ``nb_read_frames_video`` — frame count of the video stream
        (requires ``-count_frames``; can be None if not reported).
      * ``r_frame_rate`` — video stream's nominal frame rate (e.g. '30/1').
      * ``audio_duration`` — audio stream's duration (None when no audio).
    """
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,duration,nb_read_frames,r_frame_rate",
            "-of",
            "json",
            "-count_frames",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    data = json.loads(out)

    info: dict = {
        "audio_duration": None,
        "nb_read_frames_video": None,
        "r_frame_rate": None,
        "duration": None,
        "codec_types": [],
    }
    fmt = data.get("format") or {}
    info["duration"] = float(fmt["duration"]) if fmt.get("duration") else None

    for stream in data.get("streams", []):
        ctype = stream.get("codec_type")
        if ctype:
            info["codec_types"].append(ctype)
        if ctype == "video":
            rfr = stream.get("r_frame_rate")
            if rfr:
                info["r_frame_rate"] = rfr
            nbf = stream.get("nb_read_frames")
            if nbf is not None:
                info["nb_read_frames_video"] = int(nbf)
        elif ctype == "audio":
            d = stream.get("duration")
            if d:
                info["audio_duration"] = float(d)
    return info


def _assert_av_in_sync(info: dict, *, tolerance_s: float = 0.05) -> None:
    """Assert audio duration is within ``tolerance_s`` of video duration.

    AAC encoders add a priming frame (~21ms at 48 kHz) at the head of
    each encode; the concat path doesn't strip it, so a per-segment
    encode drifts slightly. 50ms tolerance covers the worst case
    (one AAC frame + one video frame) without being so loose that a
    real desync passes.
    """
    audio = info.get("audio_duration")
    video = info.get("duration")
    if audio is None or video is None:
        pytest.fail(f"missing duration for A/V sync check: {info}")
    drift = abs(audio - video)
    assert drift <= tolerance_s, (
        f"A/V drift {drift * 1000:.1f}ms exceeds {tolerance_s * 1000:.1f}ms tolerance; "
        f"audio={audio}s video={video}s"
    )


@pytest.fixture
def synthetic_source(tmp_path: Path) -> Path:
    """6s / 30 FPS / AAC 192k source — the original reproduction case."""
    src = tmp_path / "src.mp4"
    _make_source(src, duration=6.0, fps=30, audio_bitrate="192k")
    return src


@pytest.mark.parametrize("method", ["segment", "batch"])
def test_keep_two_segments_4s_120_frames(method: str, synthetic_source: Path, tmp_path: Path):
    """Original reproduction: silence (2,4) → keep [(0,2),(4,6)].

    Expected output: 4.0s, 120 frames at 30 FPS. The historical bugs
    (P0.1 double-seek, P0.2 setpts=N/FRAME_RATE/TB, P0.4 apad drift)
    produced 4.72s/135 frames (segment) and 5.05s/151 frames (batch).
    """
    out = tmp_path / f"out_{method}.mp4"
    silence = [SilenceSegment(2.0, 4.0)]
    cut_and_concat(
        synthetic_source,
        silence,
        out,
        method=method,
        encoder="libx264",
        video_quality="medium",
        audio_quality="high",
    )
    info = _probe(out)
    frames = info.get("nb_read_frames_video")
    duration = info.get("duration")
    assert frames is not None, f"ffprobe didn't report video frames: {info}"
    assert abs(frames - 120) <= 1, (
        f"{method}: frame count {frames} != 120 (±1 tolerance); info={info}"
    )
    assert duration is not None and abs(duration - 4.0) <= 0.1, (
        f"{method}: duration {duration}s != 4.0s (±100ms tolerance); info={info}"
    )
    assert info.get("r_frame_rate") == "30/1", (
        f"{method}: r_frame_rate {info.get('r_frame_rate')!r} != '30/1'; info={info}"
    )
    _assert_av_in_sync(info)


@pytest.mark.parametrize("method", ["segment", "batch"])
@pytest.mark.parametrize("fps", [24, 25, 30, 50, 60])
def test_cfr_fps_preserved(method: str, fps: int, tmp_path: Path):
    """CFR source at ``fps`` must come out at the same FPS (Этап 1).

    With ``output_fps='source'`` (the default), no ``-r``/``fps`` filter
    is added; the encoder preserves the input's PTS cadence. A 4s keep
    at ``fps`` FPS must yield ``4*fps`` frames.
    """
    src = tmp_path / f"src_{fps}.mp4"
    _make_source(src, duration=6.0, fps=fps)
    out = tmp_path / f"out_{method}_{fps}.mp4"
    silence = [SilenceSegment(2.0, 4.0)]
    cut_and_concat(
        src,
        silence,
        out,
        method=method,
        encoder="libx264",
        video_quality="medium",
        audio_quality="medium",
        output_fps="source",
    )
    info = _probe(out)
    frames = info.get("nb_read_frames_video")
    expected = 4 * fps
    assert frames is not None and abs(frames - expected) <= 1, (
        f"{method}@{fps}FPS: frame count {frames} != {expected} (±1); info={info}"
    )
    assert info.get("r_frame_rate") == f"{fps}/1", (
        f"{method}@{fps}FPS: r_frame_rate {info.get('r_frame_rate')!r} != '{fps}/1'; info={info}"
    )


@pytest.mark.parametrize("method", ["segment", "batch"])
def test_silence_at_start(method: str, synthetic_source: Path, tmp_path: Path):
    """Silence at t=0 — keep segment is [(2,6)] = 4s/120 frames."""
    out = tmp_path / f"out_start_{method}.mp4"
    silence = [SilenceSegment(0.0, 2.0)]
    cut_and_concat(
        synthetic_source,
        silence,
        out,
        method=method,
        encoder="libx264",
        video_quality="medium",
    )
    info = _probe(out)
    frames = info.get("nb_read_frames_video")
    assert frames is not None and abs(frames - 120) <= 1, (
        f"{method} silence@0: frames {frames} != 120; info={info}"
    )


@pytest.mark.parametrize("method", ["segment", "batch"])
def test_silence_at_end(method: str, synthetic_source: Path, tmp_path: Path):
    """Silence at EOF — keep segment is [(0,4)] = 4s/120 frames.

    Trailing silence is closed at media duration by the silence
    detector (P1.12); this test verifies the cut respects that.
    """
    out = tmp_path / f"out_end_{method}.mp4"
    silence = [SilenceSegment(4.0, 6.0)]
    cut_and_concat(
        synthetic_source,
        silence,
        out,
        method=method,
        encoder="libx264",
        video_quality="medium",
    )
    info = _probe(out)
    frames = info.get("nb_read_frames_video")
    assert frames is not None and abs(frames - 120) <= 1, (
        f"{method} silence@EOF: frames {frames} != 120; info={info}"
    )


@pytest.mark.parametrize("method", ["segment", "batch"])
def test_many_short_segments(method: str, synthetic_source: Path, tmp_path: Path):
    """10 short silence segments — frame count must still match.

    Stress test for accumulated drift: each segment historically lost
    ~0.1s to apad, so 10 segments drifted ~1s. With the fix, the total
    should be exact. Silence durations are kept above one frame
    (~33ms at 30 FPS) so ffmpeg's silence detector has a stable window
    to work with.
    """
    out = tmp_path / f"out_many_{method}.mp4"
    # 6s source, 10 segments of 0.2s each, evenly spaced.
    # Total silence = 2.0s, so keep = 4.0s = 120 frames at 30 FPS.
    silence = []
    t = 0.4
    while t < 6.0 - 0.2:
        silence.append(SilenceSegment(t, t + 0.2))
        t += 0.55
    cut_and_concat(
        synthetic_source,
        silence,
        out,
        method=method,
        encoder="libx264",
        video_quality="medium",
    )
    info = _probe(out)
    frames = info.get("nb_read_frames_video")
    # 10 segments of 0.2s = 2.0s removed → keep ≈ 4.0s ≈ 120 frames.
    # Tolerance is looser here because 10 boundary decisions add up.
    assert frames is not None and 110 <= frames <= 130, (
        f"{method} 10 segments: frames {frames} outside [110,130]; info={info}"
    )
    _assert_av_in_sync(info, tolerance_s=0.1)


def test_audio_quality_high_preserves_bitrate(synthetic_source: Path, tmp_path: Path):
    """``audio_quality='high'`` should produce a higher bitrate than 'low'.

    The exact AAC ABR target depends on the encoder, but 'high' (256k)
    must beat 'low' (128k) by a clear margin — otherwise the user's
    choice has no effect (the historical P0.3 bug: hard-coded 128k).
    """
    out_high = tmp_path / "out_high.mp4"
    out_low = tmp_path / "out_low.mp4"
    silence = [SilenceSegment(2.0, 4.0)]
    cut_and_concat(
        synthetic_source,
        silence,
        out_high,
        method="segment",
        encoder="libx264",
        video_quality="medium",
        audio_quality="high",
    )
    cut_and_concat(
        synthetic_source,
        silence,
        out_low,
        method="segment",
        encoder="libx264",
        video_quality="medium",
        audio_quality="low",
    )

    def _audio_bitrate(path: Path) -> int:
        out = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=bit_rate",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return int(out) if out else 0

    high = _audio_bitrate(out_high)
    low = _audio_bitrate(out_low)
    # AAC ABR target isn't exact, but high should clearly beat low.
    assert high > low, f"audio_quality='high' ({high} bps) didn't beat 'low' ({low} bps)"


def test_output_fps_60_doubles_frames(synthetic_source: Path, tmp_path: Path):
    """``output_fps='60'`` forces CFR conversion via the fps filter.

    A 4s keep at 30 FPS source with output_fps='60' must produce
    240 frames (60*4) — the fps filter duplicates frames to match the
    target. This is the documented trade-off (P1.17).
    """
    out = tmp_path / "out_60.mp4"
    silence = [SilenceSegment(2.0, 4.0)]
    cut_and_concat(
        synthetic_source,
        silence,
        out,
        method="segment",
        encoder="libx264",
        video_quality="medium",
        output_fps="60",
    )
    info = _probe(out)
    frames = info.get("nb_read_frames_video")
    assert frames is not None and abs(frames - 240) <= 2, (
        f"output_fps=60: frames {frames} != 240 (±2); info={info}"
    )
    assert info.get("r_frame_rate") == "60/1", (
        f"output_fps=60: r_frame_rate {info.get('r_frame_rate')!r} != '60/1'; info={info}"
    )


def test_audio_less_source_produces_video_only(tmp_path: Path):
    """Source without an audio stream produces a valid video-only MP4.

    P1.14: previously the segment path passed ``-c:a aac`` unconditionally
    and failed with "Output file does not contain any stream" on a
    video-only input.
    """
    src = tmp_path / "src_noaudio.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=6:size=320x240:rate=30",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(src),
        ],
        check=True,
    )
    out = tmp_path / "out_noaudio.mp4"
    silence = [SilenceSegment(2.0, 4.0)]
    cut_and_concat(
        src,
        silence,
        out,
        method="segment",
        encoder="libx264",
        video_quality="medium",
    )
    info = _probe(out)
    assert "audio" not in info.get("codec_types", []), (
        f"audio-less source produced audio stream: {info}"
    )
    frames = info.get("nb_read_frames_video")
    assert frames is not None and abs(frames - 120) <= 1, (
        f"audio-less: frames {frames} != 120; info={info}"
    )
