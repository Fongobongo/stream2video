"""Silence cache read/write (final cache and resume checkpoints)."""

import json
import logging
import os
import tempfile
from pathlib import Path

from stream2video.silence.parser import SilenceSegment

logger = logging.getLogger(__name__)


def _get_wav_cache_path(video_path: Path, output_dir: Path) -> Path:
    return output_dir / f"{video_path.stem}_audio.wav"


def _get_wav_verified_path(wav_path: Path) -> Path:
    """Sidecar marking that `wav_path` passed the sample-verify PTS check."""
    return wav_path.with_name(wav_path.name + ".verified")


def _mark_wav_verified(wav_path: Path) -> None:
    """Drop the verified sidecar next to `wav_path` (best-effort)."""
    try:
        _get_wav_verified_path(wav_path).write_text("ok\n", encoding="utf-8")
    except OSError as e:
        logger.debug(f"Could not write WAV verified marker: {e}")


def clear_wav_verified(wav_path: Path) -> None:
    """Invalidate the WAV cache after a failed verification / partial extract."""
    try:
        _get_wav_verified_path(wav_path).unlink(missing_ok=True)
    except OSError:
        pass


def _is_wav_cache_valid(wav_path: Path, video_path: Path) -> bool:
    """WAV cache is valid if it exists, is at least as new as the source,
    and has passed the broken-PTS sample verification.

    The verified sidecar matters: a cancelled run can leave a freshly
    extracted (fresh mtime) but never-verified WAV on disk. Without the
    marker every subsequent run would trust that WAV and, on a
    broken-timestamp source, silently produce shifted cut points
    forever. When the sidecar is missing the caller re-runs the cheap
    60s sample-verify before trusting the cache.
    """
    if not wav_path.exists():
        return False
    if not _get_wav_verified_path(wav_path).exists():
        return False
    return wav_path.stat().st_mtime >= video_path.stat().st_mtime


def _get_cache_path(video_path: Path, output_dir: Path) -> Path:
    return output_dir / f"{video_path.stem}_silence_cache.json"


def _save_cache(
    cache_path: Path,
    video_path: Path,
    segments: list[SilenceSegment],
    config: dict,
    *,
    indent: int | None = 2,
    fsync: bool = True,
) -> None:
    """Atomically write a silence cache to `cache_path`.

    The temp file is created in the same directory as `cache_path` so
    `os.replace` is atomic on the same filesystem. Parent directories
    are created if needed.

    Args:
        indent: JSON indent level (None for compact, default 2).
        fsync: Whether to fsync after writing (True for final cache,
               False for ephemeral resume checkpoints).

    Note: with ``fsync=False`` (resume checkpoint path), a kernel crash
    between ``json.dump`` and ``os.replace`` could leave the previous
    file's bytes partially overwritten on disk. ``os.replace`` is still
    atomic for the rename so the *name* always points at a complete file
    or the old one — but the data is not fsync'd so on-disk contents may
    lag. Resume cache is best-effort by design; the canonical final
    cache (``fsync=True``) is the durable source of truth.
    """
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "source": video_path.name,
        "config": {
            "threshold": config.get("threshold"),
            "min_silence": config.get("min_silence"),
            "margin": config.get("margin"),
        },
        "segments": [{"start": s.start, "end": s.end} for s in segments],
    }
    fd, tmp_path = tempfile.mkstemp(
        dir=cache_path.parent, prefix=f".{cache_path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent)
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        os.replace(tmp_path, cache_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def save_silence_cache(
    video_path: Path,
    segments: list[SilenceSegment],
    output_dir: Path,
    config: dict,
) -> None:
    cache_path = _get_cache_path(video_path, output_dir)
    _save_cache(cache_path, video_path, segments, config)
    logger.info(f"Silence cache saved to {cache_path}")


def load_silence_cache(
    video_path: Path,
    output_dir: Path,
    config: dict,
) -> list[SilenceSegment] | None:
    """Load the final silence cache for `video_path` if fresh and config-matching.

    Convenience wrapper around `_load_silence_cache_from_path` that
    constructs the canonical final cache path. Returns margin-applied
    segments (margin is part of the cache key, so any hit was built
    with this exact margin).
    """
    cache_path = _get_cache_path(video_path, output_dir)
    segments = _load_silence_cache_from_path(cache_path, video_path, config)
    if segments is not None:
        logger.info(f"Loaded {len(segments)} silence segments from cache")
    return segments


def _load_silence_cache_from_path(
    cache_path: Path,
    video_path: Path,
    config: dict,
) -> list[SilenceSegment] | None:
    """Load and validate a silence cache file at `cache_path`.

    Returns the margin-applied segments on success, ``None`` on any
    failure: file missing, source newer than cache, malformed JSON,
    config mismatch, or malformed segments. The final cache stores
    margin-applied results; for resume, the caller uses the raw
    progressive_segments directly (no cache load).
    """
    if not cache_path.exists():
        return None
    if cache_path.stat().st_mtime < video_path.stat().st_mtime:
        logger.info(f"Silence cache outdated (source file newer): {cache_path.name}")
        return None
    try:
        with open(cache_path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Could not read silence cache: {e}")
        return None
    # Cache key comparison (P2.14): exact ``!=`` on the float values
    # stored in the JSON cache vs the runtime config. This is
    # intentional — a tolerance-based comparison would let a user's
    # ``threshold: -30.0001`` (typed into the GUI) silently match a
    # cache built with ``threshold: -30.0`` (the slider default),
    # producing cuts from a different detection than the user just
    # requested. The trade-off is that hand-editing the YAML with
    # ``2.0000001`` invalidates the cache, but that's the safer
    # failure mode (re-detect is cheap; wrong cuts are not).
    # UI sliders write rounded floats (1 decimal) so this never bites
    # the common path; only affects hand-edited configs.
    for key in ("threshold", "min_silence", "margin"):
        if data.get("config", {}).get(key) != config.get(key):
            logger.info(f"Silence cache ignored: config mismatch ({key})")
            return None
    try:
        return [SilenceSegment(s["start"], s["end"]) for s in data["segments"]]
    except (KeyError, TypeError, ValueError) as e:
        logger.warning(f"Invalid silence cache: {e}")
        return None
