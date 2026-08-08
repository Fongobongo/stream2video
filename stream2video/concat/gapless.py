"""Gapless segment join via concat filter (re-encode both streams).

Supports a binary-tree reduction on Windows when the number of inputs
would otherwise blow past the Win32 32K CreateProcess command-line
limit; see the kernel-level docstring for the design.
"""

import logging
import os
import shutil
from collections.abc import Callable
from pathlib import Path

from stream2video import concat as _c
from stream2video.concat.constants import (
    _FINAL_CONCAT_TIMEOUT,
    _MIN_PART_BYTES,
    _STALL_KILL,
    _STALL_WARNING,
)
from stream2video.memory import MemoryMonitor
from stream2video.tools import ffmpeg_path

logger = logging.getLogger(__name__)


def _concat_filter_one_pass(
    part_paths: list[Path],
    output_path: Path,
    vcodec: str,
    vcodec_opts: list[str],
    *,
    audio_codec: str,
    audio_opts: list[str],
    total_duration: float,
    progress_callback: Callable[[float], None] | None = None,
    cancel_callback: Callable[[], bool] | None = None,
    timeout: int,
    label: str,
    stall_kill: int,
    stall_warning: int,
    low_process_priority: bool = False,
    rlimit_as_mb: int = 0,
    memory_monitor_factory: Callable[[str], MemoryMonitor | None] | None = None,
) -> None:
    """Run one concat-filter pass over ``part_paths`` → ``output_path``.

    Decodes every part (video + audio) into a single joined stream and
    re-encodes once — exact gapless semantics for whichever codecs the
    caller picks. Used by :func:`_run_gapless_segment_concat` both for
    the final output (user codecs) and for intermediate levels when the
    input count exceeds the per-call command-line budget (PCM audio +
    libx264 CRF 18 video on intermediates — ``-c:v copy`` and ``ffv1``
    cannot be used with ``-filter_complex``: copy is rejected, ffv1
    blows up disk 10-30x).
    """
    n = len(part_paths)
    inputs: list[str] = []
    for p in part_paths:
        inputs.extend(["-i", str(p)])
    chain = "".join(f"[{i}:v][{i}:a]" for i in range(n))
    graph = f"{chain}concat=n={n}:v=1:a=1[outv][outa]"

    def _prog(seconds: float) -> None:
        if progress_callback and total_duration > 0:
            progress_callback(min(seconds / total_duration, 1.0))

    # ``-movflags +faststart`` is MP4-only; intermediates are MKV
    # (ffv1+pcm) and must not carry it.
    _movflags: list[str] = (
        ["-movflags", "+faststart"] if output_path.suffix.lower() == ".mp4" else []
    )
    _c._run_ffmpeg(
        [
            ffmpeg_path(),
            "-y",
            "-loglevel",
            "error",
            "-progress",
            "pipe:1",
            *inputs,
            "-filter_complex",
            graph,
            "-map",
            "[outv]",
            "-map",
            "[outa]",
            "-c:v",
            vcodec,
            *vcodec_opts,
            "-c:a",
            audio_codec,
            *audio_opts,
            *_movflags,
            str(output_path),
        ],
        progress_callback=_prog,
        timeout=timeout,
        label=label,
        cancel_callback=cancel_callback,
        memory_monitor=_c._new_memory_monitor(memory_monitor_factory, label),
        stall_kill=stall_kill,
        stall_warning=stall_warning,
        low_process_priority=low_process_priority,
        rlimit_as_mb=rlimit_as_mb,
    )


def _run_gapless_segment_concat(
    output_path: Path,
    part_paths: list[Path],
    vcodec: str,
    vcodec_opts: list[str],
    *,
    audio_quality: str = "medium",
    total_duration: float,
    progress_callback: Callable[[float], None] | None = None,
    cancel_callback: Callable[[], bool] | None = None,
    timeout: int = _FINAL_CONCAT_TIMEOUT,
    stall_kill: int = _STALL_KILL,
    stall_warning: int = _STALL_WARNING,
    low_process_priority: bool = False,
    rlimit_as_mb: int = 0,
    memory_monitor_factory: Callable[[str], MemoryMonitor | None] | None = None,
) -> None:
    """Gapless segment join via concat filter (re-encode both streams).

    The concat demuxer stream-copies per-segment AAC, preserving each
    segment's encoder priming (~21ms at 48kHz) — N segments drift
    ~21*N ms. The concat filter decodes every segment's audio into PCM,
    concatenates the PCM buffers, and re-encodes once, so priming is
    added only once (not per-segment).

    Video is also re-encoded through the concat filter (``v=1:a=1``).
    This is the trade-off of gapless_concat: the video quality loss is
    one generation (H.264 → decode → H.264), but the output is truly
    gapless (no per-segment priming on either stream). For lossless
    video + gapless audio, use ``cut_then_encode`` instead — it does
    one encode pass total, but sacrifices frame accuracy (``-c copy``
    snaps to keyframes).

    The command shape is::

        ffmpeg -i seg_0.mp4 -i seg_1.mp4 ... -i seg_N.mp4 \\
               -filter_complex "[0:v][0:a][1:v][1:a]...concat=n=N:v=1:a=1[outv][outa]" \\
               -map "[outv]" -map "[outa]" \\
               -c:v <vcodec> <vcodec_opts> -c:a aac -b:a <bitrate> \\
               -ar 48000 -ac 2 -movflags +faststart \\
               output.mp4

    The ``-filter_complex`` graph interleaves video and audio pads
    (``[0:v][0:a][1:v][1:a]...``) because the concat filter expects
    them in that order, not all-videos-then-all-audios.

    Windows caps the command line at 32,767 chars; with a few hundred
    segments the single call can not fit (measured 48 069 at 381 of
    108-char paths — winerror 206). When the estimated single-pass
    command approaches the limit, we cascade the joins into a tree:
    groups of at most ``max_inputs`` parts are joined into
    intermediates, then the intermediates are joined again, etc.
     Intermediates use lossless PCM audio (no priming is ever written)
     and lossless ``ffv1`` video (``-c:v copy`` is illegal with a
     ``-filter_complex`` graph — ffmpeg rejects the combination with
     "Streamcopy requested for output stream fed from a complex
     filtergraph"). ``ffv1`` is bit-exact and fast enough for the
     intermediates; only the final pass to ``output_path`` applies the
     user's ``audio_quality`` / video codec — the output is identical to
     what a single-pass would have produced, except the concat filter
     now runs 2·log(N)-ish times instead of once (lossless intermediates
     make the extra passes free of quality loss).

     Tree intermediates are kept in ``output_path.parent /
     _gapless_tree_<stem>`` so an interrupted run keeps the working
     set for the next attempt; the dir is deleted on success.
     Intermediates use libx264 CRF 18 (visually lossless, ~source
     size) + PCM — ffv1 would be 10-30x larger (disk blowup on 15GB
     sources → -28 ENOSPC).
     """
    n = len(part_paths)
    if n == 0:
        raise _c.ConcatError("gapless concat: no parts to join")

    # Maximum inputs per call so the final cmdline stays well under 32K.
    # Honour the module-level cap first (tests shrink it); then tighten it
    # further if the actual paths are long (a 250-char path in a deep temp
    # dir needs fewer inputs than an 80-char one to stay under 24K).
    if os.name == "nt":
        worst_path = max(len(str(p)) for p in part_paths) + 23
        max_inputs = min(
            _c._GAPLESS_MAX_INPUTS_PER_CALL,
            max(2, (24_000 - 512) // max(1, worst_path)),
        )
    else:
        max_inputs = _c._GAPLESS_MAX_INPUTS_PER_CALL

    if n <= max_inputs or os.name != "nt":
        _c._concat_filter_one_pass(
            part_paths,
            output_path,
            vcodec,
            vcodec_opts,
            audio_codec="aac",
            audio_opts=[*_c._audio_bitrate_opts(audio_quality), *_c._audio_opts(audio_quality)],
            total_duration=total_duration,
            progress_callback=(
                (lambda s: progress_callback(min(s / total_duration * 0.1, 0.1) + 0.9))
                if progress_callback and total_duration > 0
                else None
            ),
            cancel_callback=cancel_callback,
            timeout=timeout,
            label="gapless segment concat",
            stall_kill=stall_kill,
            stall_warning=stall_warning,
            low_process_priority=low_process_priority,
            rlimit_as_mb=rlimit_as_mb,
            memory_monitor_factory=memory_monitor_factory,
        )
        return

    # ── Tree path (Windows, too many inputs for one pass) ──
    tree_dir = output_path.parent / f"_gapless_tree_{output_path.stem}"
    tree_dir.mkdir(parents=True, exist_ok=True)

    # Tree uses overall 0.9..1.0 (pipeline_controller: 90% Cutting / 10%
    # Concatenating). Reserve 0.9..0.98 for tree intermediate groups and
    # 0.98..1.0 for final pass so the ETA moves during L0. Log ETA heads
    # up so pipeline_controller can show remaining even when per-group
    # progress stalls between groups.
    current = list(part_paths)
    level = 0
    completed_groups = 0
    total_groups_est = 0
    _tc = list(part_paths)
    while len(_tc) > max_inputs:
        total_groups_est += (len(_tc) + max_inputs - 1) // max_inputs
        _tc = [None] * ((len(_tc) + max_inputs - 1) // max_inputs)  # type: ignore[list-item]

    def _report_tree_progress() -> None:
        if progress_callback is None or total_groups_est == 0 or total_duration <= 0:
            return
        frac = completed_groups / max(1, total_groups_est)
        progress_callback(0.9 + frac * 0.08)

    # Estimate per-group duration so the batch map below can budget the
    # whole tree (groups G0..Gn are 0.9..0.98). The per-group figure is
    # read by ``for_each_input``-style ETA smoothing inside the loop.
    _tree_group_dur = (total_duration / max(1, total_groups_est)) if total_duration > 0 else 0

    while len(current) > max_inputs:
        next_level: list[Path] = []
        n_groups = (len(current) + max_inputs - 1) // max_inputs
        for g in range(n_groups):
            chunk = current[g * max_inputs : (g + 1) * max_inputs]
            inter = tree_dir / f"L{level}_{g:05d}.mkv"
            if cancel_callback and cancel_callback():
                raise _c.CancelledError(f"gapless tree L{level} cancelled")
            if inter.exists() and inter.stat().st_size >= _MIN_PART_BYTES:
                next_level.append(inter)
                completed_groups += 1
                _report_tree_progress()
                continue
            logger.info(
                f"gapless tree L{level}: joining group {g + 1}/{n_groups} "
                f"({len(chunk)} parts) -> {inter.name}"
            )
            # Intermediates: video re-encoded, audio PCM (no AAC priming).
            # ``-c:v copy`` is NOT valid with ``-filter_complex``:
            # ffmpeg refuses "Streamcopy requested for output stream fed
            # from a complex filtergraph" (2026-08-06 failure on 380
            # segments L0 G0). Previous fix used lossless ``ffv1`` which
            # is bit-exact but blows up disk: raw 1080p30 ~90MB/s,
            # ffv1 ~30MB/s (~240 Mbps, 30x source 5-8 Mbps). Your 15.2GB
            # source -> L0 intermediate ~300GB -> -28 No space left on
            # device (your 18:50 error). Switch to visually-lossless
            # ``libx264 -crf 18 -preset ultrafast``: ~5-10 Mbps similar
            # to source, 3-5x smaller than ffv1, still near-lossless.
            # Trade-off: one extra lossy generation vs single-pass
            # gapless (lossless intermediates would be 1 gen, this is 2
            # gens). For true lossless + no disk blowup use
            # ``method=cut_then_encode`` (one encode total, stream-copy
            # cuts). PCM stays sample-accurate via concat filter.
            # Intra-group out_time_us so 4/4 doesn't freeze 1-2 mins per group.
            # Use chunk duration for scaling (sum of segment durations).
            try:
                _chunk_dur = sum(_c.get_video_duration(p) or 0 for p in chunk)
            except Exception:
                _chunk_dur = _tree_group_dur
            if _chunk_dur <= 0:
                _chunk_dur = _tree_group_dur

            def _make_group_prog(base_completed: int, chunk_dur: float) -> Callable[[float], None]:
                def _pg(s: float) -> None:
                    if progress_callback is None or total_groups_est == 0:
                        return
                    gf = max(0.0, min(1.0, s / max(1.0, chunk_dur)))
                    overall = 0.9 + (base_completed / max(1, total_groups_est)) * 0.08 + gf * (0.08 / max(1, total_groups_est))
                    progress_callback(min(0.98, overall))

                return _pg

            _group_prog = _make_group_prog(completed_groups, _chunk_dur)
            _c._concat_filter_one_pass(
                chunk,
                inter,
                "libx264",
                ["-crf", "18", "-preset", "ultrafast", "-pix_fmt", "yuv420p"],
                audio_codec="pcm_s16le",
                audio_opts=[],
                total_duration=_chunk_dur,
                    progress_callback=_group_prog,
                cancel_callback=cancel_callback,
                timeout=timeout,
                label=f"gapless tree L{level} G{g}",
                stall_kill=stall_kill,
                stall_warning=stall_warning,
                low_process_priority=low_process_priority,
                rlimit_as_mb=rlimit_as_mb,
                memory_monitor_factory=memory_monitor_factory,
            )
            completed_groups += 1
            _report_tree_progress()
            next_level.append(inter)
        current = next_level
        level += 1

    # Final pass: encode video + audio with user settings, full priming
    # fix happens here (audiodec=re-encode), progress callback mapped to
    # the last 10% of the bar — but tree already consumed 0.9..0.99, so
    # map final encode to 0.99..1.0 via _prog wrapper below, or directly
    # to 0.9..1.0 when there's no tree (n <= max_inputs). For tree case
    # we remap to the tail slice to keep overall monotonic.
    logger.info(
        f"gapless tree: {n} parts -> {len(current)} intermediates after "
        f"{level + 1} level(s); final join with encode"
    )

    def _final_prog(seconds: float) -> None:
        if progress_callback is None or total_duration <= 0:
            return
        # Tree case reserves 0.9..0.98 for groups, 0.98..1.0 for final
        base, span = (0.98, 0.02) if total_groups_est > 0 else (0.9, 0.1)
        frac = max(0.0, min(1.0, seconds / total_duration))
        progress_callback(base + frac * span)

    _c._concat_filter_one_pass(
        current,
        output_path,
        vcodec,
        vcodec_opts,
        audio_codec="aac",
        audio_opts=[*_c._audio_bitrate_opts(audio_quality), *_c._audio_opts(audio_quality)],
        total_duration=total_duration,
        progress_callback=_final_prog if progress_callback and total_duration > 0 else None,
        cancel_callback=cancel_callback,
        timeout=timeout,
        label="gapless segment concat (final)",
        stall_kill=stall_kill,
        stall_warning=stall_warning,
        low_process_priority=low_process_priority,
        rlimit_as_mb=rlimit_as_mb,
        memory_monitor_factory=memory_monitor_factory,
    )
    shutil.rmtree(tree_dir, ignore_errors=True)
