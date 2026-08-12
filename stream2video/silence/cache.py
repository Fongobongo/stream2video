"""Silence cache read/write (final cache and resume checkpoints)."""

import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path

from stream2video.silence.parser import SilenceSegment

logger = logging.getLogger(__name__)


def build_resume_cache_path(video_path: Path, output_dir: Path) -> Path:
    """Canonical resume-checkpoint path shared by the CLI and GUI.

    The filename embeds a hash of the resolved source path so two videos
    that share a stem but live in different directories never share one
    resume file. Prior to this helper the CLI hashed the path while the
    GUI didn't, so neither front-end ever saw the other's checkpoints.
    """
    path_key = hashlib.sha256(str(video_path.resolve()).encode("utf-8", "replace")).hexdigest()[:8]
    return output_dir / f"{video_path.stem}_{path_key}_silence_cache.json.resume"


def resume_inuse_path(resume_path: Path) -> Path:
    """Sidecar path a resume file is moved to *while being consumed*.

    ``detect_silence`` used to unlink the resume file right after loading
    it; a crash between that unlink and the first throttled checkpoint
    lost hours of detection progress. Renaming to ``.inuse`` keeps the
    data on disk until the run proves it can write fresh checkpoints.
    """
    return resume_path.with_name(resume_path.name + ".inuse")


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
    probe_position: float | None = None,
) -> None:
    """Atomically write a silence cache to `cache_path`.

    The temp file is created in the same directory as `cache_path` so
    `os.replace` is atomic on the same filesystem. Parent directories
    are created if needed.

    Args:
        indent: JSON indent level (None for compact, default 2).
        fsync: Whether to fsync after writing (True for final cache,
               False for ephemeral resume checkpoints).
        probe_position: Source-time position (seconds) ffmpeg had decoded
               up to when this checkpoint was written. Recorded so a
               resume with ZERO detected segments still restarts from the
               checkpoint instead of from t=0 (fix-plan #3b).

    Note: with ``fsync=False`` (resume checkpoint path), a kernel crash
    between ``json.dump`` and ``os.replace`` could leave the previous
    file's bytes partially overwritten on disk. ``os.replace`` is still
    atomic for the rename so the *name* always points at a complete file
    or the old one — but the data is not fsync'd so on-disk contents may
    lag. Resume cache is best-effort by design; the canonical final
    cache (``fsync=True``) is the durable source of truth.
    """
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        src_stat = video_path.stat()
        src_size: int | None = src_stat.st_size
    except OSError:
        src_size = None
    data: dict = {
        "source": video_path.name,
        "source_size": src_size,
        "config": {
            "threshold": config.get("threshold"),
            "min_silence": config.get("min_silence"),
            "margin": config.get("margin"),
        },
        "segments": [{"start": s.start, "end": s.end} for s in segments],
    }
    if probe_position is not None:
        data["probe_position"] = probe_position
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
    failure: file missing, source newer than cache, source *identity*
    mismatch (name or size), malformed JSON, config mismatch, or
    malformed segments. The final cache stores margin-applied results;
    for resume, the caller uses the raw progressive_segments directly
    (no cache load).
    """
    try:
        cache_mtime = cache_path.stat().st_mtime
        src_stat = video_path.stat()
    except OSError as e:
        # Either file vanished between the caller's existence check and
        # here (AV scan, user delete) — treat as a cache miss, not a crash.
        logger.info(f"Silence cache stat failed ({e}); treating as miss")
        return None
    if cache_mtime < src_stat.st_mtime:
        logger.info(f"Silence cache outdated (source file newer): {cache_path.name}")
        return None
    try:
        # encoding="utf-8" matches _save_cache; without it a source name
        # containing non-ASCII chars raises UnicodeDecodeError on cp1251
        # Windows locales instead of a graceful cache miss.
        with open(cache_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning(f"Could not read silence cache: {e}")
        return None
    # Source identity (fix-plan #8): the cache filename only embeds the
    # stem, so two different videos named ``a.mp4`` in the same output
    # dir would otherwise share one cache and cut each other's content.
    # mtime is also forgeable (robocopy /COPYALL preserves it), so check
    # the recorded name AND byte size against the actual source.
    recorded_name = data.get("source")
    if recorded_name is not None and recorded_name != video_path.name:
        logger.info(
            f"Silence cache ignored: source name mismatch "
            f"(cached {recorded_name!r}, actual {video_path.name!r})"
        )
        return None
    recorded_size = data.get("source_size")
    if recorded_size is not None and recorded_size != src_stat.st_size:
        logger.info(
            f"Silence cache ignored: source size mismatch "
            f"(cached {recorded_size}, actual {src_stat.st_size})"
        )
        return None
    # Cache key comparison (P2.14): compare the *numeric* values, not
    # raw ``!=``. JSON round-trips lose the int/float distinction
    # (``-30`` loads back as ``-30``, a YAML default ``-30.0`` stays
    # float), and an exact-Python-equality ``!=`` falsely invalidates
    # the cache on that representation difference alone. A tolerance
    # comparison would have the opposite problem: a user-typed
    # ``-30.0001`` silently matching a ``-30.0`` cache. Converting both
    # sides to ``float`` first gets the strict-equality semantic we
    # want while being representation-agnostic.
    for key in ("threshold", "min_silence", "margin"):
        cached = data.get("config", {}).get(key)
        current = config.get(key)
        try:
            if float(cached) != float(current):  # type: ignore[arg-type]
                logger.info(f"Silence cache ignored: config mismatch ({key})")
                return None
        except (TypeError, ValueError):
            logger.info(f"Silence cache ignored: config mismatch ({key})")
            return None
    try:
        return [SilenceSegment(s["start"], s["end"]) for s in data["segments"]]
    except (KeyError, TypeError, ValueError) as e:
        logger.warning(f"Invalid silence cache: {e}")
        return None


def load_resume_probe_position(cache_path: Path, video_path: Path, config: dict) -> float | None:
    """Return the ``probe_position`` recorded in a resume checkpoint.

    Needed for fix-plan #3b: a checkpoint with ZERO detected segments
    (healthy source, hours already scanned) must resume from the probe
    position, not from t=0. Re-runs the same validation as
    ``_load_silence_cache_from_path`` so a stale/foreign/mismatched
    checkpoint never yields a seek point. Returns None when the field
    is absent (legacy checkpoint) or invalid — the caller then falls
    back to the last segment's end, or to a fresh scan.
    """
    try:
        cache_mtime = cache_path.stat().st_mtime
        src_stat = video_path.stat()
    except OSError:
        return None
    if cache_mtime < src_stat.st_mtime:
        return None
    try:
        with open(cache_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if data.get("source") is not None and data.get("source") != video_path.name:
        return None
    if data.get("source_size") is not None and data.get("source_size") != src_stat.st_size:
        return None
    for key in ("threshold", "min_silence", "margin"):
        try:
            if float(data.get("config", {}).get(key)) != float(config.get(key)):  # type: ignore[arg-type]
                return None
        except (TypeError, ValueError):
            return None
    pos = data.get("probe_position")
    if pos is None:
        return None
    try:
        return float(pos)
    except (TypeError, ValueError):
        return None
