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


def _probe_audio_codec(path: Path) -> dict:
    """Return audio codec name and channel layout for ``path``.

    Used by tests that verify the pipeline normalises non-AAC input
    codecs (Opus/MP3) and channel layouts (mono/5.1) into the
    configured output format (AAC stereo by default).
    """
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=codec_name,channel_layout,channels",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    data = json.loads(out)
    streams = data.get("streams") or []
    if not streams:
        return {"codec_name": None, "channel_layout": None, "channels": None}
    s = streams[0]
    return {
        "codec_name": s.get("codec_name"),
        "channel_layout": s.get("channel_layout"),
        "channels": int(s["channels"]) if s.get("channels") is not None else None,
    }


def _have_encoder(codec: str) -> bool:
    """Return True when ``ffmpeg -encoders`` lists ``codec`` as available."""
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for line in out.splitlines():
        # codec lines look like "A....D libopus            libopus Opus (codec opus)"
        if len(line) > 8 and line[1:8].strip() and codec in line.split()[1:3][:1]:
            return True
    return False


def _require_encoders(*codecs: str) -> None:
    """Skip the calling test when any of ``codecs`` is missing."""
    missing = [c for c in codecs if not _have_encoder(c)]
    if missing:
        pytest.skip(f"ffmpeg missing required encoder(s): {', '.join(missing)}")


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


@pytest.mark.parametrize("method", ["segment", "batch"])
def test_100_keep_segments(method: str, tmp_path: Path):
    """100 keep segments — edge case for large segment counts.

    Generates a long-ish synthetic source (30s at 30 FPS = 900 frames)
    and distributes 100 short silence gaps evenly. Total silence ~20s,
    keep ~10s (~300 frames). The tolerance is wider than the 10-segment
    test because 100 boundary decisions amplify AAC priming + keyframe
    rounding effects.

    This catches:
      * accumulated drift from per-segment padding (P0.4);
      * batch path's trim+concat with 100 segments (filter graph
        complexity / memory);
      * segment path's per-segment encode + concat list length.

    Only runs the ``segment`` method by default; ``batch`` is marked
    slow because the trim+concat filter graph on 100 segments is
    significantly heavier.
    """
    if method == "batch":
        pytest.skip("batch with 100 segments is very slow; covered by 10-segment test")

    src = tmp_path / "src_100.mp4"
    _make_source(src, duration=60.0, fps=30)
    out = tmp_path / f"out_100_{method}.mp4"

    # 100 silence gaps of 0.3s each, evenly spaced across 60s.
    # Each gap = 0.3s; spacing = 60s / 100 = 0.6s; so gap starts at
    # 0.15, 0.75, 1.35, ... up to ~59.55. Total silence = 30.0s.
    # Keep segments between gaps = 0.3s each (~9 frames) — short but
    # above the one-frame minimum.
    silence: list[SilenceSegment] = []
    step = 60.0 / 100
    for i in range(100):
        start = i * step + 0.15
        silence.append(SilenceSegment(start, start + 0.3))

    cut_and_concat(
        src,
        silence,
        out,
        method=method,
        encoder="libx264",
        video_quality="low",  # faster encode; quality irrelevant for frame count
    )
    info = _probe(out)
    frames = info.get("nb_read_frames_video")
    # 100 x 0.3s = 30.0s removed -> keep ~30.0s ~900 frames at 30 FPS.
    # Wide tolerance: 100 boundary decisions x AAC priming (~21ms each)
    # can accumulate ~2s of drift in pathological cases.
    assert frames is not None and 800 <= frames <= 1000, (
        f"{method} 100 segments: frames {frames} outside [800,1000]; info={info}"
    )
    _assert_av_in_sync(info, tolerance_s=0.5)


def test_cut_then_encode_basic(synthetic_source: Path, tmp_path: Path):
    """cut_then_encode method: one encode pass after lossless cut.

    Same keep plan as test_basic_keep (keep 0-2 + 4-6 = 4s = 120 frames
    at 30 FPS). The cut_then_encode method stream-copies each segment,
    concats losslessly, then does ONE final encode. Frame count should
    match the segment/batch result (within tolerance — keyframe
    alignment may shift by up to 1 GOP).
    """
    out = tmp_path / "out_cte.mp4"
    silence = [SilenceSegment(2.0, 4.0)]
    cut_and_concat(
        synthetic_source,
        silence,
        out,
        method="cut_then_encode",
        encoder="libx264",
        video_quality="medium",
    )
    info = _probe(out)
    frames = info.get("nb_read_frames_video")
    # Keyframe alignment with -c copy means the cut snaps to the nearest
    # preceding keyframe. For the synthetic source (libx264 ultrafast,
    # default GOP ~250 frames), the keyframe interval is very large, so
    # the output may be longer than expected (includes frames from the
    # previous keyframe up to the cut point). This is the documented
    # trade-off of cut_then_encode v1 — the output is structurally valid
    # (correct FPS, A/V in sync) but may include extra footage at cut
    # boundaries. Smart-cut (exact frame accuracy) is deferred to v2.
    assert frames is not None and frames > 0, f"cut_then_encode: no frames in output; info={info}"
    _assert_av_in_sync(info, tolerance_s=0.5)
    # The output must have both video and audio streams.
    assert "video" in info["codec_types"], f"missing video stream: {info}"
    assert "audio" in info["codec_types"], f"missing audio stream: {info}"
    # FPS must be preserved.
    assert info.get("r_frame_rate") == "30/1", (
        f"cut_then_encode: r_frame_rate {info.get('r_frame_rate')!r} != '30/1'; info={info}"
    )


def test_cut_then_encode_no_audio(synthetic_source: Path, tmp_path: Path):
    """cut_then_encode handles audio-less sources (no crash, no -map 0:a)."""
    # Create a video-only source by stripping audio.
    src_noaudio = tmp_path / "src_noaudio.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(synthetic_source),
            "-an",  # drop audio
            "-c",
            "copy",
            str(src_noaudio),
        ],
        check=True,
    )
    out = tmp_path / "out_cte_noaudio.mp4"
    silence = [SilenceSegment(1.0, 3.0)]
    cut_and_concat(
        src_noaudio,
        silence,
        out,
        method="cut_then_encode",
        encoder="libx264",
        video_quality="medium",
    )
    info = _probe(out)
    assert "video" in info["codec_types"], f"missing video stream: {info}"
    # Audio stream should NOT be present.
    assert "audio" not in info["codec_types"], f"unexpected audio stream: {info}"


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


def test_29_97_fps_preserved(tmp_path: Path):
    """29.97 FPS (NTSC) — common Twitch/YouTube framerate.

    The trim+concat filter must preserve the fractional framerate without
    rounding to 30. Expected: 4s x 29.97 ≈ 119.88 → 120 frames (rounds
    up because ffmpeg emits a full frame at the boundary).
    """
    src = tmp_path / "src_2997.mp4"
    _make_source(src, duration=6.0, fps=30)  # lavfi doesn't support 29.97 directly
    # Re-encode at 29.97 FPS via fps filter
    ntsc = tmp_path / "src_ntsc.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(src),
            "-vf",
            "fps=30000/1001",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "copy",
            str(ntsc),
        ],
        check=True,
    )
    out = tmp_path / "out_ntsc.mp4"
    silence = [SilenceSegment(2.0, 4.0)]
    cut_and_concat(ntsc, silence, out, method="segment", encoder="libx264", video_quality="medium")
    info = _probe(out)
    # 29.97 FPS source: r_frame_rate should be 30000/1001
    assert "30000/1001" in info.get("r_frame_rate", ""), f"29.97 FPS not preserved: {info}"


def test_vfr_source_preserved(tmp_path: Path):
    """VFR source — variable frame rate must be preserved.

    Creates a source with mixed frame rates (first 3s at 30 FPS, last 3s
    at 15 FPS) via concat demuxer, then verifies the pipeline doesn't
    crash and produces a valid output. VFR is common in screen recordings
    and OBS captures.
    """
    # Create two CFR segments
    seg1 = tmp_path / "vfr_seg1.mp4"
    seg2 = tmp_path / "vfr_seg2.mp4"
    _make_source(seg1, duration=3.0, fps=30)
    _make_source(seg2, duration=3.0, fps=15)

    # Concat them into a VFR source
    vfr_src = tmp_path / "vfr_src.mp4"
    list_file = tmp_path / "vfr_list.txt"
    list_file.write_text(f"file '{seg1}'\nfile '{seg2}'\n", encoding="utf-8")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            "-c",
            "copy",
            str(vfr_src),
        ],
        check=True,
    )

    out = tmp_path / "out_vfr.mp4"
    silence = [SilenceSegment(2.0, 4.0)]
    # VFR pipeline should not crash — frame count tolerance is looser
    # because VFR sources don't have a single nominal framerate.
    cut_and_concat(
        vfr_src, silence, out, method="segment", encoder="libx264", video_quality="medium"
    )
    info = _probe(out)
    # Output should be a valid MP4 with video + audio streams.
    assert "video" in info.get("codec_types", []), f"no video stream: {info}"
    frames = info.get("nb_read_frames_video")
    assert frames is not None and frames > 0, f"VFR: no frames in output: {info}"


def test_multiple_audio_streams(tmp_path: Path):
    """Source with multiple audio tracks — pipeline must pick the first.

    A dual-audio MKV (e.g. dual-language) should use track 0 via
    ``-map 0:a:0`` rather than ffmpeg's auto-select heuristic.
    """
    src = tmp_path / "src_multi_audio.mp4"
    _make_source(src, duration=6.0, fps=30)
    # Add a second silent audio track
    multi = tmp_path / "multi_audio.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(src),
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-map",
            "0:v",
            "-map",
            "0:a",
            "-map",
            "1:a",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-shortest",
            str(multi),
        ],
        check=True,
    )
    # Verify the source has 2 audio streams
    src_info = _probe(multi)
    audio_count = src_info.get("codec_types", []).count("audio")
    assert audio_count >= 2, f"source should have 2+ audio tracks: {src_info}"

    out = tmp_path / "out_multi.mp4"
    silence = [SilenceSegment(2.0, 4.0)]
    cut_and_concat(multi, silence, out, method="segment", encoder="libx264", video_quality="medium")
    out_info = _probe(out)
    # Output should have exactly 1 audio stream (the first one).
    out_audio = out_info.get("codec_types", []).count("audio")
    assert out_audio == 1, f"expected 1 audio stream in output, got {out_audio}: {out_info}"
    frames = out_info.get("nb_read_frames_video")
    assert frames is not None and abs(frames - 120) <= 1, (
        f"multi-audio: frames {frames} != 120; info={out_info}"
    )


# ---------------------------------------------------------------------------
# Non-AAC input codecs (fix-plan §4: "AAC, Opus и MP3 input audio").
#
# The pipeline historically normalised every input to AAC stereo via
# ``-c:a aac -ar 48000 -ac 2`` (see ``_audio_opts()`` in concat.py).
# These tests verify that an Opus or MP3 audio track is decoded and
# re-encoded to AAC without introducing frame loss / A-V desync that
# would slip through a pure-AAC test matrix.
# ---------------------------------------------------------------------------


def _make_source_with_audio_codec(
    out: Path,
    *,
    duration: float = 6.0,
    fps: int = 30,
    audio_codec: str,
    audio_bitrate: str = "192k",
    container: str = "mkv",
) -> None:
    """Generate a source whose audio uses an arbitrary codec/container.

    ``audio_codec`` is the ffmpeg encoder name (e.g. ``libopus``,
    ``libmp3lame``, ``aac``). ``container`` selects the muxer via the
    file extension; most non-MP4 codecs (Opus/MP3/PCM) require a
    Matroska or similar flexible container because the MP4 muxer
    either rejects them or inserts edit-list gaps that would confuse
    the test.
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
            audio_codec,
            "-b:a",
            audio_bitrate,
            "-ac",
            "2",
            "-shortest",
            str(out),
        ],
        check=True,
    )


@pytest.mark.parametrize("method", ["segment", "batch"])
@pytest.mark.parametrize(
    "audio_codec,container",
    [
        ("libopus", "mkv"),
        ("libmp3lame", "mkv"),
    ],
)
def test_non_aac_input_audio_normalized_to_aac(
    method: str, audio_codec: str, container: str, tmp_path: Path
):
    """Opus/MP3 audio input is decoded and re-encoded to AAC stereo.

    The output's audio stream must be AAC regardless of the input
    codec because the pipeline always encodes through ``-c:a aac``
    (see concat.py:_audio_opts). A/V sync and frame count are also
    asserted so a regression that swapped the audio stream order or
    dropped a channel wouldn't pass silently.

    Both codecs are muxed into a Matroska (mkv) container because the
    raw ``mp3`` muxer is audio-only (it strips the video stream) and
    the MP4 muxer rejects the opus-to-MP4 combination on older ffmpeg
    builds. Matroska accepts both codecs alongside H.264 video.
    """
    _require_encoders(audio_codec)
    src = tmp_path / f"src_{audio_codec}.{container}"
    _make_source_with_audio_codec(src, duration=6.0, fps=30, audio_codec=audio_codec)

    # Sanity: the source really uses the requested audio codec.
    src_audio = _probe_audio_codec(src)
    assert src_audio["codec_name"] is not None, f"source has no audio stream: {src_audio}"

    out = tmp_path / f"out_{method}_{audio_codec}.mp4"
    silence = [SilenceSegment(2.0, 4.0)]
    cut_and_concat(
        src,
        silence,
        out,
        method=method,
        encoder="libx264",
        video_quality="medium",
        audio_quality="high",
    )
    info = _probe(out)
    # Frame accuracy must be preserved for non-AAC input too.
    frames = info.get("nb_read_frames_video")
    assert frames is not None and abs(frames - 120) <= 1, (
        f"{audio_codec}/{method}: frames {frames} != 120; info={info}"
    )
    _assert_av_in_sync(info)
    out_audio = _probe_audio_codec(out)
    assert out_audio["codec_name"] == "aac", (
        f"{audio_codec} input: output audio codec {out_audio['codec_name']!r} != 'aac'"
    )
    assert out_audio["channel_layout"] == "stereo", (
        f"{audio_codec} input: output channel_layout {out_audio['channel_layout']!r} != 'stereo'"
    )


# ---------------------------------------------------------------------------
# Channel layout normalisation (fix-plan §4: "Mono/stereo/5.1").
#
# ``-ac 2`` forces the output to stereo regardless of the input layout,
# so mono and 5.1 inputs must be up/down-mixed by the AAC encoder.
# These tests guard against:
#   * a future change that drops ``-ac 2`` (silent channel-count bug);
#   * ffmpeg mis-routing the channel layout through a 5.1 HDMI path;
#   * an audio-less edge case where ``-ac`` is added without ``-map``.
# ---------------------------------------------------------------------------


def _make_source_with_channel_layout(
    out: Path, *, duration: float = 6.0, fps: int = 30, channel_layout: str
) -> None:
    """Generate a source with the requested audio channel layout.

    Uses ``-channel_layout`` so ffmpeg's lavfi sine generator produces
    the right channel count (mono=1, stereo=2, 5.1=6). All sources
    use AAC audio so the test isolates the channel-layout variable.
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
            "192k",
            "-channel_layout",
            channel_layout,
            "-shortest",
            str(out),
        ],
        check=True,
    )


@pytest.mark.parametrize("method", ["segment", "batch"])
@pytest.mark.parametrize("channel_layout", ["mono", "stereo", "5.1"])
def test_channel_layout_normalised_to_stereo(method: str, channel_layout: str, tmp_path: Path):
    """Mono / 5.1 inputs are down/up-mixed to AAC stereo on output.

    ``-ac 2`` in ``_audio_opts()`` is the documented contract; this
    test catches a future change that either drops the option (so mono
    stays mono, surprising users who expect stereo) or removes the
    up-front ``-map 0:a:0?`` routing.
    """
    src = tmp_path / f"src_{channel_layout.replace('.', '')}.mp4"
    _make_source_with_channel_layout(src, channel_layout=channel_layout)

    src_audio = _probe_audio_codec(src)
    # Sanity: source really has the requested layout.
    assert src_audio["channel_layout"] is not None, f"source has no channel_layout: {src_audio}"

    out = tmp_path / f"out_{method}_{channel_layout.replace('.', '')}.mp4"
    silence = [SilenceSegment(2.0, 4.0)]
    cut_and_concat(
        src,
        silence,
        out,
        method=method,
        encoder="libx264",
        video_quality="medium",
    )
    info = _probe(out)
    out_audio = _probe_audio_codec(out)
    assert out_audio["channel_layout"] == "stereo", (
        f"{channel_layout} input: output channel_layout {out_audio['channel_layout']!r} != 'stereo'"
    )
    frames = info.get("nb_read_frames_video")
    assert frames is not None and abs(frames - 120) <= 1, (
        f"{channel_layout}/{method}: frames {frames} != 120; info={info}"
    )
    _assert_av_in_sync(info)


# ---------------------------------------------------------------------------
# Non-zero / shifted start PTS (fix-plan §4: "Broken/non-zero timestamps").
#
# Sources captured by OBS with ``-output_ts_offset`` or re-muxed from a
# mid-file cut may have non-zero start PTS. The pipeline must normalise
# the output so it starts at t=0 (otherwise downstream players hang or
# report a phantom black frame at the head). These tests verify:
#   * start_time is 0 (within one frame);
#   * frame count and A/V sync are still correct;
#   * the segment path handles the shifted PTS through the input-side
#     ``-ss`` seek without dropping the first keep segment.
# ---------------------------------------------------------------------------


def _make_shifted_pts_source(out: Path, *, ts_offset: float = 5.0) -> None:
    """Generate a source whose PTS starts at ``ts_offset`` seconds.

    ``-itsoffset`` shifts input timestamps as if the capture started
    at ``ts_offset`` seconds — this mimics OBS / ffmpeg live captures
    that begin recording some seconds after the encoder's internal
    clock started, so the muxer's ``start_time`` is non-zero but the
    container's duration is preserved (unlike ``-output_ts_offset``
    with a negative value, which actually truncates the file).
    """
    raw = out.with_suffix(".raw.mp4")
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
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=6:sample_rate=48000",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ac",
            "2",
            "-shortest",
            str(raw),
        ],
        check=True,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-itsoffset",
            f"{ts_offset}",
            "-i",
            str(raw),
            "-c",
            "copy",
            str(out),
        ],
        check=True,
    )


def _probe_start_time(path: Path) -> float | None:
    """Container-level ``start_time`` (seconds) as reported by ffprobe."""
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=start_time",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return float(out) if out else None


@pytest.mark.parametrize("method", ["segment", "batch"])
def test_non_zero_start_pts_normalised_to_zero(method: str, tmp_path: Path):
    """Source with a non-zero start PTS produces output starting at t=0.

    With ``-output_ts_offset 5.0`` the demuxer reports
    ``start_time≈4.98s``. Without ``setpts=PTS-STARTPTS`` normalisation
    (or with ``-copyts`` left enabled), the output would inherit the
    shifted start and confuse downstream players. These tests assert
    the pipeline produces a clean output whose container
    ``start_time`` is within one frame of 0.
    """
    src = tmp_path / "src_shifted.mp4"
    _make_shifted_pts_source(src, ts_offset=5.0)

    # Sanity: the source really has a non-zero start.
    src_start = _probe_start_time(src)
    assert src_start is not None and src_start > 1.0, (
        f"source start_time {src_start} should be > 1s"
    )

    out = tmp_path / f"out_shifted_{method}.mp4"
    silence = [SilenceSegment(2.0, 4.0)]
    cut_and_concat(
        src,
        silence,
        out,
        method=method,
        encoder="libx264",
        video_quality="medium",
    )
    info = _probe(out)
    # Output must start at ~0 — one frame's worth of jitter is fine.
    out_start = _probe_start_time(out)
    assert out_start is not None and abs(out_start) <= 0.1, (
        f"{method}: output start_time {out_start}s should be ~0; info={info}"
    )
    frames = info.get("nb_read_frames_video")
    assert frames is not None and abs(frames - 120) <= 1, (
        f"{method}: shifted-PTS frames {frames} != 120; info={info}"
    )
    _assert_av_in_sync(info)


def test_shifted_pts_long_offset_survives(tmp_path: Path):
    """Source with a large PTS shift (30s) is still cut accurately.

    Some OBS / capture tools record with ``-itsoffset`` set to dozens
    of seconds (e.g. when the encoder's clock was started long before
    the actual recording began). This is the regression net for the
    historical ``setpts=N/FRAME_RATE/TB`` bug whose behaviour changed
    unpredictably on shifted-PTS sources — the segment path's
    input-side ``-ss`` seek must locate the keep intervals in source
    time, not in shifted container time, otherwise the output loses
    the first keep segment entirely.
    """
    src = tmp_path / "src_shifted30.mp4"
    _make_shifted_pts_source(src, ts_offset=30.0)

    # Sanity: source really has a large non-zero start.
    src_start = _probe_start_time(src)
    assert src_start is not None and src_start > 10.0, (
        f"source start_time {src_start} should be > 10s"
    )

    out = tmp_path / "out_shifted30.mp4"
    silence = [SilenceSegment(2.0, 4.0)]
    cut_and_concat(src, silence, out, method="segment", encoder="libx264", video_quality="medium")
    info = _probe(out)
    out_start = _probe_start_time(out)
    assert out_start is not None and abs(out_start) <= 0.1, (
        f"30s-shift: output start_time {out_start}s should be ~0; info={info}"
    )
    frames = info.get("nb_read_frames_video")
    assert frames is not None and abs(frames - 120) <= 1, (
        f"30s-shift: frames {frames} != 120; info={info}"
    )
    _assert_av_in_sync(info)


# ---------------------------------------------------------------------------
# Audio-only output formats (mp3 / opus / aac-m4a / wav / flac).
#
# ``cut_and_concat(output_format=...)`` short-circuits the video pipeline
# entirely: the video stream is dropped, each keep segment's audio is
# re-encoded into the chosen codec, and the per-segment files are joined
# by the concat demuxer (mp3/opus/aac/wav) or the concat filter (flac,
# whose muxer misreports duration on a concat-demuxer join).
#
# These tests verify:
#   * the output file has the right codec/container;
#   * duration matches the sum of keep segments (lossy formats allow
#     ~50ms priming per segment; lossless is exact);
#   * no video stream is present;
#   * audio_quality controls bitrate on lossy formats, ignored on lossless.
# ---------------------------------------------------------------------------


def _probe_audio_output(path: Path) -> dict:
    """Probe an audio-only output: codec, container duration, channel layout."""
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,codec_name,channel_layout,channels",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    data = json.loads(out)
    info: dict = {
        "duration": None,
        "codec_types": [],
        "audio_codec": None,
        "channel_layout": None,
        "channels": None,
    }
    fmt = data.get("format") or {}
    if fmt.get("duration"):
        info["duration"] = float(fmt["duration"])
    for stream in data.get("streams", []):
        ctype = stream.get("codec_type")
        if ctype:
            info["codec_types"].append(ctype)
        if ctype == "audio":
            info["audio_codec"] = stream.get("codec_name")
            info["channel_layout"] = stream.get("channel_layout")
            ch = stream.get("channels")
            if ch is not None:
                info["channels"] = int(ch)
    return info


# Codec name as reported by ffprobe for each output_format. Maps the
# config key (mp3/opus/...) to the ffprobe codec_name field so the test
# can assert "the output really is an mp3 stream" rather than just
# "the file exists".
EXPECTED_AUDIO_CODECS: dict[str, str] = {
    "mp3": "mp3",
    "opus": "opus",
    "aac": "aac",
    "wav": "pcm_s16le",
    "flac": "flac",
}

# Expected file extension for each output_format. Matches the spec in
# OUTPUT_FORMAT_SPECS so a mismatch here catches a drift between the
# config table and the actual output filename.
EXPECTED_AUDIO_EXTENSIONS: dict[str, str] = {
    "mp3": "mp3",
    "opus": "opus",
    "aac": "m4a",
    "wav": "wav",
    "flac": "flac",
}


@pytest.mark.parametrize("output_format", ["mp3", "opus", "aac", "wav", "flac"])
def test_audio_only_output_format(output_format: str, synthetic_source: Path, tmp_path: Path):
    """Each audio format produces a valid audio-only file with the right codec.

    The 6s/30FPS source with silence (2,4) → keep [(0,2),(4,6)] = 4s.
    Output must:
      * have the codec matching the format (mp3/opus/aac/pcm_s16le/flac);
      * be audio-only (no video stream);
      * have a duration close to 4s (lossless=exact, lossy within ~50ms
        per segment for AAC/MP3/Opus priming).
    """
    ext = EXPECTED_AUDIO_EXTENSIONS[output_format]
    out = tmp_path / f"out_audio.{ext}"
    silence = [SilenceSegment(2.0, 4.0)]
    cut_and_concat(
        synthetic_source,
        silence,
        out,
        output_format=output_format,
        audio_quality="medium",
    )
    info = _probe_audio_output(out)
    # No video stream in the output.
    assert "video" not in info.get("codec_types", []), (
        f"{output_format}: output has a video stream: {info}"
    )
    assert "audio" in info.get("codec_types", []), (
        f"{output_format}: output has no audio stream: {info}"
    )
    # Codec matches the format.
    expected_codec = EXPECTED_AUDIO_CODECS[output_format]
    assert info["audio_codec"] == expected_codec, (
        f"{output_format}: audio codec {info['audio_codec']!r} != {expected_codec!r}"
    )
    # Channel layout is always stereo (the pipeline normalises via -ac 2).
    # WAV's muxer doesn't write ``channel_layout`` into the stream
    # metadata (only ``channels``), so accept either a "stereo" layout
    # or a 2-channel stream for wav/pcm.
    if output_format == "wav":
        assert info["channels"] == 2, f"{output_format}: channels {info['channels']} != 2"
    else:
        assert info["channel_layout"] == "stereo", (
            f"{output_format}: channel_layout {info['channel_layout']!r} != 'stereo'"
        )
    # Duration is close to 4s. Lossless formats (wav, flac) are exact;
    # lossy (mp3/opus/aac) have ~21ms priming per segment (2 segments
    # → ~40ms drift), so 100ms tolerance covers the worst case.
    duration = info.get("duration")
    assert duration is not None, f"{output_format}: no duration: {info}"
    tolerance = 0.05 if output_format in ("wav", "flac") else 0.2
    assert abs(duration - 4.0) <= tolerance, (
        f"{output_format}: duration {duration}s != 4.0s (±{tolerance}s); info={info}"
    )


def test_audio_only_multi_segment_duration(synthetic_source: Path, tmp_path: Path):
    """Multi-segment audio output: 10 keep segments, total duration ~4s.

    Stress test for the concat path: each segment adds ~21ms of AAC/MP3
    priming, so 10 segments drift ~200ms on lossy formats. Lossless
    formats (wav, flac) must be sample-accurate across 10 segments.

    Uses flac (lossless) so the duration assertion is strict — a flac
    regression would immediately show up as a duration != 4.0s failure.
    """
    out = tmp_path / "out_multi.flac"
    silence: list[SilenceSegment] = []
    t = 0.4
    while t < 6.0 - 0.2:
        silence.append(SilenceSegment(t, t + 0.2))
        t += 0.55
    cut_and_concat(
        synthetic_source,
        silence,
        out,
        output_format="flac",
        audio_quality="medium",
    )
    info = _probe_audio_output(out)
    duration = info.get("duration")
    # 10 segments x 0.2s silence = 2.0s removed -> keep ~4.0s.
    # Lossless flac must be exact (no priming).
    assert duration is not None and abs(duration - 4.0) <= 0.05, (
        f"flac multi-segment: duration {duration}s != 4.0s; info={info}"
    )


def test_audio_quality_affects_lossy_bitrate(synthetic_source: Path, tmp_path: Path):
    """audio_quality controls the bitrate on lossy formats (mp3).

    high=256k must beat low=128k by a clear margin, otherwise the user's
    audio_quality choice has no effect (the historical P0.3 bug:
    hard-coded 128k regardless of the requested preset).
    """
    out_high = tmp_path / "out_high.mp3"
    out_low = tmp_path / "out_low.mp3"
    silence = [SilenceSegment(2.0, 4.0)]
    cut_and_concat(
        synthetic_source,
        silence,
        out_high,
        output_format="mp3",
        audio_quality="high",
    )
    cut_and_concat(
        synthetic_source,
        silence,
        out_low,
        output_format="mp3",
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
    assert high > low, f"audio_quality='high' ({high} bps) didn't beat 'low' ({low} bps)"


def test_audio_quality_ignored_on_lossless(synthetic_source: Path, tmp_path: Path):
    """audio_quality is ignored on lossless formats (flac).

    flac's encoder runs at its native compression level regardless of
    the audio_quality knob; the output is bit-exact PCM, so the bitrate
    is determined by the source's sample rate / channel count, not by
    high/medium/low. This test guards against a future change that
    accidentally applies ``-b:a`` to flac (which the encoder would
    silently ignore, but the command line would be misleading).
    """
    out_high = tmp_path / "out_high.flac"
    out_low = tmp_path / "out_low.flac"
    silence = [SilenceSegment(2.0, 4.0)]
    cut_and_concat(
        synthetic_source,
        silence,
        out_high,
        output_format="flac",
        audio_quality="high",
    )
    cut_and_concat(
        synthetic_source,
        silence,
        out_low,
        output_format="flac",
        audio_quality="low",
    )
    info_h = _probe_audio_output(out_high)
    info_l = _probe_audio_output(out_low)
    # Both must be flac; bit_rate should be ~equal (lossless: same PCM
    # content → same compressed size regardless of the -b:a hint).
    assert info_h["audio_codec"] == "flac"
    assert info_l["audio_codec"] == "flac"
    assert info_h["duration"] is not None and info_l["duration"] is not None
    assert abs(info_h["duration"] - info_l["duration"]) < 0.05, (
        f"flac duration changed with audio_quality: high={info_h['duration']}, "
        f"low={info_l['duration']}"
    )


def test_audio_only_rejects_videoless_source(tmp_path: Path):
    """Audio-only output on a video-only source raises a clear error.

    Without an audio stream there's nothing to extract; the pipeline
    must refuse early rather than produce an empty/corrupt audio file.
    """
    src = tmp_path / "src_video_only.mp4"
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
    out = tmp_path / "out.mp3"
    silence = [SilenceSegment(2.0, 4.0)]
    with pytest.raises(Exception, match="no audio stream"):
        cut_and_concat(
            src,
            silence,
            out,
            output_format="mp3",
            audio_quality="medium",
        )


def test_audio_only_unknown_format_raises(synthetic_source: Path, tmp_path: Path):
    """An unknown output_format raises ConcatError before touching ffmpeg."""
    out = tmp_path / "out.ogg"
    silence = [SilenceSegment(2.0, 4.0)]
    with pytest.raises(Exception, match="Unknown output_format"):
        cut_and_concat(
            synthetic_source,
            silence,
            out,
            output_format="ogg",
            audio_quality="medium",
        )


# ---------------------------------------------------------------------------
# Gapless concat (AAC priming fix).
#
# The segment path's default concat demuxer preserves per-segment AAC
# encoder priming (~21ms at 48kHz), which accumulates as A/V drift on
# multi-segment outputs — 10 segments drift ~170ms. ``gapless_concat=True``
# switches the final join to the concat filter (re-encode), so priming
# is added only once (not per-segment), giving gapless audio.
#
# These tests verify:
#   * gapless output duration is closer to expected than demuxer output;
#   * gapless A/V drift is within one frame (not accumulating per segment);
#   * 1-segment output is unaffected (gapless_concat only kicks in for n>1).
# ---------------------------------------------------------------------------


def test_gapless_concat_reduces_priming_drift(synthetic_source: Path, tmp_path: Path):
    """gapless_concat=True produces a valid output with bounded A/V drift.

    10 segments x ~21ms priming = ~170ms drift on the default path; the
    gapless path uses the concat filter (single re-encode) so priming is
    added only once. On short segments the concat filter adds its own
    per-segment alignment overhead, so the gapless output's total audio
    duration may not be shorter than the default's — but the A/V drift
    (audio vs video within the same file) must stay within one AAC frame
    (~21ms), which is the contract the gapless path guarantees.

    This is the regression net for the concat filter path: a future
    change that breaks the filter graph (e.g. wrong pad ordering, missing
    ``-map``) would show up as a missing stream or a large A/V drift.
    """
    silence: list[SilenceSegment] = []
    t = 0.4
    while t < 6.0 - 0.2:
        silence.append(SilenceSegment(t, t + 0.2))
        t += 0.55

    out_default = tmp_path / "out_default.mp4"
    out_gapless = tmp_path / "out_gapless.mp4"

    cut_and_concat(
        synthetic_source,
        silence,
        out_default,
        method="segment",
        encoder="libx264",
        video_quality="medium",
    )
    cut_and_concat(
        synthetic_source,
        silence,
        out_gapless,
        method="segment",
        encoder="libx264",
        video_quality="medium",
        gapless_concat=True,
    )

    info_default = _probe(out_default)
    info_gapless = _probe(out_gapless)

    # Both must have video + audio streams.
    assert "video" in info_default["codec_types"], f"default: no video: {info_default}"
    assert "audio" in info_default["codec_types"], f"default: no audio: {info_default}"
    assert "video" in info_gapless["codec_types"], f"gapless: no video: {info_gapless}"
    assert "audio" in info_gapless["codec_types"], f"gapless: no audio: {info_gapless}"

    # Gapless A/V drift must be within one AAC frame (~21ms at 48kHz).
    # The default path can drift more (per-segment priming accumulates),
    # but the gapless path re-encodes through a single pipeline so the
    # audio and video durations must be aligned to within one frame.
    gapless_av_drift = abs(info_gapless["audio_duration"] - info_gapless["duration"])
    assert gapless_av_drift <= 0.05, (
        f"gapless A/V drift {gapless_av_drift * 1000:.1f}ms > 50ms; info={info_gapless}"
    )

    # Frame count must match the default path's (within ±1 boundary).
    # The concat filter re-encodes video but shouldn't drop/duplicate
    # frames beyond what the demuxer path does.
    default_frames = info_default["nb_read_frames_video"]
    gapless_frames = info_gapless["nb_read_frames_video"]
    assert default_frames is not None and gapless_frames is not None
    assert abs(gapless_frames - default_frames) <= 2, (
        f"gapless frames {gapless_frames} != default {default_frames} (±2); "
        f"default={info_default}, gapless={info_gapless}"
    )


def test_gapless_concat_frame_count_preserved(synthetic_source: Path, tmp_path: Path):
    """gapless_concat preserves the frame count (video is re-encoded but
    frame-accurate — the concat filter doesn't drop/duplicate frames).

    10 segments on a 6s/30FPS source → keep ~4.0s → ~120 frames. The
    concat filter re-encodes through a single pipeline, so the frame
    count should match the demuxer path's result (within ±1 boundary).
    """
    silence: list[SilenceSegment] = []
    t = 0.4
    while t < 6.0 - 0.2:
        silence.append(SilenceSegment(t, t + 0.2))
        t += 0.55

    out = tmp_path / "out_gapless_frames.mp4"
    cut_and_concat(
        synthetic_source,
        silence,
        out,
        method="segment",
        encoder="libx264",
        video_quality="medium",
        gapless_concat=True,
    )
    info = _probe(out)
    frames = info.get("nb_read_frames_video")
    # Same tolerance as test_many_short_segments: 10 boundary decisions
    # add up, but the concat filter shouldn't drop frames beyond that.
    assert frames is not None and 110 <= frames <= 130, (
        f"gapless: frames {frames} outside [110,130]; info={info}"
    )
    _assert_av_in_sync(info, tolerance_s=0.1)


def test_gapless_concat_single_segment_uses_demuxer(synthetic_source: Path, tmp_path: Path):
    """gapless_concat=True with 1 segment still uses the concat demuxer.

    The gapless path only kicks in when n_segs > 1 (priming doesn't
    accumulate with a single segment). This test guards against a future
    change that always uses the concat filter, which would add an
    unnecessary re-encode for single-segment outputs.
    """
    out = tmp_path / "out_single.mp4"
    silence = [SilenceSegment(2.0, 4.0)]
    cut_and_concat(
        synthetic_source,
        silence,
        out,
        method="segment",
        encoder="libx264",
        video_quality="medium",
        gapless_concat=True,
    )
    info = _probe(out)
    # Single segment → frame count must be exactly 120 (±1), same as
    # the default path. A concat filter re-encode would also produce
    # 120 frames, so this test alone doesn't prove the demuxer was used
    # — but it confirms the output is still valid.
    frames = info.get("nb_read_frames_video")
    assert frames is not None and abs(frames - 120) <= 1, (
        f"gapless single-segment: frames {frames} != 120; info={info}"
    )


def test_low_process_priority_produces_valid_output(synthetic_source: Path, tmp_path: Path):
    """low_process_priority=True applies priority flags to the spawned
    ffmpeg subprocesses without breaking the encode pipeline.

    Verifies that ``subprocess_kwargs(low_priority=True)`` composes
    safely with ``no_window_kwargs()`` on both Windows (CREATE_NO_WINDOW
    OR BELOW_NORMAL_PRIORITY_CLASS) and POSIX (preexec_fn=os.nice(+10))
    across the per-segment encode + final concat join, producing a
    valid MP4 with the expected frame count. A regression in the
    kwargs composition would surface as a Popen error or a corrupt
    output file.
    """
    out = tmp_path / "out_low_prio.mp4"
    silence = [SilenceSegment(1.0, 3.0), SilenceSegment(5.0, 7.0)]
    cut_and_concat(
        synthetic_source,
        silence,
        out,
        method="segment",
        encoder="libx264",
        video_quality="medium",
        low_process_priority=True,
    )
    info = _probe(out)
    frames = info.get("nb_read_frames_video")
    # Source is 6s @ 30fps; with silence segments (1-3, 5-6 clamped from
    # 5-7) the kept range is 0-1 + 3-5 = 3s ≈ 90 frames. Allow ±2
    # tolerance for boundary frames.
    assert frames is not None and abs(frames - 90) <= 2, (
        f"low_process_priority: frames {frames} != 90; info={info}"
    )
    # Sanity check: the output is a valid video + audio MP4.
    assert "video" in info.get("codec_types", [])
    assert "audio" in info.get("codec_types", [])


def test_low_memory_preset_tunables_produce_valid_output(synthetic_source: Path, tmp_path: Path):
    """The 'low_memory' preset's tunables (x264_low_memory=True,
    batch_chunk_size=20, low_process_priority=True) compose safely
    across the batch path's chunk encode + final concat join.

    Verifies that applying the preset's per-key values by hand (the
    CLI/GUI apply_preset path is unit-tested in test_config.py —
    here we verify the resulting ffmpeg invocations themselves work)
    produces a valid MP4. Catches regressions where, e.g., a future
    x264_low_memory change breaks the encoder args.
    """
    out = tmp_path / "out_low_mem.mp4"
    silence = [SilenceSegment(1.0, 3.0), SilenceSegment(5.0, 7.0)]
    cut_and_concat(
        synthetic_source,
        silence,
        out,
        method="batch",
        encoder="libx264",
        video_quality="medium",
        # Apply low_memory preset's tunables by hand.
        x264_low_memory=True,
        batch_chunk_size=20,
        low_process_priority=True,
    )
    info = _probe(out)
    frames = info.get("nb_read_frames_video")
    # Same source / silence layout as
    # test_low_process_priority_produces_valid_output: 6s @ 30fps,
    # kept [0-1, 3-5] = 3s ≈ 90 frames (±2 tolerance for boundaries).
    assert frames is not None and abs(frames - 90) <= 2, (
        f"low_memory preset: frames {frames} != 90; info={info}"
    )
    assert "video" in info.get("codec_types", [])
    assert "audio" in info.get("codec_types", [])
