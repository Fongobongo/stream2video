#!/usr/bin/env python3
"""Benchmark x264 presets on the stream2video pipeline.

Generates a synthetic source (testsrc + sine tone), runs cut_and_concat
on it with each of the named x264 presets (ultrafast / veryfast / fast /
medium / slow / slower / superfast / placebo — the user can filter via
--presets), and prints a comparative table of wall-clock time, output
file size, and output bitrate.

Usage::

    uv run scripts/benchmark_presets.py
    uv run scripts/benchmark_presets.py --presets ultrafast,medium,slow
    uv run scripts/benchmark_presets.py --duration 30 --size 640x480
    uv run scripts/benchmark_presets.py --method batch
    uv run scripts/benchmark_presets.py --repeat 3

The script deliberately doesn't import pytest — it's a standalone tool
a user can run from a fresh checkout. It calls cut_and_concat directly
so the benchmark measures the same code path the pipeline runs in
production, not a synthetic re-implementation.

Why testsrc + sine: ``testsrc`` produces a uniformly-compressible
pattern (same entropy across frames), so the encode time differences
between presets reflect the encoder's work, not the source's entropy.
``sine`` is a steady 440 Hz tone — small and constant. Real-world
sources would vary run-to-run; this synthetic source is reproducible.

The output table columns:
  * preset — x264 preset name
  * time_s — wall-clock encode time (seconds)
  * size_mb — output MP4 size (MiB)
  * bitrate_kbps — output video stream bitrate (kbps via ffprobe)
  * speed — realtime_factor (source_duration / encode_time); 1.0 = realtime
  * ratio — size / ultrafast_baseline size (1.0 == ultrafast)

Default repeat=3 runs each preset 3 times and reports the median
wall-clock time (the first run warms the OS file cache; the median
of 3 is less noisy than a single sample).
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

# Resolve the package without installing it (uv run handles this).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stream2video.concat import cut_and_concat
from stream2video.silence import SilenceSegment

VALID_X264_PRESETS = [
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
    "placebo",
]
DEFAULT_BENCH_PRESETS = ["ultrafast", "veryfast", "fast", "medium"]


def _generate_source(out: Path, duration: float, fps: int, size: str) -> None:
    """Generate a synthetic source via lavfi testsrc + sine."""
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=duration={duration}:size={size}:rate={fps}",
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
            "128k",
            str(out),
        ],
        check=True,
    )


def _probe_video_bitrate(path: Path) -> float:
    """Return the output video stream's bitrate in kbps (or 0.0 on error)."""
    try:
        out = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=bit_rate",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        data = json.loads(out)
        streams = data.get("streams", [])
        if streams:
            br = streams[0].get("bit_rate")
            if br is not None:
                return float(br) / 1000.0  # bps -> kbps
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        pass
    return 0.0


def _run_one(
    source: Path,
    out: Path,
    preset: str,
    method: str,
    video_quality: str,
) -> dict:
    """Run cut_and_concat with a single preset; return timing + size."""
    # Two silence segments -> three keep ranges. For a source of duration D
    # with silence=[2,4]+[6,8], the kept range is [0,2]+[4,6]+[8,D],
    # exercising the per-segment encode + concat path's segment-count
    # branch (n_segs=3) without triggering the "segment clamped to fit
    # duration" warning when D >= 8.
    silence = [SilenceSegment(2.0, 4.0), SilenceSegment(6.0, 8.0)]
    t0 = time.perf_counter()
    cut_and_concat(
        source,
        silence,
        out,
        method=method,
        encoder="libx264",
        video_quality=video_quality,
        audio_quality="medium",
        x264_preset=preset,
    )
    elapsed = time.perf_counter() - t0
    size_mb = out.stat().st_size / (1024 * 1024)
    bitrate_kbps = _probe_video_bitrate(out)
    return {
        "preset": preset,
        "time_s": elapsed,
        "size_mb": size_mb,
        "bitrate_kbps": bitrate_kbps,
    }


def _format_table(rows: list[dict], baseline: str) -> str:
    """Format results as a fixed-width comparative table."""
    if not rows:
        return "(no rows)"
    baseline_size = next(
        (r["size_mb"] for r in rows if r["preset"] == baseline),
        rows[0]["size_mb"],
    )
    headers = ["preset", "time_s", "size_mb", "bitrate_kbps", "speed", "ratio"]
    widths = {h: max(len(h), 10) for h in headers}
    widths["preset"] = max(len(r["preset"]) for r in rows) + 2
    sep = "  "
    lines = [sep.join(h.ljust(widths[h]) for h in headers)]
    lines.append(sep.join("-" * widths[h] for h in headers))
    for r in rows:
        # speed: source seconds per encode second; >1 means faster than realtime
        speed = 6.0 / r["time_s"] if r["time_s"] > 0 else 0.0
        ratio = r["size_mb"] / baseline_size if baseline_size > 0 else 0.0
        cells = {
            "preset": r["preset"],
            "time_s": f"{r['time_s']:.2f}",
            "size_mb": f"{r['size_mb']:.2f}",
            "bitrate_kbps": f"{r['bitrate_kbps']:.0f}",
            "speed": f"{speed:.2f}x",
            "ratio": f"{ratio:.2f}",
        }
        lines.append(sep.join(cells[h].ljust(widths[h]) for h in headers))
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Benchmark x264 presets on the stream2video pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--presets",
        type=str,
        default=",".join(DEFAULT_BENCH_PRESETS),
        help=(
            "Comma-separated x264 presets to benchmark "
            f"(default: {','.join(DEFAULT_BENCH_PRESETS)}). "
            f"Valid: {','.join(VALID_X264_PRESETS)}."
        ),
    )
    p.add_argument(
        "--method",
        choices=["segment", "batch", "cut_then_encode"],
        default="segment",
        help="Pipeline method (default: segment).",
    )
    p.add_argument(
        "--duration",
        type=float,
        default=10.0,
        help="Synthetic source duration in seconds (default: 10).",
    )
    p.add_argument(
        "--fps",
        type=int,
        default=30,
        help="Synthetic source frame rate (default: 30).",
    )
    p.add_argument(
        "--size",
        type=str,
        default="320x240",
        help="Synthetic source frame size WxH (default: 320x240).",
    )
    p.add_argument(
        "--video-quality",
        choices=["low", "medium", "high", "veryhigh"],
        default="medium",
        help="Video quality (default: medium).",
    )
    p.add_argument(
        "--repeat",
        type=int,
        default=3,
        help=("Number of runs per preset. The median wall-clock time is reported (default: 3)."),
    )
    p.add_argument(
        "--keep-source",
        action="store_true",
        help="Keep the generated source file (default: delete).",
    )
    p.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help=(
            "Working directory for source + outputs (default: a fresh tempdir under the OS temp)."
        ),
    )
    args = p.parse_args()

    # Parse --presets.
    requested = [s.strip() for s in args.presets.split(",") if s.strip()]
    invalid = [s for s in requested if s not in VALID_X264_PRESETS]
    if invalid:
        p.error(f"Unknown preset(s): {','.join(invalid)}. Valid: {','.join(VALID_X264_PRESETS)}")
    if not requested:
        p.error("No presets selected.")

    # Work directory.
    if args.work_dir is not None:
        work = args.work_dir
        work.mkdir(parents=True, exist_ok=True)
        cleanup = False
    else:
        import tempfile

        work = Path(tempfile.mkdtemp(prefix="s2v_bench_"))
        cleanup = not args.keep_source

    print(f"[bench] work dir: {work}")
    print(
        f"[bench] source: {args.duration}s @ {args.fps}fps {args.size}, "
        f"method={args.method}, video_quality={args.video_quality}, "
        f"repeat={args.repeat}"
    )
    print(f"[bench] presets: {','.join(requested)}")

    # Generate the source once — all presets encode the same input.
    source = work / "source.mp4"
    t0 = time.perf_counter()
    _generate_source(source, args.duration, args.fps, args.size)
    print(
        f"[bench] source generated: {source} "
        f"({source.stat().st_size / 1024 / 1024:.2f} MiB in "
        f"{time.perf_counter() - t0:.2f}s)"
    )

    rows: list[dict] = []
    for preset in requested:
        samples: list[float] = []
        last_out: Path | None = None
        for i in range(args.repeat):
            out = work / f"out_{preset}_{i}.mp4"
            if out.exists():
                out.unlink()
            r = _run_one(source, out, preset, args.method, args.video_quality)
            samples.append(r["time_s"])
            last_out = out
            print(
                f"  {preset} run {i + 1}/{args.repeat}: "
                f"{r['time_s']:.2f}s, "
                f"{r['size_mb']:.2f} MiB, "
                f"{r['bitrate_kbps']:.0f} kbps"
            )
        median_time = statistics.median(samples)
        size_mb = last_out.stat().st_size / (1024 * 1024) if last_out else 0.0
        bitrate_kbps = _probe_video_bitrate(last_out) if last_out else 0.0
        rows.append(
            {
                "preset": preset,
                "time_s": median_time,
                "size_mb": size_mb,
                "bitrate_kbps": bitrate_kbps,
            }
        )

    # Print the comparative table.
    print()
    print(f"Results (median of {args.repeat} runs, baseline=ultrafast):")
    print(_format_table(rows, baseline=requested[0]))

    # Cleanup.
    if cleanup:
        import shutil

        shutil.rmtree(work, ignore_errors=True)
        print(f"\n[bench] cleaned up {work}")
    elif args.keep_source:
        print(f"\n[bench] source + outputs kept in {work}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
