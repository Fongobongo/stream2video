"""Audio-only output paths (mp3/opus/aac/wav/flac) and the
concat-filter audio joiner."""

import logging
import shutil
from collections.abc import Callable
from pathlib import Path

from stream2video import concat as _c
from stream2video.concat.constants import _FINAL_CONCAT_TIMEOUT
from stream2video.concat.options import ConcatOptions, coerce_options
from stream2video.config import OUTPUT_FORMAT_SPECS
from stream2video.tools import ffmpeg_path

logger = logging.getLogger(__name__)


def _run_audio_concat_filter(
    output_path: Path,
    part_paths: list[Path],
    *,
    codec: str,
    extra_opts: list[str],
    total_duration: float,
    lossless: bool = False,
    progress_callback: Callable[[float], None] | None = None,
    cancel_callback: Callable[[], bool] | None = None,
    timeout: int = _FINAL_CONCAT_TIMEOUT,
    options: ConcatOptions | None = None,
    **legacy_kwargs: object,
) -> None:
    """Join audio parts via the ``concat`` filter (re-encode path).

    Used for containers whose muxer misreports duration on a concat-
    demuxer join (notably ``.flac`` — ffmpeg's flac muxer keeps the
    first segment's duration when stream-copying concat-demuxer input,
    producing a 2s file from two 2s segments). The concat filter
    decodes every part into PCM, concatenates the PCM buffers, and
    re-encodes once — reliable but not free.

    For lossless codecs (flac, wav) the re-encode round-trips
    bit-exact, so the lossless contract is preserved. Lossy codecs
    shouldn't reach here (the caller routes them through the concat
    demuxer instead), but if they did, the re-encode would add one
    generation of loss — acceptable as a fallback, not as policy.
    """
    options = coerce_options(options, legacy_kwargs)
    n = len(part_paths)
    if n == 0:
        raise _c.ConcatError("audio concat filter: no parts to join")

    # Bitrate knob: only meaningful for lossy codecs. Lossless formats
    # (flac/wav) ignore ``-b:a`` anyway; omitting it keeps the ffmpeg
    # command line readable in the log.
    bitrate_opts: list[str] = []
    if not lossless:
        bitrate_opts = _c._audio_bitrate_opts(options.audio_quality)

    # Build the -i inputs and the [N:a]concat=n=N:v=0:a=1 filter graph.
    inputs: list[str] = []
    for p in part_paths:
        inputs.extend(["-i", str(p)])
    chain = "".join(f"[{i}:a]" for i in range(n))
    graph = f"{chain}concat=n={n}:v=0:a=1[outa]"

    label_text = "audio concat filter"
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
            "[outa]",
            "-c:a",
            codec,
            # Bitrate first so ``extra_opts`` (already carrying e.g.
            # explicit sample-rate / channel pins) can override on a
            # later flag — ffmpeg takes the LAST occurrence of each option.
            *bitrate_opts,
            *_c._audio_opts(options.audio_quality),
            *extra_opts,
            str(output_path),
        ],
        progress_callback=_c._concat_progress_callback(progress_callback, total_duration),
        timeout=timeout,
        label=label_text,
        cancel_callback=cancel_callback,
        memory_monitor=_c._new_memory_monitor(options.memory_monitor_factory, label_text),
        stall_kill=options.stall_kill,
        stall_warning=options.stall_warning,
        low_process_priority=options.low_process_priority,
        rlimit_as_mb=options.rlimit_as_mb,
    )


def _run_audio_extract(
    video_path: Path,
    keep_segments: list[tuple[float, float]],
    output_path: Path,
    output_format: str,
    progress_callback: Callable[[float], None] | None = None,
    cancel_callback: Callable[[], bool] | None = None,
    options: ConcatOptions | None = None,
    **legacy_kwargs: object,
) -> None:
    """Extract audio-only output (mp3/opus/aac/wav/flac) from keep segments.

    The video stream is dropped entirely. Each keep segment is decoded
    via input-side ``-ss`` / ``-t`` (frame-accurate, same approach as
    ``_run_segment_concat``) and re-encoded into the chosen audio
    codec; the per-segment files are joined by the concat demuxer
    (lossless stream-copy join, no second encode pass).

    Resume: same manifest mechanism as the other paths — a mismatch in
    source / codec / quality / keep_segments wipes the work dir; each
    part file is ffprobe-validated so a partial crash artifact isn't
    reused.

    The per-segment encode is the *only* lossy step (for mp3/opus/aac);
    the concat demuxer join is lossless. AAC priming (~21 ms per
    segment) accumulates slightly across segments, mirroring the
    segment path's documented behaviour. For lossless formats (wav,
    flac) the priming is zero and the output is sample-accurate.

    ``audio_quality`` controls the bitrate for lossy formats via
    ``_audio_bitrate_opts()`` (high=256k, medium=192k, low=128k);
    ``source`` and wav/flac omit the bitrate knob.
    """
    options = coerce_options(options, legacy_kwargs)
    spec = OUTPUT_FORMAT_SPECS.get(output_format)
    if spec is None:
        # Should be unreachable: cut_and_concat validates output_format
        # before dispatching here. Kept as a defensive guard so a future
        # caller that bypasses cut_and_concat gets a clear error.
        raise _c.ConcatError(f"Unknown output_format {output_format!r}")
    codec = spec["codec"]
    ext = spec["ext"]
    lossless = bool(spec["lossless"])
    extra_opts: list[str] = list(spec["extra_opts"])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    total_duration = sum(e - s for s, e in keep_segments)
    n_segs = len(keep_segments)
    logger.info(
        f"audio_extract ({output_format}): {n_segs} segments, "
        f"{total_duration:.1f}s output, codec={codec}"
    )

    work_dir = output_path.parent / f"_{output_path.stem}_audio_{ext}"
    manifest = _c._build_manifest(
        video_path,
        keep_segments,
        "audio_extract",
        output_format,  # encoder slot — the audio "format" identifies the run
        codec,  # vcodec slot — the actual ffmpeg codec used
        [],  # vcodec_opts slot — audio has no encoder opts beyond -c:a
        "n/a",  # video_quality slot — not applicable to audio-only
        options.audio_quality,
        "n/a",  # x264_preset slot
        "auto",  # encoder_threads slot
    )
    _c._ensure_fresh_work_dir(work_dir, manifest)

    # Bitrate knob: only meaningful for lossy codecs. For wav/flac the
    # encoder ignores -b:a anyway, but we omit it to keep the ffmpeg
    # command line readable in the log.
    bitrate_opts: list[str] = []
    if not lossless:
        bitrate_opts = _c._audio_bitrate_opts(options.audio_quality)

    try:
        encoded_keep = 0.0
        skipped = 0

        # ``keep_segments`` are in the *detected* timeline that silence
        # detection produced. The WAV mirror is a plain PCM file, so its
        # timestamps start at 0 — even on a source with a non-zero
        # container ``start_time`` (OBS ``-itsoffset``) the detected
        # segments are in user-visible source-time coordinates. Input-side
        # ``-ss`` positions by file position, the same space — no
        # start_time compensation is needed (verified on ffmpeg 8.1.1;
        # see segment.py). The batch path compensates only its ``trim``
        # endpoints, which operate in ``-copyts`` PTS space.

        for i, (start, end) in enumerate(keep_segments):
            if cancel_callback and cancel_callback():
                raise _c.CancelledError("audio extract cancelled")

            dur = end - start
            seg_path = work_dir / f"seg_{i:06d}.{ext}"

            # Resume: skip already encoded segments. Same dual check as
            # _run_segment_concat: minimum size + ffprobe validity.
            # Audio segments use stream_type="a" — a video-stream probe
            # would reject any valid mp3/opus/aac/wav/flac chunk because
            # it has no video stream, defeating resume (P0 audit v0.3).
            # FULL decode (audit round 29 P4): header-level checks
            # cannot see mid-body corruption.
            if (
                seg_path.exists()
                and seg_path.stat().st_size >= options.min_part_bytes
                and _c._ffprobe_is_valid_media(seg_path, stream_type="a")
                and _c._ffprobe_duration_ok(seg_path, dur)
                and _c._ffmpeg_full_decode(seg_path, stream_type="a")
            ):
                skipped += 1
                encoded_keep += dur
                if progress_callback and total_duration > 0:
                    progress_callback(min(encoded_keep / total_duration * 0.9, 0.9))
                continue

            seg_prog = _c._seg_progress_callback(
                progress_callback, total_duration, encoded_keep, dur
            )

            label_text = f"audio segment {i} encode"
            _c._run_ffmpeg(
                [
                    ffmpeg_path(),
                    "-y",
                    "-loglevel",
                    "error",
                    "-progress",
                    "pipe:1",
                    "-ss",
                    f"{max(0.0, start):.6f}",
                    "-i",
                    str(video_path),
                    "-t",
                    f"{dur:.6f}",
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-c:a",
                    codec,
                    *bitrate_opts,
                    *_c._audio_opts(options.audio_quality),
                    *extra_opts,
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
                f"audio_extract: resumed {skipped}/{n_segs} already encoded, "
                f"encoded {n_segs - skipped}"
            )

        # Final join pass. Two strategies, picked by container:
        #
        #   * **concat demuxer** (mp3 / opus / aac-m4a / wav) — stream-
        #     copies the per-segment audio into one file, no re-encode,
        #     so lossy priming isn't re-added. Works because these
        #     containers' muxers honour the concat demuxer's "concatenate
        #     packets in order" semantics.
        #
        #   * **concat filter** (flac) — re-encodes through a single
        #     filter graph. The flac muxer misreports duration on a
        #     concat-demuxer join (it keeps the first segment's
        #     duration), so the lossless re-encode is the only reliable
        #     path. For flac the re-encode is lossless (flac → PCM → flac
        #     round-trips bit-exact), so this doesn't violate the
        #     "lossless" contract of the format.
        #
        # WAV would also work with the concat filter, but the demuxer
        # path is faster (no re-encode) and verified correct, so WAV
        # stays on the demuxer path.
        part_paths = [work_dir / f"seg_{i:06d}.{ext}" for i in range(n_segs)]
        if ext == "flac":
            _c._run_audio_concat_filter(
                output_path,
                part_paths,
                codec=codec,
                extra_opts=extra_opts,
                total_duration=total_duration,
                lossless=lossless,
                progress_callback=progress_callback,
                cancel_callback=cancel_callback,
                timeout=options.final_concat_timeout,
                options=options,
            )
        else:
            _c._run_final_concat(
                work_dir,
                output_path,
                part_paths,
                total_duration=total_duration,
                progress_callback=progress_callback,
                cancel_callback=cancel_callback,
                label="audio extract concat",
                options=options,
            )
        logger.info(f"Successfully created audio output: {output_path}")

        shutil.rmtree(work_dir, ignore_errors=True)

    except Exception:
        logger.info(f"Audio segments kept in {work_dir} for resume on next run")
        raise
