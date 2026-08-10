"""Top-level ``detect_silence`` pipeline orchestrator."""

import logging
from collections.abc import Callable
from pathlib import Path

from stream2video import silence as _c
from stream2video.config import CONFIG_DEFAULTS
from stream2video.silence.parser import (
    _SAMPLE_VERIFY_DURATION,
    _SEGMENT_MATCH_TOLERANCE,
    _SILENCE_TIMEOUT,
    SilenceDetectionError,
    SilenceSegment,
    apply_margin,
)

logger = logging.getLogger(__name__)


def _stat_mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def detect_silence(
    video_path: Path,
    threshold: float = CONFIG_DEFAULTS["threshold"],
    min_silence: float = CONFIG_DEFAULTS["min_silence"],
    margin: float = CONFIG_DEFAULTS["margin"],
    output_dir: Path | None = None,
    progress_callback: Callable[[float], None] | None = None,
    cancel_callback: Callable[[], bool] | None = None,
    on_segment: Callable[[list[SilenceSegment]], None] | None = None,
    resume_cache_path: Path | None = None,
    timeout: int | None = None,
) -> list[SilenceSegment]:
    """
    Detect silence segments using ffmpeg silencedetect filter.

    When `output_dir` is provided, the audio is first extracted to a cached WAV
    file ({stem}_audio.wav) and silencedetect runs on the WAV. The first time
    the WAV is created (or whenever the source mtime is newer), a "sample-verify"
    pass runs silencedetect on the first `_SAMPLE_VERIFY_DURATION` seconds of
    the original video to detect sources with broken timestamps; on mismatch
    the WAV is invalidated and a full direct detection is run on the video.

    Args:
        video_path: Path to video file
        threshold: Silence threshold in dB (default from CONFIG_DEFAULTS,
                currently -30, range [-60, -5]). The default was historically
                -20 when called directly via the API; that diverged from the
                config-file / GUI value of -30, so an API call cut more
                aggressively than a config-file run on the same source. The
                defaults are now unified (see P1.7 in the fix plan).
        min_silence: Minimum silence duration in seconds (default from
                CONFIG_DEFAULTS, currently 2.0, range [0.1, 60]).
        margin: How much to shrink silence zones in seconds (default from
                CONFIG_DEFAULTS, currently 0.5, range [-3, 5]).
                Positive = shrink silence (keep more audio around phrases).
                Negative = expand silence (cut more aggressively).
                0 = no adjustment.
        output_dir: If provided, enable the cached-WAV pipeline. The WAV is created
                    on the first run (or whenever the source mtime is newer) and
                    re-used on subsequent runs. If None, silencedetect runs
                    directly on the video (A path, no WAV caching).
        progress_callback: Optional callback with progress fraction [0, 1]
        cancel_callback: Optional callable returning True to abort; checked while ffmpeg runs.
        on_segment: Optional callback invoked with a *snapshot* of the running
                    list of raw (pre-margin) segments every time a new
                    ``silence_end`` line arrives on ffmpeg's stderr. The
                    callback runs on ffmpeg's stderr drain thread, so callers
                    that need to touch a UI must wrap the work in their
                    framework's main-thread dispatch (e.g. ``self.after(0, ...)``
                    in Tkinter). Used by the GUI to keep a near-real-time
                    preview in sync with the running detection. On resume,
                    the callback is also fired once at the start with the
                    pre-seeded initial list so the GUI's overlay is correct
                    from the moment the pipeline begins. Not invoked for
                    sample-verify passes (which are batch and discarded).
        resume_cache_path: Path to a resume cache file. If set and the file
                    is fresh (mtime >= source mtime) and config-matching,
                    detection picks up from the last throttled checkpoint
                    written by a previous cancelled/crashed run. The file
                    is unlinked at the start of detection so a retry
                    within the same call doesn't re-load it. The CLI and
                    GUI both pass ``{output_dir}/{stem}_silence_cache.json.resume``;
                    callers that want a custom location can pass any
                    Path. Throttled checkpoints
                    are written when `resume_cache_path` is set; a new
                    run that uses resume will overwrite the file as it
                    progresses, so leave it in place for cancellation /
                    crash recovery and let the GUI unlink it on success.

    Returns:
        List of SilenceSegment objects (margin applied)
    """
    if not video_path.exists():
        raise SilenceDetectionError(f"Video file not found: {video_path}")

    if not -60 <= threshold <= -5:
        raise ValueError(f"Threshold must be in range [-60, -5], got {threshold}")

    if not 0.1 <= min_silence <= 60:
        raise ValueError(f"Min silence must be in range [0.1, 60], got {min_silence}")

    if not -3 <= margin <= 5:
        raise ValueError(f"Margin must be in range [-3, 5], got {margin}")

    # P3.4: timeout override from config. None = use module-level fallback.
    effective_timeout = timeout if timeout is not None else _SILENCE_TIMEOUT

    current_config = {
        "threshold": threshold,
        "min_silence": min_silence,
        "margin": margin,
    }

    # Resume cache: load and validate, then unlink so a retry inside
    # this call doesn't re-load it. The file is ephemeral — if the
    # detection is cancelled, the next run reads whatever was last
    # throttled-saved. On success, the final cache is the source of
    # truth and the resume file is the GUI's responsibility to clean
    # up.
    initial_segments: list[SilenceSegment] = []
    resume_from: float | None = None
    if resume_cache_path is not None:
        loaded = _c._load_silence_cache_from_path(resume_cache_path, video_path, current_config)
        if loaded is not None:
            initial_segments = loaded
            resume_from = initial_segments[-1].end if initial_segments else None
            if resume_from is not None:
                logger.info(
                    f"Resuming from resume cache: {len(initial_segments)} segments, "
                    f"seek to t={resume_from:.2f}s"
                )
            else:
                logger.info("Resume cache has 0 segments — starting from t=0 with checkpointing on")
        else:
            logger.info("No valid resume cache — starting fresh")
        # Unlink unconditionally — if it was stale/missing, this is a
        # no-op (missing_ok=True guards against FileNotFoundError); if
        # it was valid, we don't want a retry to re-load it.
        try:
            resume_cache_path.unlink(missing_ok=True)
        except OSError as e:
            logger.debug(f"Resume cache unlink failed (will be retried next run): {e}")

    duration = _c._probe_duration(video_path)

    if output_dir is not None:
        wav_path = _c._get_wav_cache_path(video_path, output_dir)
        if _c._is_wav_cache_valid(wav_path, video_path):
            logger.debug(f"Using cached WAV: {wav_path}")
            segments = _c._run_silencedetect(
                wav_path,
                threshold,
                min_silence,
                duration,
                progress_callback,
                cancel_callback,
                "WAV cache",
                on_segment=on_segment,
                initial_segments=initial_segments,
                resume_from=resume_from,
                resume_save_path=resume_cache_path,
                resume_save_config=current_config,
                timeout=effective_timeout,
            )
        elif wav_path.exists() and wav_path.stat().st_mtime >= _stat_mtime(video_path):
            # The WAV exists and is fresh but carries no ``.verified``
            # sidecar — it was extracted by a run that was cancelled or
            # crashed before the broken-PTS sample-verify could run (or
            # was extracted by an older version without the marker).
            # mtime alone cannot prove the WAV's timestamps match the
            # source, and trusting an unverified WAV on a broken-PTS
            # source would silently shift every cut point. Run the cheap
            # 60s sample-verify once here; on success mark the WAV so
            # subsequent runs skip straight to the fast path above.
            logger.info(
                f"Cached WAV has no verified marker — running one-time "
                f"sample-verify before reusing it: {wav_path.name}"
            )
            segments_A_sample = _c._run_silencedetect(
                video_path,
                threshold,
                min_silence,
                duration,
                None,
                cancel_callback,
                "video (sample)",
                duration_limit=_SAMPLE_VERIFY_DURATION,
                timeout=effective_timeout,
            )
            segments_D_sample = _c._run_silencedetect(
                wav_path,
                threshold,
                min_silence,
                duration,
                None,
                cancel_callback,
                "WAV (sample)",
                duration_limit=_SAMPLE_VERIFY_DURATION,
                timeout=effective_timeout,
            )
            if _c._sample_segments_match(
                segments_D_sample, segments_A_sample, _SEGMENT_MATCH_TOLERANCE
            ):
                _c._mark_wav_verified(wav_path)
                logger.info(
                    f"Sample-verify passed for cached WAV — marking verified: {wav_path.name}"
                )
                segments = _c._run_silencedetect(
                    wav_path,
                    threshold,
                    min_silence,
                    duration,
                    progress_callback,
                    cancel_callback,
                    "WAV cache",
                    on_segment=on_segment,
                    initial_segments=initial_segments,
                    resume_from=resume_from,
                    resume_save_path=resume_cache_path,
                    resume_save_config=current_config,
                    timeout=effective_timeout,
                )
            else:
                logger.warning(
                    f"Sample-verify failed for previously-cached WAV "
                    f"(D-sample: {len(segments_D_sample)}, "
                    f"A-sample: {len(segments_A_sample)} segment starts in first "
                    f"{_SAMPLE_VERIFY_DURATION:.0f}s). Source may have broken "
                    f"timestamps — re-extracting and falling back if needed."
                )
                wav_path.unlink(missing_ok=True)
                _c.clear_wav_verified(wav_path)
                if resume_cache_path is not None:
                    # The resume checkpoints were written against the
                    # broken WAV timeline — continuing from them would
                    # poison the direct result.
                    resume_cache_path.unlink(missing_ok=True)
                    initial_segments = []
                    resume_from = None
                segments = _c._run_silencedetect(
                    video_path,
                    threshold,
                    min_silence,
                    duration,
                    progress_callback,
                    cancel_callback,
                    "video",
                    on_segment=on_segment,
                    initial_segments=initial_segments,
                    resume_from=resume_from,
                    resume_save_path=resume_cache_path,
                    resume_save_config=current_config,
                    timeout=effective_timeout,
                )
        else:
            # Fresh WAV — report extraction as 0..15% of the Silence phase
            # so the thin bar moves during the 15GB extract (otherwise 0%).
            def _wav_prog(f: float) -> None:
                if progress_callback is not None:
                    progress_callback(max(0.0, min(1.0, f)) * 0.15)

            _c._extract_audio_wav(
                video_path,
                wav_path,
                cancel_callback,
                timeout=effective_timeout,
                progress_callback=_wav_prog if progress_callback is not None else None,
                duration=duration,
            )
            if progress_callback is not None:
                # Keep bar at 15% until silencedetect starts producing
                progress_callback(0.15)

            # The WAV was just (re-)extracted with -copyts — its PTS
            # timeline matches the source video exactly, so the
            # resume context (initial_segments + resume_from) is still
            # valid. Pass it through so a cancelled/crashed run can
            # pick up from the last checkpoint instead of starting
            # from t=0 on a multi-hour source. Both the WAV and the
            # .resume file live in source-time coordinates thanks to
            # -copyts on the extraction side.
            # Map WAV silencedetect 0..1 to 15..85% of the Silence phase
            # (extraction is 0..15 illustration above, sample-verify is
            # hidden). When resuming, offset already handled by progress_divisor.
            def _wav_silence_prog(f: float) -> None:
                if progress_callback is not None:
                    progress_callback(0.15 + max(0.0, min(1.0, f)) * 0.70)

            segments_D = _c._run_silencedetect(
                wav_path,
                threshold,
                min_silence,
                duration,
                _wav_silence_prog if progress_callback is not None else None,
                cancel_callback,
                "WAV",
                on_segment=on_segment,
                initial_segments=initial_segments,
                resume_from=resume_from,
                resume_save_path=resume_cache_path,
                resume_save_config=current_config,
                timeout=effective_timeout,
            )
            # Sample-verify must NOT use the user-facing progress_callback:
            # it runs for a fixed _SAMPLE_VERIFY_DURATION window and would
            # fill the progress bar to 100% over 60s while the real D
            # detection (which has no progress callback) just finished.
            # The bar would either freeze at 100% (verify pass) or jump back
            # to 0% on the A-fallback (verify fail). Keep verify invisible.
            segments_A_sample = _c._run_silencedetect(
                video_path,
                threshold,
                min_silence,
                duration,
                None,
                cancel_callback,
                "video (sample)",
                duration_limit=_SAMPLE_VERIFY_DURATION,
                timeout=effective_timeout,
            )
            # Pad the D-sample clip by one ``min_silence`` window: a
            # segment starting just inside ``_SAMPLE_VERIFY_DURATION``
            # must also appear in the ``-t``-clipped A-sample, but the
            # silencedetect ``duration=min_silence`` integration window
            # means the filter raises ``silence_start`` up to
            # ``min_silence`` seconds after the real energy dip — a
            # segment whose true start is a hair below the boundary can
            # legitimately be reported just *past* it on one pass and
            # inside it on the other. Dropping the tail window removes
            # the false positives that previously triggered a pointless
            # full re-detect on a healthy source.
            _pad = max(0.0, min_silence)
            _pad_active = _pad < _SAMPLE_VERIFY_DURATION
            segments_D_sample = (
                [s for s in segments_D if s.start < _SAMPLE_VERIFY_DURATION - _pad]
                if _pad_active
                else list(segments_D)
            )
            if _c._sample_segments_match(
                segments_D_sample, segments_A_sample, _SEGMENT_MATCH_TOLERANCE
            ):
                logger.debug(
                    f"Sample-verify passed (D-sample: {len(segments_D_sample)} starts within "
                    f"first {_SAMPLE_VERIFY_DURATION:.0f}s"
                    + (f" padded by {_pad:.1f}s" if _pad_active else "")
                    + f" match A-sample: {len(segments_A_sample)}) — using D result, "
                    f"keeping WAV cache"
                )
                _c._mark_wav_verified(wav_path)
                segments = segments_D
            else:
                logger.warning(
                    f"Sample-verify failed (D-sample: {len(segments_D_sample)}, "
                    f"A-sample: {len(segments_A_sample)} segment starts in first "
                    f"{_SAMPLE_VERIFY_DURATION:.0f}s, tolerance={_SEGMENT_MATCH_TOLERANCE}s). "
                    f"Source may have broken timestamps — falling back to full direct "
                    f"detection. WAV cache invalidated."
                )
                wav_path.unlink(missing_ok=True)
                _c.clear_wav_verified(wav_path)
                # The throttled resume checkpoints above were written
                # against the broken WAV's timeline during the D pass —
                # before this verify pass ran. Feeding them into the
                # direct-video fallback would produce a cut plan that is
                # half broken-PTS, half correct. Drop them and restart
                # the direct detection from t=0.
                if resume_cache_path is not None:
                    resume_cache_path.unlink(missing_ok=True)
                initial_segments = []
                resume_from = None

                def _direct_prog(f: float) -> None:
                    # The WAV silencedetect above already consumed
                    # 0.15..0.85 of the phase bar; report the direct
                    # fallback's 0..1 fraction inside the same slice so
                    # the overall bar never regresses.
                    if progress_callback is not None:
                        progress_callback(0.15 + max(0.0, min(1.0, f)) * 0.70)

                segments = _c._run_silencedetect(
                    video_path,
                    threshold,
                    min_silence,
                    duration,
                    _direct_prog if progress_callback is not None else None,
                    cancel_callback,
                    "video",
                    on_segment=on_segment,
                    initial_segments=initial_segments,
                    resume_from=resume_from,
                    resume_save_path=resume_cache_path,
                    resume_save_config=current_config,
                    timeout=effective_timeout,
                )
            if progress_callback is not None:
                progress_callback(0.85)
    else:
        segments = _c._run_silencedetect(
            video_path,
            threshold,
            min_silence,
            duration,
            progress_callback,
            cancel_callback,
            "video",
            on_segment=on_segment,
            initial_segments=initial_segments,
            resume_from=resume_from,
            resume_save_path=resume_cache_path,
            resume_save_config=current_config,
            timeout=effective_timeout,
        )

    segments = apply_margin(segments, margin, duration)

    if progress_callback is not None:
        # Finish 85..100% slice (verify/fallback already at 85) so bar
        # hits 100% before the controller flips to Cutting 3/4.
        progress_callback(1.0)

    if not segments:
        logger.info("No silence segments detected (video may have no audio track)")

    return segments
