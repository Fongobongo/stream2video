"""Segment pipeline: encode each keep segment, join with concat demuxer
(or concat filter for gapless output)."""

import logging
import shutil
from collections.abc import Callable
from pathlib import Path

from stream2video import concat as _c
from stream2video.concat.options import ConcatOptions, coerce_options
from stream2video.tools import ffmpeg_path

logger = logging.getLogger(__name__)


def _run_segment_concat(
    video_path: Path,
    keep_segments: list[tuple[float, float]],
    output_path: Path,
    vcodec: str,
    vcodec_opts: list[str],
    progress_callback: Callable[[float], None] | None = None,
    cancel_callback: Callable[[], bool] | None = None,
    options: ConcatOptions | None = None,
    **legacy_kwargs: object,
) -> None:
    """Encode each segment, join with concat demuxer (or concat filter for gapless).

    Segments are stored in a dedicated subdirectory.  If a previous run was
    interrupted, already-encoded segments are reused (resume from where it
    stopped).  On success all segment files are deleted.

    Resume integrity: the work dir contains a ``_manifest.json``
    snapshot of (source path/size/mtime, encoder, encoder_opts, quality,
    keep_segments, pipeline_version). A mismatch wipes the work dir so
    old artifacts from an incompatible run cannot be reused. Each
    resumed segment is also ffprobe-validated so a partial moov-atom
    crash artifact is detected and re-encoded.
    """
    options = coerce_options(options, legacy_kwargs)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    total_duration = sum(e - s for s, e in keep_segments)
    n_segs = len(keep_segments)
    logger.info(f"segment: {n_segs} segments, {total_duration:.1f}s output, {vcodec}")

    seg_dir = output_path.parent / f"_{output_path.stem}_segments"
    manifest = _c._build_manifest(
        video_path,
        keep_segments,
        "segment",
        options.encoder,
        vcodec,
        vcodec_opts,
        options.video_quality,
        options.audio_quality,
        options.x264_preset,
        options.encoder_threads,
        output_fps=options.output_fps,
        gapless_concat=options.gapless_concat,
        source_has_audio=options.source_has_audio,
    )
    _c._ensure_fresh_work_dir(seg_dir, manifest)

    encoded_keep = 0.0
    skipped = 0

    # ``keep_segments`` are in the *detected* timeline that silence
    # detection produced. The WAV mirror is a plain PCM file, so its
    # timestamps start at 0 — even on sources with a non-zero container
    # ``start_time`` (OBS ``-itsoffset``, mid-file re-mux) the detected
    # segments are in user-visible source-time coordinates. An input-side
    # ``-ss`` seek positions by file position, which is the same space —
    # no start_time compensation is needed here (verified on ffmpeg 8.1.1
    # with a 6s source shifted by ``-itsoffset 5``: ``-ss start`` alone
    # yields the exact requested window). The batch path compensates only
    # its ``trim`` endpoints, which operate in ``-copyts`` PTS space.
    try:
        for i, (start, end) in enumerate(keep_segments):
            if cancel_callback and cancel_callback():
                raise _c.CancelledError("segment encode cancelled")

            dur = end - start
            seg_path = seg_dir / f"seg_{i:06d}.mp4"

            # Resume: skip already encoded segments. Require both a
            # minimum size AND a successful ffprobe read so a crash
            # artifact (missing moov atom) doesn't get reused and
            # corrupt the final concat in the middle. When the source
            # has audio, also probe the audio stream — a segment killed
            # between the moov header write and the AAC body pass can
            # validate as video-but-not-audio and would otherwise inject
            # a broken track into the final concat (mirrors the audio
            # check cut_encode.py already does for `_cut_*.mp4`).
            #
            # Duration check: a segment whose MP4 moov reports a
            # plausible duration but whose body was truncated by a
            # mid-flush kill produces a "valid" ffprobe header read yet
            # a duration shorter than the requested ``dur``. Without
            # this probe the resume path would lock the broken segment
            # into the manifest and the final concat would silently
            # drop the missing tail. Mirrors ``batch.py`` (slack=1.0)
            # and ``cut_encode.py`` (slack=1.0 after the P2 audit).
            #
            # FULL decode (audit round 29 P4): a header can carry the
            # PLANNED duration while the middle of the body is corrupt
            # — neither the codec probe nor the duration check sees
            # that. Only a whole-stream decode reads every packet.
            if (
                seg_path.exists()
                and seg_path.stat().st_size >= options.min_part_bytes
                and _c._ffprobe_is_valid_mp4(seg_path)
                and (
                    not options.source_has_audio
                    or _c._ffprobe_is_valid_media(seg_path, stream_type="a")
                )
                and _c._ffprobe_duration_ok(seg_path, dur)
                and _c._ffmpeg_full_decode(seg_path, stream_type="v")
            ):
                skipped += 1
                encoded_keep += dur
                if progress_callback and total_duration > 0:
                    progress_callback(min(encoded_keep / total_duration * 0.9, 0.9))
                continue

            # Frame-accurate segment encode using a SINGLE input-side seek.
            #
            # The earlier pipeline used both an input-side `-ss` (coarse,
            # keyframe-aligned fast seek) AND an output-side `-ss` for an
            # additional sub-keyframe correction. Two consecutive seeks on
            # the same input produce an off-by-~0.5s systematic bias that
            # cuts the start of every segment after t≈0.5s, and combined
            # with the `setpts=N/FRAME_RATE/TB` resync it also dropped
            # frames at the boundary (verified: a 6s/30FPS source with
            # keep=[(0,2),(3,5)] produced 4.72s/135 frames instead of
            # the expected 4.00s/120).
            #
            # The current approach:
            #   1. Input-side `-ss {start}` performs the seek. ffmpeg's
            #      MP4 demuxer decodes from the preceding keyframe and
            #      drops frames until `start` automatically -- this is
            #      frame-accurate on modern ffmpeg builds (verified with
            #      ffmpeg 8.1.1 on a GOP=30 source at a sub-keyframe
            #      cut: a 1.5s keep returned exactly 1.500s/45 frames).
            #   2. `-t {dur}` (output-side duration on the WHOLE output)
            #      limits both video and audio to exactly `dur`. No extra
            #      `apad`/`atrim` is needed: audio is also bound by
            #      `-t`, so no per-segment `_AUDIO_PAD` drift accumulates.
            #   3. setpts/atrim are unnecessary: the input PTS is already
            #      in source time, and after `-ss`+`-t` the segment's
            #      output starts at t=0 by ffmpeg's normalisation (genpts
            #      by the muxer). The concat demuxer handles the join.
            #
            # `-copyts` is NOT used: kept off so the per-segment output
            # timeline starts at 0 (the contract the concat demuxer
            # expects when ``-fflags +genpts`` (demuxer-side, placed
            # before ``-i``) rebuilds the final PTS).
            # Without `-copyts`, timestamps in the segment file are
            # already normalised to start at 0, so a `setpts=PTS-STARTPTS`
            # is a no-op here and is omitted for clarity.

            seg_prog = _c._seg_progress_callback(
                progress_callback, total_duration, encoded_keep, dur
            )

            label_text = f"segment {i} encode"
            _c._run_ffmpeg(
                [
                    ffmpeg_path(),
                    "-y",
                    "-loglevel",
                    "error",
                    "-progress",
                    "pipe:1",
                    "-ss",
                    # Microsecond precision (was millisecond): silence
                    # boundaries come from silencedetect at sub-ms float
                    # precision, and .3f rounding could shift a cut point
                    # by up to half a millisecond per boundary.
                    f"{start:.6f}",
                    "-i",
                    str(video_path),
                    "-t",
                    f"{dur:.6f}",
                    # Explicit stream mapping: pick the first
                    # video stream and the first audio stream rather
                    # than letting ffmpeg auto-select. A source with
                    # multiple audio tracks (e.g. dual-language MKV)
                    # would otherwise have its track choice depend on
                    # ffmpeg's stream-order heuristic, which isn't
                    # stable across versions. When the source has no
                    # audio, audio mapping and the AAC encoder are
                    # omitted entirely so the segment encode produces
                    # a valid video-only MP4 instead of failing with
                    # "Output file does not contain any stream".
                    "-map",
                    "0:v:0",
                    *(
                        # When the user requests a CFR target
                        # (options.output_fps != "source"), apply the ``fps``
                        # filter on the video stream. Without a filter
                        # graph the ``-r`` output option would work
                        # too, but the filter is the documented way
                        # to do it post-encode PTS normalisation and
                        # matches the batch path's filter chain shape.
                        # ``_fps_vf_option`` reuses the batch path's
                        # VALID_OUTPUT_FPS gate so a bad value can't
                        # reach ffmpeg as ``fps=bogus``.
                        _c._fps_vf_option(options.output_fps)
                    ),
                    "-c:v",
                    vcodec,
                    *vcodec_opts,
                    *(
                        [
                            "-map",
                            "0:a:0?",
                            "-c:a",
                            "aac",
                            *_c._audio_bitrate_opts(options.audio_quality),
                            *_c._audio_opts(options.audio_quality),
                            # Trim/pad the audio to EXACTLY ``dur``. Without
                            # it the AAC encoder's ~21 ms frame/priming
                            # overshoot leaves each segment's audio slightly
                            # LONGER than its video (2.0053s vs 2.0000s).
                            # The final concat-demuxer ``-c copy`` join then
                            # re-estimates the video rate from the stretched
                            # total duration and ffprobe reports a wrong
                            # r_frame_rate (359/12 for a 30/1 source on
                            # ffmpeg 9.0.1 — verified locally). ``apad``
                            # fills a short audio tail with silence, and the
                            # trailing ``atrim`` clamps both directions to
                            # exactly ``dur`` — the same normalisation the
                            # batch path has always applied (batch's
                            # ``atrim=0:{e-s}``) and the reason its tests
                            # pass on ffmpeg 9.
                            "-af",
                            f"apad,atrim=0:{dur:.6f}",
                        ]
                        if options.source_has_audio
                        else []
                    ),
                    *(
                        # When fps conversion duplicates frames, the
                        # video track grows past ``dur`` while audio is
                        # bound by ``-t`` — without ``-shortest`` the muxer
                        # extends the segment by the duplicated tail and
                        # the final concat plays longer than the keep
                        # window (frozen video over silence at each join).
                        ["-shortest"]
                        if options.source_has_audio and options.output_fps != "source"
                        else []
                    ),
                    str(seg_path),
                ],
                progress_callback=seg_prog,
                timeout=options.segment_encode_timeout,
                label=label_text,
                cancel_callback=cancel_callback,
                memory_monitor=_c._new_memory_monitor(options.memory_monitor_factory, label_text),
                stall_kill=options.stall_kill,
                stall_warning=options.stall_warning,
                low_process_priority=options.low_process_priority,
                rlimit_as_mb=options.rlimit_as_mb,
            )

            encoded_keep += dur
            if progress_callback and total_duration > 0:
                progress_callback(min(encoded_keep / total_duration * 0.9, 0.9))

        if skipped:
            logger.info(
                f"segment: resumed {skipped}/{n_segs} already encoded, encoded {n_segs - skipped}"
            )

        # Final join. Two strategies, picked by the ``gapless_concat``
        # flag and whether the source has audio:
        #
        #   * **concat demuxer** (default, or audio-less source) —
        #     stream-copies per-segment video + audio into one file.
        #     Fast, lossless for video, but preserves per-segment AAC
        #     priming (~21ms per segment at 48kHz) which accumulates as
        #     A/V drift on multi-segment outputs (10 segments → ~170ms).
        #
        #   * **concat filter** (``gapless_concat=True`` + audio source)
        #     — re-encodes through a single PCM pipeline so priming is
        #     added only once (not per-segment), giving gapless audio.
        #     Both video and audio are re-encoded (the concat filter's
        #     ``v=1:a=1`` joins both streams). The trade-off is one
        #     generation of video quality loss (H.264 → decode → H.264);
        #     for lossless video + gapless audio, use ``cut_then_encode``
        #     (one encode pass, but sacrifices frame accuracy via
        #     keyframe snap).
        #
        # Audio-less sources always use the demuxer path: there's no
        # priming to compensate for, so the concat filter would just
        # add a pointless re-encode of nothing.
        part_paths = [seg_dir / f"seg_{i:06d}.mp4" for i in range(n_segs)]

        # Gapless concat path: the tree builder inside
        # ``_run_gapless_segment_concat`` splits N parts into
        # max_inputs-sized groups, joins them pairwise through
        # intermediate files, and finally applies the user-selected
        # codecs on the last pass. No single-pass command exceeds the
        # Windows 32K limit no matter how many segments were generated.
        # The demuxer path is used when ``gapless_concat`` is off, or
        # when the source has no audio (nothing to re-encode).
        if options.gapless_concat and options.source_has_audio and n_segs > 1:
            _c._run_gapless_segment_concat(
                output_path,
                part_paths,
                vcodec,
                vcodec_opts,
                total_duration=total_duration,
                progress_callback=progress_callback,
                cancel_callback=cancel_callback,
                options=options,
                manifest=manifest,
            )
        else:
            _c._run_final_concat(
                seg_dir,
                output_path,
                part_paths,
                total_duration=total_duration,
                progress_callback=progress_callback,
                cancel_callback=cancel_callback,
                label="segment concat",
                options=options,
                # A mixed part set (some resumed, some freshly
                # encoded) can carry per-segment AAC priming offsets that
                # accumulate into audible seam clicks under -c copy; the
                # aresample re-encode re-anchors the audio. A fresh-only
                # set shares one encode session's timebase — no correction.
                audio_resync=bool(skipped) and options.source_has_audio,
            )
        logger.info(f"Successfully created output: {output_path}")

        # Cleanup on success
        shutil.rmtree(seg_dir, ignore_errors=True)

    except Exception:
        # On failure: keep segments for resume
        logger.info(f"Segments kept in {seg_dir} for resume on next run")
        raise
