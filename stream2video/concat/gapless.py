"""Gapless segment join via concat filter (re-encode both streams).

Supports a binary-tree reduction when the estimated command line
would exceed the per-call budget — on Windows that's the Win32 32K
CreateProcess limit; on POSIX the module-level per-call input cap.
See the kernel-level docstring for the design.
"""

import logging
import os
import shutil
from collections.abc import Callable
from pathlib import Path

from stream2video import concat as _c
from stream2video.concat.options import ConcatOptions, coerce_options
from stream2video.concat.probing import _media_is_valid
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
    options: ConcatOptions | None = None,
    **legacy_kwargs: object,
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
    options = coerce_options(options, legacy_kwargs)
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
    # (libx264 CRF 18 + PCM) and must not carry it.
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
            # The concat filter joins video frames and audio
            # PCM whose lengths can differ by a fraction of a frame at the
            # tail (per-part encode rounding, AAC priming, dropped final
            # frame). Without ``-shortest`` the muxer extends the output to
            # the LONGER stream — a frozen last video frame held over
            # silence, or a silent audio tail — making the output play
            # longer than the keep window at every join. ``-shortest``
            # truncates at the shorter stream, matching the segment
            # path's per-part guard (segment.py).
            "-shortest",
            *_movflags,
            str(output_path),
        ],
        progress_callback=_prog,
        timeout=timeout,
        label=label,
        cancel_callback=cancel_callback,
        memory_monitor=_c._new_memory_monitor(options.memory_monitor_factory, label),
        stall_kill=options.stall_kill,
        stall_warning=options.stall_warning,
        low_process_priority=options.low_process_priority,
        rlimit_as_mb=options.rlimit_as_mb,
    )


def _run_gapless_segment_concat(
    output_path: Path,
    part_paths: list[Path],
    vcodec: str,
    vcodec_opts: list[str],
    *,
    total_duration: float,
    progress_callback: Callable[[float], None] | None = None,
    cancel_callback: Callable[[], bool] | None = None,
    options: ConcatOptions | None = None,
    manifest: dict | None = None,
    **legacy_kwargs: object,
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
    and visually-lossless ``libx264 -crf 18 -preset ultrafast`` video
    (``-c:v copy`` is illegal with a ``-filter_complex`` graph — ffmpeg
    rejects the combination with "Streamcopy requested for output
    stream fed from a complex filtergraph"). ``ffv1`` would be
    bit-exact but blows up disk 10-30x (measured: 15.2GB source → L0
    intermediate ~300GB → -28 ENOSPC); only the final pass to
    ``output_path`` applies the user's ``audio_quality`` / video codec —
    the output is identical to what a single-pass would have produced,
    except the concat filter now runs 2·log(N)-ish times instead of once
    (near-lossless intermediates make the extra passes nearly free of
    quality loss).

     Tree intermediates are kept in ``output_path.parent /
     _gapless_tree_<stem>`` so an interrupted run keeps the working
     set for the next attempt; the dir is deleted on success. Reuse is
     gated on the run manifest (``manifest`` kwarg, written into the tree
     dir via ``_ensure_fresh_work_dir``) so intermediates from a run with
     different cut points / source / encoder are wiped, not reused.
     Intermediates use libx264 CRF 18 (visually lossless, ~source
     size) + PCM — ffv1 would be 10-30x larger (disk blowup on 15GB
     sources → -28 ENOSPC).
     """
    options = coerce_options(options, legacy_kwargs)
    n = len(part_paths)
    if n == 0:
        raise _c.ConcatError("gapless concat: no parts to join")

    # Maximum inputs per call so the final cmdline stays well under 32K.
    # Honour the module-level cap first (tests shrink it); then tighten it
    # further if the actual paths are long (a 250-char path in a deep temp
    # dir needs fewer inputs than an 80-char one to stay under 24K).
    # per-input = path + `-i "` + `" ` + `[i:v][i:a]` pads + argv headroom.
    # ``output_path`` is included in `prefix` so a long final output path
    # shrinks the budget too (the final pass carries the user codec opts).
    def _compute_max_inputs(paths: list[Path]) -> int:
        if os.name != "nt" or not paths:
            return _c._GAPLESS_MAX_INPUTS_PER_CALL
        prefix = 512 + len(str(output_path))
        worst_path = max(len(str(p)) for p in paths)
        per_input = worst_path + 23
        budget = 24_000 - prefix
        if per_input <= 0 or budget <= 0:
            return 2
        return min(_c._GAPLESS_MAX_INPUTS_PER_CALL, max(2, budget // per_input))

    max_inputs = _compute_max_inputs(part_paths)

    if n <= max_inputs:
        _c._concat_filter_one_pass(
            part_paths,
            output_path,
            vcodec,
            vcodec_opts,
            audio_codec="aac",
            audio_opts=[
                *_c._audio_bitrate_opts(options.audio_quality),
                *_c._audio_opts(options.audio_quality),
            ],
            total_duration=total_duration,
            progress_callback=(
                (lambda s: progress_callback(min(s / total_duration * 0.1, 0.1) + 0.9))
                if progress_callback and total_duration > 0
                else None
            ),
            cancel_callback=cancel_callback,
            timeout=options.final_concat_timeout,
            label="gapless segment concat",
            options=options,
        )
        return

    # ── Tree path (too many inputs for one pass) ──
    tree_dir = output_path.parent / f"_gapless_tree_{output_path.stem}"
    # Gate tree-intermediate reuse on a manifest snapshot identical to the
    # segment path's: ``tree_dir`` lives OUTSIDE the per-run seg_dir, so a
    # stale tree from an interrupted run would otherwise be reused after
    # the user changed cut points / source / encoder settings — the old
    # run's content would be silently concatenated into the new output.
    # `_ensure_fresh_work_dir` wipes the dir and rewrites the manifest on
    # any mismatch; a run that matches the manifest reuses intermediates.
    if manifest is not None:
        _c._ensure_fresh_work_dir(tree_dir, manifest)
    else:
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
        # The estimate ``total_groups_est`` is computed from the INITIAL
        # max_inputs; deeper levels can shrink max_inputs below it, so
        # completed_groups can overshoot 1.0 — clamp so the UI never sees
        # the bar regress past the 0.98 tree-phase ceiling.
        frac = min(completed_groups / max(1, total_groups_est), 1.0)
        progress_callback(min(0.9 + frac * 0.08, 0.98))

    # Estimate per-group duration so the batch map below can budget the
    # whole tree (groups G0..Gn are 0.9..0.98). The per-group figure is
    # read by ``for_each_input``-style ETA smoothing inside the loop.
    _tree_group_dur = (total_duration / max(1, total_groups_est)) if total_duration > 0 else 0

    # Per-run duration cache: the per-group ``_chunk_dur``
    # is probe-based (ffprobe per part) and only feeds progress scaling,
    # but a naive ``sum(get_video_duration(p) for p in chunk)`` fires one
    # ffprobe per part at EVERY tree level — the L0 intermediates are
    # re-probed at L1, L1's at L2, ... (≈ N·k/(k-1) probes for N parts).
    # Cache by path and seed it with each group's computed sum when the
    # intermediate is written, so the next level reuses the number
    # instead of re-probing: exactly N probes total (all at L0) for a
    # cold tree, zero additional probes for L1+.
    _dur_cache: dict[str, float] = {}

    def _cached_group_dur(chunk: list[Path]) -> float:
        total = 0.0
        for p in chunk:
            d = _dur_cache.get(str(p))
            if d is None:
                try:
                    # Cancellable probe (audit round 36 P2): with a
                    # large gapless tree a stuck per-part probe would
                    # hold the user's Cancel for up to the 10 s ceiling
                    # PER PART (audit round 37 P2). A cancel must abort
                    # the whole tree immediately — never become a 0.0
                    # ETA no-op.
                    d = _c.get_video_duration(p, cancel_callback=cancel_callback) or 0.0
                except _c.CancelledError:
                    raise
                except Exception:
                    d = 0.0
                _dur_cache[str(p)] = d
            total += d
        return total

    while len(current) > max_inputs:
        # Intermediate paths (``tree_dir\L{level}_{g:05d}.mkv``) are
        # typically *longer* than the original part paths (tree prefix +
        # fixed-width group suffix). Recompute the per-call cap from the
        # current level's actual paths so a long temp-dir prefix can't
        # push the deeper tree levels back over the Win32 32K line limit
        # on L1+.
        max_inputs = min(max_inputs, _compute_max_inputs(current))
        if len(current) <= max_inputs:
            break
        next_level: list[Path] = []
        n_groups = (len(current) + max_inputs - 1) // max_inputs
        for g in range(n_groups):
            chunk = current[g * max_inputs : (g + 1) * max_inputs]
            inter = tree_dir / f"L{level}_{g:05d}.mkv"
            if cancel_callback and cancel_callback():
                raise _c.CancelledError(f"gapless tree L{level} cancelled")
            reuse = inter.exists() and inter.stat().st_size >= options.min_part_bytes
            if reuse:
                # The unified gate, exactly like every other resume
                # path (audit round 35 P1): a header-only codec probe
                # accepts an intermediate whose BODY is damaged — a
                # zeroed-middle MKV passed both codec probes and its
                # full audio decode failed (reproduced live), yet the
                # corrupted audio track was reused into the next tree
                # level. ``_media_is_valid`` runs the codec probe, a
                # WHOLE-STREAM decode of every required stream and the
                # v/a duration match; ``fail_safe=True`` keeps infra
                # faults fail-closed (re-encode) and ``CancelledError``
                # still propagates immediately below.
                try:
                    reuse = _media_is_valid(
                        inter,
                        require_video=True,
                        require_audio=True,
                        timeout=float(options.final_concat_timeout),
                        cancel_callback=cancel_callback,
                        low_process_priority=options.low_process_priority,
                        rlimit_as_mb=options.rlimit_as_mb,
                        fail_safe=True,
                    )
                    if not reuse:
                        logger.warning(
                            f"gapless tree L{level}: intermediate {inter.name} "
                            f"failed full validation; re-encoding group {g}"
                        )
                except _c.CancelledError:
                    # A user Cancel fired DURING the metadata probe
                    # (the probe is cancellable now — audit round 33
                    # P2). It must propagate IMMEDIATELY — the generic
                    # handler below would swallow it into a warning +
                    # re-encode, delaying (and for edge-triggered
                    # callbacks, losing) the cancellation (audit round
                    # 34 P1-3).
                    raise
                except Exception:
                    # ffprobe missing / timed out / unexpected error — the
                    # file's integrity cannot be verified. A crash mid-write
                    # here does NOT *create* a corrupt file (the ffprobe
                    # probe is read-only), but NOT detecting one would let a
                    # truncated intermediate sneak into the output — the
                    # very hole the probe exists to close. Re-encode instead
                    # of silently trusting the size check.
                    logger.warning(
                        f"gapless tree L{level}: validation failed for {inter}; "
                        f"re-encoding group {g} to be safe",
                        exc_info=True,
                    )
                    reuse = False
            if reuse:
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
            _chunk_dur = _cached_group_dur(chunk)
            if _chunk_dur <= 0:
                _chunk_dur = _tree_group_dur

            def _make_group_prog(base_completed: int, chunk_dur: float) -> Callable[[float], None]:
                def _pg(s: float) -> None:
                    if progress_callback is None or total_groups_est == 0:
                        return
                    gf = max(0.0, min(1.0, s / max(1.0, chunk_dur)))
                    overall = (
                        0.9
                        + (base_completed / max(1, total_groups_est)) * 0.08
                        + gf * (0.08 / max(1, total_groups_est))
                    )
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
                timeout=options.final_concat_timeout,
                label=f"gapless tree L{level} G{g}",
                options=options,
            )
            completed_groups += 1
            _report_tree_progress()
            next_level.append(inter)
            # Seed the cache so the next tree level reuses this group's
            # computed sum instead of re-probing the fresh intermediate
            # with ffprobe.
            _dur_cache[str(inter)] = _chunk_dur
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
        audio_opts=[
            *_c._audio_bitrate_opts(options.audio_quality),
            *_c._audio_opts(options.audio_quality),
        ],
        total_duration=total_duration,
        progress_callback=_final_prog if progress_callback and total_duration > 0 else None,
        cancel_callback=cancel_callback,
        timeout=options.final_concat_timeout,
        label="gapless segment concat (final)",
        options=options,
    )
    shutil.rmtree(tree_dir, ignore_errors=True)
