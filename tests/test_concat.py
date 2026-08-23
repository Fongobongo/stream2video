from __future__ import annotations

import io
import shutil
import subprocess
from pathlib import Path
from subprocess import TimeoutExpired
from unittest.mock import patch

import pytest

from stream2video.concat import (
    _run_final_concat,
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
        # The direct-call path publishes a ``.s2v_partial`` sibling via
        # ``os.replace`` (audit round 32 P0); simulate what the real
        # encoder does — write the file it was asked to write.
        Path(args[2]).write_bytes(b"output")

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


class TestDirectCallAtomicPublish:
    """Audit round 32 P0: a DIRECT ``cut_and_concat`` caller (no
    pipeline lock) takes the output lock but previously wrote straight
    into the user's output path — a failed rerun destroyed the previous
    good result. The encode now runs into a ``.s2v_partial`` sibling
    and publishes with ONE ``os.replace``."""

    def _run(self, video: Path, output: Path, encode_side_effect):
        with (
            patch("stream2video.concat.generate_keep_segments", return_value=[(0.0, 1.0)]),
            patch("stream2video.concat.get_video_encoder", return_value=("libx264", [])),
            patch("stream2video.concat.has_audio_stream", return_value=True),
            patch("stream2video.concat._run_with_fallback", side_effect=encode_side_effect),
        ):
            return cut_and_concat(video, [], output)

    def test_success_publishes_via_replace_without_partial_residue(self, tmp_path: Path):
        video = tmp_path / "src.mp4"
        video.write_bytes(b"source")
        output = tmp_path / "out.mp4"

        def encode(video_path, keep, target, *args, **kwargs):
            # The body must see the PARTIAL path, never the stable one.
            assert target.name == ".out.mp4.s2v_partial.mp4"
            Path(target).write_bytes(b"result")

        result = self._run(video, output, encode)
        assert result == output
        assert output.read_bytes() == b"result"
        assert not list(tmp_path.glob(".*s2v_partial*")), "partial must be gone after publish"

    def test_failure_keeps_previous_output_and_removes_partial(self, tmp_path: Path):
        from stream2video.concat import ConcatError

        video = tmp_path / "src.mp4"
        video.write_bytes(b"source")
        output = tmp_path / "out.mp4"
        output.write_bytes(b"previous good result")

        def failing_encode(*args, **kwargs):
            # Simulate a mid-write crash: partial exists, then error.
            Path(args[2]).write_bytes(b"partial garbage")
            raise ConcatError("encode fail")

        with pytest.raises(ConcatError, match="encode fail"):
            self._run(video, output, failing_encode)
        assert output.read_bytes() == b"previous good result", (
            "a failed rerun must not touch the previous good output"
        )
        assert not list(tmp_path.glob(".*s2v_partial*")), "partial must be unlinked on failure"

    def test_controller_lock_path_publishes_atomically_too(self, tmp_path: Path):
        """Audit round 33 P0: the atomic publish is NOT tied to who owns
        the lock. A caller passing a pre-acquired ``lock=`` (the
        pipeline's project lock) still encodes into the partial and the
        result still reaches the given target via ``os.replace`` — one
        contract for every caller."""
        from stream2video.concat import acquire_output_lock, release_output_lock

        video = tmp_path / "src.mp4"
        video.write_bytes(b"source")
        output = tmp_path / "out.mp4"
        output.write_bytes(b"previous good result")
        lock = acquire_output_lock(output)
        targets: list[Path] = []

        def encode(video_path, keep, target, *args, **kwargs):
            targets.append(Path(target))
            Path(target).write_bytes(b"staged")

        try:
            with (
                patch("stream2video.concat.generate_keep_segments", return_value=[(0.0, 1.0)]),
                patch("stream2video.concat.get_video_encoder", return_value=("libx264", [])),
                patch("stream2video.concat.has_audio_stream", return_value=True),
                patch("stream2video.concat._run_with_fallback", side_effect=encode),
            ):
                result = cut_and_concat(video, [], output, lock=lock)
        finally:
            release_output_lock(lock)
        assert targets[0].name == ".out.mp4.s2v_partial.mp4", (
            "lock-holder callers must still encode into the partial, not the stable path"
        )
        assert result == output and output.read_bytes() == b"staged"
        assert not list(tmp_path.glob(".*s2v_partial*"))

    def test_controller_lock_path_failure_preserves_previous_output(self, tmp_path: Path):
        """Same lock= path, failing encode: the previous version of the
        target must survive (the round-32 hole this locks shut)."""
        from stream2video.concat import ConcatError, acquire_output_lock, release_output_lock

        video = tmp_path / "src.mp4"
        video.write_bytes(b"source")
        output = tmp_path / "out.mp4"
        output.write_bytes(b"previous good result")
        lock = acquire_output_lock(output)

        def failing_encode(*args, **kwargs):
            Path(args[2]).write_bytes(b"partial garbage")
            raise ConcatError("encode fail")

        try:
            with (
                patch("stream2video.concat.generate_keep_segments", return_value=[(0.0, 1.0)]),
                patch("stream2video.concat.get_video_encoder", return_value=("libx264", [])),
                patch("stream2video.concat.has_audio_stream", return_value=True),
                patch("stream2video.concat._run_with_fallback", side_effect=failing_encode),
                pytest.raises(ConcatError, match="encode fail"),
            ):
                cut_and_concat(video, [], output, lock=lock)
        finally:
            release_output_lock(lock)
        assert output.read_bytes() == b"previous good result"
        assert not list(tmp_path.glob(".*s2v_partial*"))


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


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not on PATH",
)
def test_gapless_tree_reuse_rejects_corrupted_intermediate_body(tmp_path: Path, monkeypatch):
    """Audit round 35 P1: the tree reuse gate validated only headers
    (two codec probes) — a zeroed-middle MKV passed both while its full
    audio decode failed (reproduced live), and the corrupted audio
    track was reused into the next tree level. The reuse gate is now
    the unified ``_media_is_valid`` (codec probe + WHOLE-STREAM decode
    of every required stream), so an intermediate whose audio body
    fails the decode must be re-encoded, never reused."""
    from stream2video import concat as concat_mod

    reencoded: list[str] = []

    def fake_one_pass(part_paths, output_path, vcodec, vcodec_opts, **kwargs):
        reencoded.append(Path(output_path).name)

    def fake_decode(path, stream_type="v", **kwargs):
        # The intermediate's audio BODY is damaged: the real codec probe
        # passes, the whole-stream audio decode fails — the audit's
        # zeroed-middle counterexample.
        return (stream_type != "a" or not Path(path).name.startswith("L0_")), None

    monkeypatch.setattr(concat_mod, "_concat_filter_one_pass", fake_one_pass)
    monkeypatch.setattr(concat_mod, "_GAPLESS_MAX_INPUTS_PER_CALL", 2)

    parts = [tmp_path / f"seg_{i:06d}.mp4" for i in range(5)]
    for p in parts:
        p.write_bytes(b"\x00" * 2048)
    out = tmp_path / "out.mp4"

    # A REAL finished-looking intermediate: header probes pass (real
    # ffmpeg), the (patched) full audio decode reports the body bad.
    tree_dir = out.parent / f"_gapless_tree_{out.stem}"
    tree_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=0.5:size=320x240:rate=30",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.5:sample_rate=48000",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-shortest",
            str(tree_dir / "L0_00000.mkv"),
        ],
        check=True,
    )

    with patch("stream2video.concat.probing._ffmpeg_decode_timing", side_effect=fake_decode):
        concat_mod._run_gapless_segment_concat(
            out,
            parts,
            "libx264",
            [],
            audio_quality="medium",
            total_duration=4.5,
        )

    assert "L0_00000.mkv" in reencoded, "the corrupted intermediate must be re-encoded, not reused"


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

        def _run(args, **kwargs):
            if exc is not None:
                raise exc("ffprobe", 30)
            # The probe helper returns (returncode, stdout) after the
            # round-33 cancellable rewrite.
            return result.returncode, result.stdout

        with (
            patch("stream2video.utils.ffprobe_path", return_value="ffprobe"),
            patch("stream2video.concat.probing._run_ffprobe", side_effect=_run),
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

        # 0.15 s off a 10 s part: inside the bounded tolerance
        # min(1.0, max(0.10, 10.0 * 0.02)) = 0.2 s (audit round 35 P1).
        result = CompletedProcess(args=[], returncode=0, stdout="10.15\n", stderr="")
        assert self._probe(tmp_path, result) is True

    def test_mismatched_duration_is_rejected(self, tmp_path: Path):
        from subprocess import CompletedProcess

        result = CompletedProcess(args=[], returncode=0, stdout="4.0\n", stderr="")
        assert self._probe(tmp_path, result) is False


class TestFfprobeDurationOkBoundedSlack:
    """Audit round 35 P1: the resume-part duration gate used a FLAT
    1 s slack — larger than the short speech islands it guarded. A
    0.8 s part holding only 0.1 s passed with 87.5 % of the fragment
    lost (reproduced on a real MP4). The tolerance is now bounded:
    ``min(slack, max(100 ms floor, 2 % of the expected length))``.
    """

    def _probe(self, tmp_path: Path, actual: str, expected: float, slack: float = 1.0):
        from stream2video.concat.probing import _ffprobe_duration_ok

        with (
            patch("stream2video.utils.ffprobe_path", return_value="ffprobe"),
            patch("stream2video.concat.probing._run_ffprobe", return_value=(0, actual)),
        ):
            return _ffprobe_duration_ok(
                tmp_path / "part.mp4", expected_seconds=expected, slack=slack
            )

    def test_short_part_with_fraction_of_body_rejected(self, tmp_path: Path):
        """The audit counterexample: expected 0.8 s, actual 0.1 s — the
        flat 1 s slack accepted it, the bounded 0.1 s allowance must
        reject it."""
        assert self._probe(tmp_path, "0.1\n", 0.8) is False

    def test_short_part_within_floor_accepted(self, tmp_path: Path):
        """0.75 s vs 0.8 s expected: 50 ms off, inside the 100 ms floor."""
        assert self._probe(tmp_path, "0.75\n", 0.8) is True

    def test_long_part_keeps_caller_slack(self, tmp_path: Path):
        """300 s part 0.8 s off: the caller's 1 s slack still applies
        (2 % of 300 s would be 6 s — the slack ceiling binds)."""
        assert self._probe(tmp_path, "299.2\n", 300.0) is True

    def test_long_part_beyond_slack_rejected(self, tmp_path: Path):
        """300 s part 1.2 s off: beyond the 1 s slack — rejected."""
        assert self._probe(tmp_path, "298.8\n", 300.0) is False

    def test_slack_ceiling_can_be_raised_by_caller(self, tmp_path: Path):
        """The caller's slack stays the ceiling: slack=2 allows the
        1.2 s offset the default 1 s rejected."""
        assert self._probe(tmp_path, "298.8\n", 300.0, slack=2.0) is True


class TestFfmpegDecodeTiming:
    """The whole-stream decode gate (audit round 29 P3/P4 / 30 P7/P8 /
    31 P1) — the only check that reads every packet, used by the
    fresh-download publish, every resume-reuse decision and the
    final-output validation. Runs through a cancellable Popen with a
    ring-bounded stderr drain, ``-xerror``, and a caller-supplied
    timeout — and tears the child down (kill + bounded reap + pipe
    close + drain join) on EVERY exit path. Since audit round 37 P1 it
    also measures the decoded length via ``-progress pipe:1``.
    """

    class _FakeProc:
        def __init__(self, rc: int, stderr_text: str, waits_before_exit: int = 0):
            import io

            self.returncode: int | None = None
            self._rc = rc
            self._waits = waits_before_exit
            self._killed = False
            self.stderr = io.StringIO(stderr_text)
            self.stdout = io.StringIO("")

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            if self._waits > 0:
                self._waits -= 1
                raise TimeoutExpired("ffmpeg", timeout or 0)
            if self.returncode is None:
                self.returncode = self._rc
            return self.returncode

        def kill(self):
            self._killed = True
            if self.returncode is None:
                self.returncode = 1

    def _probe(self, proc, *, cancel_callback=None, timeout=60.0):
        from stream2video.concat.probing import _ffmpeg_decode_timing

        with (
            patch("stream2video.concat.probing.ffmpeg_path", return_value="ffmpeg"),
            patch("stream2video.concat.probing.popen_with_retry", return_value=proc),
        ):
            ok, _ = _ffmpeg_decode_timing(
                Path("part.mp4"), "v", timeout=timeout, cancel_callback=cancel_callback
            )
            return ok

    def test_clean_decode_accepted(self):
        assert self._probe(self._FakeProc(0, "")) is True

    def test_rc_nonzero_rejected(self):
        assert self._probe(self._FakeProc(1, "")) is False

    def test_truncation_markers_rejected(self):
        for marker in ("partial file", "packet corrupt", "moov atom not found"):
            assert self._probe(self._FakeProc(0, f"[mov] {marker}")) is False, marker

    def test_cancel_kills_decode(self):
        import pytest

        from stream2video.concat.errors import CancelledError

        proc = self._FakeProc(0, "", waits_before_exit=1000)
        with pytest.raises(CancelledError):
            self._probe(proc, cancel_callback=lambda: True)
        assert proc._killed, "cancel must kill ffmpeg instead of waiting"
        assert proc.poll() is not None, "the killed child must be reaped before propagating"

    def test_timeout_kills_decode(self):
        from subprocess import TimeoutExpired

        import pytest

        # waits_before_exit must be effectively unbounded: the deadline
        # check (time.monotonic >= start + timeout) is the ONLY thing that
        # must fire. If the fake proc could "finish" within the 0.01 s
        # window, the loop would break on proc.wait() and the test would
        # flakily return True. 10M waits >> any iteration count possible
        # in 10 ms, so the deadline always wins.
        proc = self._FakeProc(0, "", waits_before_exit=10_000_000)
        with pytest.raises(TimeoutExpired):
            self._probe(proc, timeout=0.01)
        assert proc._killed
        assert proc.poll() is not None, "the timed-out child must be reaped before propagating"

    def test_cancel_callback_exception_still_kills_child(self):
        """Audit round 31 P1-2: if the cancel CALLBACK itself raises,
        the unconditional finally must still kill + reap the child —
        the pipeline errors out, but no ffmpeg survives the failure."""
        import pytest

        from stream2video.concat import probing

        proc = self._FakeProc(0, "", waits_before_exit=1000)

        def boom():
            raise RuntimeError("callback exploded")

        with (
            patch("stream2video.concat.probing.ffmpeg_path", return_value="ffmpeg"),
            patch("stream2video.concat.probing.popen_with_retry", return_value=proc),
            pytest.raises(RuntimeError, match="callback exploded"),
        ):
            probing._ffmpeg_decode_timing(Path("part.mp4"), "v", timeout=60, cancel_callback=boom)
        assert proc._killed, "a raising callback must not leave the child running"
        assert proc.poll() is not None
        assert proc.stderr.closed, "the stderr pipe must be closed on every exit path"

    def test_resource_policy_is_passed_to_spawn(self):
        """Audit round 31 P1-3: the validation decode must honour the
        caller's low_process_priority / rlimit_as_mb — the main encode
        respects them, so the decode before/after it must not run with
        a different resource contract."""
        import stream2video.utils as utils
        from stream2video.concat import probing

        proc = self._FakeProc(0, "")
        seen: list[tuple[bool, int]] = []

        def spy(low_priority=False, rlimit_as_mb=0):
            seen.append((low_priority, rlimit_as_mb))
            return utils.no_window_kwargs()

        with (
            patch("stream2video.concat.probing.ffmpeg_path", return_value="ffmpeg"),
            patch("stream2video.concat.probing.popen_with_retry", return_value=proc),
            patch("stream2video.concat.probing.subprocess_kwargs", side_effect=spy),
        ):
            probing._ffmpeg_decode_timing(
                Path("part.mp4"),
                "v",
                low_process_priority=True,
                rlimit_as_mb=1024,
            )
        assert seen == [(True, 1024)]

    def test_process_is_registered_and_unregistered(self):
        """Audit round 31 P1-3: the decode child joins the shared
        process registry (so the shutdown kill covers it) and leaves it
        on every exit path."""
        from stream2video import utils
        from stream2video.concat import probing

        proc = self._FakeProc(0, "")
        registered_during: list[bool] = []

        def wait_then_record(timeout=None):
            registered_during.append(proc in utils._proc_registry.get("default", []))
            proc.returncode = 0
            return 0

        proc.wait = wait_then_record
        with (
            patch("stream2video.concat.probing.ffmpeg_path", return_value="ffmpeg"),
            patch("stream2video.concat.probing.popen_with_retry", return_value=proc),
        ):
            probing._ffmpeg_decode_timing(Path("part.mp4"), "v")
        assert registered_during == [True], "the child must be registered while running"
        assert proc not in utils._proc_registry.get("default", [])

    def test_spawn_fault_propagates(self):
        import pytest

        with (
            patch("stream2video.concat.probing.ffmpeg_path", return_value="ffmpeg"),
            patch(
                "stream2video.concat.probing.popen_with_retry",
                side_effect=FileNotFoundError,
            ),
            pytest.raises(FileNotFoundError),
        ):
            from stream2video.concat.probing import _ffmpeg_decode_timing

            _ffmpeg_decode_timing(Path("part.mp4"), "v")


class TestMediaIsValid:
    """The unified stream-set gate (audit round 31 P1-4) shared by the
    fresh-download publish, the final staged output and every resume
    part: per required stream a codec probe + full decode, and a v/a
    end comparison when both are required (audit round 36 P1 — track
    ENDS, start+duration, so MPEG-PS-style shifted starts stay valid;
    containers with N/A stream durations fall back to the container
    duration, audit round 36 P0)."""

    def _call(
        self,
        *,
        require_video,
        require_audio,
        codec=True,
        decode=True,
        durations=(5.0, 5.0),
        starts=(0.0, 0.0),
        measured=None,
    ):
        from stream2video.concat import probing

        def fake_decode_timing(path, stream_type="v", **kw):
            idx = 0 if stream_type == "v" else 1
            if not decode:
                return False, None
            return True, (measured or durations)[idx]

        def fake_timing(path, t, **kw):
            idx = 0 if t == "v" else 1
            return starts[idx], durations[idx]

        with (
            patch("stream2video.concat.probing._ffprobe_is_valid_media", return_value=codec),
            patch(
                "stream2video.concat.probing._ffmpeg_decode_timing",
                side_effect=fake_decode_timing,
            ),
            patch("stream2video.concat.probing._ffprobe_stream_timing", side_effect=fake_timing),
        ):
            return probing._media_is_valid(
                Path("out.mp4"),
                require_video=require_video,
                require_audio=require_audio,
            )

    def test_video_only_source_passes_without_audio(self):
        """require_audio=False must never probe the audio stream."""
        from stream2video.concat import probing

        probed: list[str] = []

        def record(path, stream_type="v", **kw):
            probed.append(stream_type)
            return True

        with (
            patch("stream2video.concat.probing._ffprobe_is_valid_media", side_effect=record),
            patch(
                "stream2video.concat.probing._ffmpeg_decode_timing",
                side_effect=lambda p, t="v", **kw: (True, 5.0),
            ),
            patch("stream2video.concat.probing._ffprobe_stream_timing", return_value=(0.0, 5.0)),
        ):
            assert (
                probing._media_is_valid(Path("x.mp4"), require_video=True, require_audio=False)
                is True
            )
        assert "a" not in probed, "video-only validation must not touch the audio stream"

    def test_audio_only_output_requires_only_audio(self):
        from stream2video.concat import probing

        probed: list[str] = []

        def record(path, stream_type="v", **kw):
            probed.append(stream_type)
            return True

        with (
            patch("stream2video.concat.probing._ffprobe_is_valid_media", side_effect=record),
            patch(
                "stream2video.concat.probing._ffmpeg_decode_timing",
                side_effect=lambda p, t="v", **kw: (True, 5.0),
            ),
            patch("stream2video.concat.probing._ffprobe_stream_timing", return_value=(0.0, 5.0)),
        ):
            assert (
                probing._media_is_valid(Path("x.mp4"), require_video=False, require_audio=True)
                is True
            )
        assert "v" not in probed

    def test_truncated_audio_body_rejected(self):
        """12 s video + 2 s audio (audit counterexample): both streams
        exist and decode, but the duration mismatch must fail the gate."""
        assert self._call(require_video=True, require_audio=True, durations=(12.0, 2.0)) is False

    def test_matching_durations_pass(self):
        assert self._call(require_video=True, require_audio=True, durations=(12.0, 11.8)) is True

    def test_short_truncated_audio_rejected(self):
        """Audit round 33 P1 counterexample: a 1.8 s video carrying a
        0.1 s audio track lost nearly the whole audio body — the fixed
        2 s tolerance accepted it. The bounded tolerance (min 0.1 s,
        max 2 s, 2 % in between) must reject it."""
        assert self._call(require_video=True, require_audio=True, durations=(1.8, 0.1)) is False

    def test_short_healthy_file_passes_within_floor(self):
        """Very short clips have genuine codec-level drift (AAC priming,
        frame rounding) — the 150 ms floor keeps a healthy 1 s file with
        a ~50 ms stream offset valid."""
        assert self._call(require_video=True, require_audio=True, durations=(1.0, 0.95)) is True

    def test_long_file_allows_capped_drift(self):
        """A six-hour encode's legitimate mux/flush drift stays capped at
        2 s (the ceiling), not the uncapped 2 % (~13 min)."""
        six_hours = 6 * 3600.0
        assert (
            self._call(
                require_video=True, require_audio=True, durations=(six_hours, six_hours - 1.9)
            )
            is True
        )
        assert (
            self._call(
                require_video=True, require_audio=True, durations=(six_hours, six_hours - 2.1)
            )
            is False
        )

    def test_codec_probe_failure_rejects(self):
        assert self._call(require_video=True, require_audio=False, codec=False) is False

    def test_decode_failure_rejects(self):
        assert self._call(require_video=True, require_audio=False, decode=False) is False

    def test_unknown_duration_is_fail_closed_when_both_required(self):
        """Audit round 32 P1 + round 37 P1: when both streams are
        required, an unreadable duration on EITHER side is fail-closed
        UNLESS the DECODED lengths (measured during the full decode)
        prove the tracks agree — a container duration cannot do that
        (a 12 s video with a 0.1 s audio track decodes cleanly and has
        a positive container duration; only the measured lengths
        expose the truncation). Unmeasurable or empty measured lengths
        stay fail-closed."""
        assert self._call(require_video=True, require_audio=True, durations=(12.0, None)) is False
        assert self._call(require_video=True, require_audio=True, durations=(None, 12.0)) is False
        assert (
            self._call(
                require_video=True,
                require_audio=True,
                durations=(12.0, None),
                measured=(12.0, 12.0),
            )
            is True
        )
        assert (
            self._call(
                require_video=True, require_audio=True, durations=(None, None), measured=(2.0, 2.0)
            )
            is True
        )
        assert (
            self._call(
                require_video=True, require_audio=True, durations=(None, None), measured=(2.0, 0.5)
            )
            is False
        )
        assert (
            self._call(
                require_video=True,
                require_audio=True,
                durations=(None, None),
                measured=(None, 2.0),
            )
            is False
        )
        assert (
            self._call(
                require_video=True,
                require_audio=True,
                durations=(None, None),
                measured=(2.0, 0.0),
            )
            is False
        )

    def test_na_container_truncated_audio_rejected_by_measured_lengths(self):
        """Audit round 37 P1 counterexample: a no-stream-duration
        container (FLV) whose audio track is 0.1 s against 12 s of
        video — both tracks exist, each decodes cleanly, and the
        container duration is positive; the measured decoded lengths
        must reject it (11.9 s of audio body missing)."""
        assert (
            self._call(
                require_video=True,
                require_audio=True,
                durations=(None, None),
                measured=(12.0, 0.1),
            )
            is False
        )

    def test_shifted_start_tracks_accepted_by_end_compare(self):
        """Audit round 36 P1: MPEG-PS tracks legitimately start at
        different timestamps — a 532 ms DURATION difference on a healthy
        file (video start 0.533, audio start 1.112) must not reject it;
        the ENDS are what the muxer keeps aligned. Pins the audit's
        exact numbers: video (0.6, 1.9) → end 2.5, audio (0.589089,
        2.011433) → end 2.600522, end offset ≈ 100.5 ms ≤ 150 ms floor."""
        assert (
            self._call(
                require_video=True,
                require_audio=True,
                starts=(0.6, 0.589089),
                durations=(1.9, 2.011433),
            )
            is True
        )

    def test_end_compare_still_catches_truncated_track(self):
        """The end comparison must still reject a genuinely truncated
        secondary track when the starts differ: video ends at 1.9
        (0.5 + 1.4) vs audio ending at 1.6 (0.0 + 1.6) — 0.3 s of body
        missing, beyond the 150 ms floor."""
        assert (
            self._call(
                require_video=True,
                require_audio=True,
                starts=(0.5, 0.0),
                durations=(1.4, 1.6),
            )
            is False
        )

    def test_unknown_duration_allowed_for_single_stream(self):
        """A video-only / audio-only gate does NOT depend on the
        duration comparison — an unreadable duration field must not
        reject a file that probed and decoded cleanly."""
        assert self._call(require_video=True, require_audio=False, durations=(None, None)) is True
        assert self._call(require_video=False, require_audio=True, durations=(None, None)) is True

    def test_probe_infra_fault_raises_by_default(self):
        """Audit round 32 P1: a transient ffprobe timeout / spawn fault
        must RAISE (validation unavailable), not become False — False
        would make the controller delete a completed download."""
        import subprocess

        import pytest

        from stream2video.concat import probing

        for exc in (subprocess.TimeoutExpired("ffprobe", 10.0), FileNotFoundError, OSError):
            with (
                patch(
                    "stream2video.concat.probing._ffprobe_is_valid_media",
                    side_effect=exc,
                ),
                pytest.raises((subprocess.TimeoutExpired, FileNotFoundError, OSError)),
            ):
                probing._media_is_valid(Path("x.mp4"), require_video=True, require_audio=False)

    def test_probe_infra_fault_is_fail_safe_for_resume(self):
        """Resume gates pass fail_safe=True: an unprobed part is simply
        re-encoded (False) instead of aborting the whole run."""
        import subprocess

        from stream2video.concat import probing

        with patch(
            "stream2video.concat.probing._ffprobe_is_valid_media",
            side_effect=subprocess.TimeoutExpired("ffprobe", 10.0),
        ):
            assert (
                probing._media_is_valid(
                    Path("x.mp4"),
                    require_video=True,
                    require_audio=False,
                    fail_safe=True,
                )
                is False
            )

    def test_duration_probe_fault_raises_by_default(self):
        """Audit round 33 P1-1: a transient DURATION-probe fault must
        raise like the codec probe — round 32 covered only the codec
        one, so a hiccup here fell into the fail-closed branch and the
        controller deleted a completed download."""
        import subprocess

        import pytest

        from stream2video.concat import probing

        for exc in (subprocess.TimeoutExpired("ffprobe", 10.0), FileNotFoundError, OSError):
            with (
                patch("stream2video.concat.probing._ffprobe_is_valid_media", return_value=True),
                patch(
                    "stream2video.concat.probing._ffmpeg_decode_timing", return_value=(True, 5.0)
                ),
                patch("stream2video.concat.probing._ffprobe_stream_timing", side_effect=exc),
                pytest.raises((subprocess.TimeoutExpired, FileNotFoundError, OSError)),
            ):
                probing._media_is_valid(Path("x.mp4"), require_video=True, require_audio=False)

    def test_duration_probe_fault_is_fail_safe_for_resume(self):
        """fail_safe covers BOTH metadata probes uniformly."""
        import subprocess

        from stream2video.concat import probing

        with (
            patch("stream2video.concat.probing._ffprobe_is_valid_media", return_value=True),
            patch("stream2video.concat.probing._ffmpeg_decode_timing", return_value=(True, 5.0)),
            patch(
                "stream2video.concat.probing._ffprobe_stream_timing",
                side_effect=subprocess.TimeoutExpired("ffprobe", 10.0),
            ),
        ):
            assert (
                probing._media_is_valid(
                    Path("x.mp4"),
                    require_video=True,
                    require_audio=False,
                    fail_safe=True,
                )
                is False
            )


class TestRunFfprobe:
    """Audit round 33 P2: every metadata probe runs through ONE
    cancellable, bounded popen loop — a cancel fires within the poll
    cadence, not after the 10 s ceiling."""

    def test_cancel_aborts_promptly(self):
        from subprocess import TimeoutExpired

        import pytest

        from stream2video.concat import probing

        class _FakeProc:
            def __init__(self):
                import io

                self.stdout = io.StringIO("")
                self._waits = 10_000_000
                self._killed = False
                self.returncode = None

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                if self._waits > 0:
                    self._waits -= 1
                    raise TimeoutExpired("ffprobe", timeout or 0)
                return 0

            def kill(self):
                self._killed = True
                self.returncode = 1

        proc = _FakeProc()
        with (
            # The runner now lives in utils (audit round 36 P2) — its
            # module-level references are patched there.
            patch("stream2video.utils.popen_with_retry", return_value=proc),
            pytest.raises(probing.CancelledError),
        ):
            probing._run_ffprobe(["ffprobe", "x.mp4"], cancel_callback=lambda: True)
        assert proc._killed, "a cancelled probe must kill the child immediately"
        assert proc.poll() is not None, "the killed probe must be reaped"
        assert proc.stdout.closed, "the stdout pipe must be closed on every exit path"

    def test_timeout_propagates_and_kills(self):
        from subprocess import TimeoutExpired

        import pytest

        from stream2video.concat import probing

        class _FakeProc:
            def __init__(self):
                import io

                self.stdout = io.StringIO("")
                self._killed = False
                self.returncode = None

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                raise TimeoutExpired("ffprobe", timeout or 0)

            def kill(self):
                self._killed = True
                self.returncode = 1

        proc = _FakeProc()
        with (
            patch("stream2video.utils.popen_with_retry", return_value=proc),
            pytest.raises(TimeoutExpired),
        ):
            probing._run_ffprobe(["ffprobe", "x.mp4"], timeout=0.01)
        assert proc._killed
        assert proc.poll() is not None

    def test_stdout_joined_before_pipe_close(self):
        """Audit round 34 P1-2 regression: the drain thread must be
        joined to EOF BEFORE the pipe is closed — the old order raced
        the still-reading drain and lost buffered output, turning a
        healthy rc=0 probe into an empty-stdout false INVALID verdict."""
        from stream2video.concat import probing

        calls: list[str] = []

        class _FakePipe:
            def close(self):
                calls.append("close")

            def readline(self):
                return ""

        class _FakeProc:
            stdout = _FakePipe()
            returncode = 0

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                return self.returncode

        def fake_drain(pipe, sink, on_line=None):
            def wait_for_drain(timeout=None):
                calls.append("drain")
                sink.append("h264\n")
                return True

            return wait_for_drain

        with (
            patch("stream2video.utils.popen_with_retry", return_value=_FakeProc()),
            patch("stream2video.utils.drain_stderr_lines", side_effect=fake_drain),
        ):
            rc, stdout = probing._run_ffprobe(["ffprobe", "x.mp4"])
        assert rc == 0
        assert stdout == "h264\n"
        assert calls[0] == "drain", "the drain must be joined before the pipe close"
        assert "close" in calls

    def test_unfinished_drain_gets_pipe_close_and_short_grace(self):
        """Audit round 34 P1-2 fallback: when the drain does not reach
        EOF within the bound (a grandchild still holds the pipe), the
        helper closes the pipe to force EOF and retries one short wait
        instead of leaving the drain on a live fd forever."""
        from stream2video.concat import probing

        state = {"finished": False}

        class _FakePipe:
            def close(self):
                pass

            def readline(self):
                return ""

        class _FakeProc:
            stdout = _FakePipe()
            returncode = 0

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                return self.returncode

        def fake_drain(pipe, sink, on_line=None):
            def wait_for_drain(timeout=None):
                if not state["finished"]:
                    state["finished"] = True
                    return False  # drain missed the bound on the first wait
                return True

            return wait_for_drain

        with (
            patch("stream2video.utils.popen_with_retry", return_value=_FakeProc()),
            patch("stream2video.utils.drain_stderr_lines", side_effect=fake_drain),
        ):
            rc, _stdout = probing._run_ffprobe(["ffprobe", "x.mp4"])
        assert rc == 0
        assert state["finished"], "the second (grace) wait must have run"

    def test_media_complete_forwards_cancel_callback(self):
        """Audit round 34 P1-1: ``_ffprobe_media_complete`` ACCEPTED a
        cancel callback but dropped it before the runner — a hung
        primary probe waited out the full 10 s ceiling. Forwarding only."""
        from stream2video.concat import probing

        seen: list = []

        def fake_run(args, *, timeout=10.0, cancel_callback=None):
            seen.append(cancel_callback)
            return 0, "h264\n5.0\n"

        cb = lambda: False  # noqa: E731 — bare callback identity is the assertion
        with (
            patch("stream2video.utils.ffprobe_path", return_value="ffprobe"),
            patch("stream2video.concat.probing._run_ffprobe", side_effect=fake_run),
        ):
            assert probing._ffprobe_media_complete(Path("x.mp4"), cancel_callback=cb) is True
        assert seen == [cb], "the callback must reach _run_ffprobe"


def test_gapless_probe_cancel_propagates_instead_of_reencode(tmp_path: Path):
    """Audit round 34 P1-3: the gapless tree's resume probe is
    cancellable, but the surrounding ``except Exception`` swallowed a
    CancelledError raised mid-probe into a warning + re-encode. The
    user Cancel must escape IMMEDIATELY, before any re-encode runs."""
    from stream2video import concat as concat_mod
    from stream2video.concat.errors import CancelledError

    recorded: list = []

    def fake_one_pass(part_paths, output_path, vcodec, vcodec_opts, **kwargs):
        recorded.append(len(part_paths))

    def cancelled_probe(path, **kwargs):
        raise CancelledError("cancelled during metadata probe")

    with (
        patch.object(concat_mod, "_concat_filter_one_pass", side_effect=fake_one_pass),
        patch.object(concat_mod, "_GAPLESS_MAX_INPUTS_PER_CALL", 2),
        patch.object(concat_mod.gapless, "_media_is_valid", side_effect=cancelled_probe),
    ):
        parts = [tmp_path / f"seg_{i:06d}.mp4" for i in range(5)]
        for p in parts:
            p.write_bytes(b"\x00" * 2048)
        out = tmp_path / "out.mp4"
        # Reuse gate: a finished-looking intermediate from a previous run.
        tree_dir = out.parent / f"_gapless_tree_{out.stem}"
        tree_dir.mkdir(parents=True, exist_ok=True)
        (tree_dir / "L0_00000.mkv").write_bytes(b"\x00" * 2048)

        with pytest.raises(CancelledError):
            concat_mod._run_gapless_segment_concat(
                out,
                parts,
                "libx264",
                [],
                audio_quality="medium",
                total_duration=4.5,
                cancel_callback=lambda: False,
            )
    assert recorded == [], "a cancelled probe must never start a re-encode"


def test_gapless_duration_cache_cancel_propagates(tmp_path: Path):
    """Audit round 37 P2: the gapless tree's per-group duration cache
    used the legacy sync probe WITHOUT a cancel callback, and the
    surrounding ``except Exception`` swallowed a cancel into a 0.0 ETA
    no-op. The cancellable probe must abort the whole tree immediately,
    before any group encode starts."""
    from stream2video import concat as concat_mod
    from stream2video.concat.errors import CancelledError

    recorded: list = []

    def fake_one_pass(part_paths, output_path, vcodec, vcodec_opts, **kwargs):
        recorded.append(len(part_paths))

    def cancelled_dur(path, **kwargs):
        raise CancelledError("cancelled during duration cache probe")

    with (
        patch.object(concat_mod, "_concat_filter_one_pass", side_effect=fake_one_pass),
        patch.object(concat_mod, "_GAPLESS_MAX_INPUTS_PER_CALL", 2),
        patch.object(concat_mod, "get_video_duration", side_effect=cancelled_dur),
    ):
        parts = [tmp_path / f"seg_{i:06d}.mp4" for i in range(5)]
        for p in parts:
            p.write_bytes(b"\x00" * 2048)
        out = tmp_path / "out.mp4"

        with pytest.raises(CancelledError):
            concat_mod._run_gapless_segment_concat(
                out,
                parts,
                "libx264",
                [],
                audio_quality="medium",
                total_duration=4.5,
                cancel_callback=lambda: False,
            )
    assert recorded == [], "a cancelled duration probe must never start a group encode"


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
