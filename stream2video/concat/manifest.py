"""Resume manifest handling (P0.6).

A manifest is written next to the segment/batch working directory the
first time a run starts. On resume, the manifest is loaded and validated
against the current run's parameters (source identity + encoder +
quality + pipeline version + keep segments). A mismatch invalidates the
working dir so old artifacts from an incompatible run cannot be reused.

Source identity is (path, size, mtime_ns, head_tail_hash). ``head_tail_hash``
is a SHA-256 over the first + last 1 MiB of the file — O(1) in file size
but enough to catch the adversarial "user re-downloaded the same stream,
the file has identical size AND the copy tool preserved mtime" case
(robocopy /COPYALL, rclone -M). A full-file hash is intentionally avoided
(P0 audit: 6h of reads for a resume check is unacceptable).
"""

import json
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

PIPELINE_VERSION = 5  # bump when the on-disk segment/chunk format changes or a new
# identity key is added (v4: +output_fps, gapless_concat, source_has_audio;
# v5: +source.head_tail_hash, see _source_identity)


def _manifest_path(work_dir: Path) -> Path:
    return work_dir / "_manifest.json"


_HEAD_TAIL_BYTES = 1024 * 1024  # 1 MiB at each end; see _source_identity


def _head_tail_hash(path: Path) -> str:
    """SHA-256 of the file's first + last 1 MiB.

    Cheap enough for resume identity (two 1 MiB reads vs a full-file
    hash) but catches a same-size same-mtime content swap. The hash is
    order-sensitive: ``H(head || tail)`` — two files that share a head
    but differ at the tail hash differently, which is exactly the case
    a re-downloaded stream flips (new outro = new tail).
    """
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read(_HEAD_TAIL_BYTES))
        try:
            f.seek(max(0, path.stat().st_size - _HEAD_TAIL_BYTES))
        except OSError:
            pass  # file shrank between reads — first read covered all of it
        h.update(f.read(_HEAD_TAIL_BYTES))
    return h.hexdigest()


def _source_identity(video_path: Path) -> dict:
    """Snapshot (path, size, mtime_ns, head_tail_hash) so resume detects source changes.

    Uses the absolute path so renaming the file (same content) doesn't
    silently reuse segments encoded against a different filename -- the
    concat list references segments by their position in the run, so a
    path rename invalidates the work dir deliberately.

    ``head_tail_hash`` (fix-plan #24): catches the case where a copy
    tool preserved both mtime and size but the *content* changed —
    re-downloads of the same stream land exactly here (same duration →
    same size; robocopy preserves mtime).
    """
    st = video_path.stat()
    try:
        hth = _head_tail_hash(video_path)
    except OSError as e:
        # Unreadable source: any segment reuse is unsafe — hash to a
        # sentinel that will never match a healthy run's hash.
        logger.warning(f"Could not hash {video_path.name} for manifest identity: {e}")
        hth = "unreadable"
    return {
        "path": str(video_path.resolve()),
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "head_tail_hash": hth,
    }


def _build_manifest(
    video_path: Path,
    keep_segments: list[tuple[float, float]],
    method: str,
    encoder: str,
    vcodec: str,
    vcodec_opts: list[str],
    video_quality: str,
    audio_quality: str,
    x264_preset: str,
    encoder_threads: str | int,
    *,
    output_fps: str = "source",
    gapless_concat: bool = False,
    source_has_audio: bool = True,
) -> dict:
    """Construct the manifest dict describing the current run's identity.

    ``output_fps`` / ``gapless_concat`` / ``source_has_audio`` must be part
    of the identity: they change the per-segment bytes (``-vf fps=...`` in
    the segment command, the tree-vs-demuxer join strategy) but are not
    captured by ``encoder`` / ``encoder_opts`` alone. Without them a user
    who changed only ``output_fps`` between two runs would silently reuse
    segments encoded at the *old* frame rate.
    """
    return {
        "pipeline_version": PIPELINE_VERSION,
        "source": _source_identity(video_path),
        "method": method,
        "encoder": encoder,
        "resolved_encoder": vcodec,  # may differ from `encoder` after fallback
        "encoder_opts": list(vcodec_opts),
        "video_quality": video_quality,
        "audio_quality": audio_quality,
        "x264_preset": x264_preset,
        "encoder_threads": encoder_threads,
        "output_fps": output_fps,
        "gapless_concat": bool(gapless_concat),
        "source_has_audio": bool(source_has_audio),
        "keep_segments": list(keep_segments),
        "keep_segments_total_duration": sum(e - s for s, e in keep_segments),
        "created_at": time.time(),
    }


def _write_manifest(work_dir: Path, manifest: dict) -> None:
    """Atomically write the manifest to ``work_dir/_manifest.json``.

    The temp file is created in the same directory so os.replace is atomic
    on the same filesystem. Parent exists because the caller already
    mkdir'd the work dir.
    """
    manifest_path = _manifest_path(work_dir)
    fd, tmp_path = tempfile.mkstemp(dir=work_dir, prefix=f".{manifest_path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, manifest_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _load_manifest(work_dir: Path) -> dict | None:
    """Read the manifest at ``work_dir/_manifest.json`` or return None."""
    manifest_path = _manifest_path(work_dir)
    if not manifest_path.exists():
        return None
    try:
        with open(manifest_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Resume manifest at {manifest_path} is unreadable: {e}")
        return None
    if not isinstance(data, dict):
        return None
    return data


def _validate_manifest(
    work_dir: Path,
    current: dict,
) -> bool:
    """Return True if the on-disk manifest matches the current run identity.

    A mismatch means the existing segments/chunks were produced by a
    different run (different source, encoder, quality, keep segments,
    or pipeline version) and must not be reused. Caller is responsible
    for wiping the work dir on False.
    """
    stored = _load_manifest(work_dir)
    if stored is None:
        # No manifest = the work dir predates the manifest system (or was
        # created by a crash before the manifest was written). Be safe
        # and treat as invalid.
        logger.info(f"Resume: no manifest in {work_dir}, treating as stale")
        return False
    for key in (
        "pipeline_version",
        "method",
        "encoder",
        "resolved_encoder",
        "encoder_opts",
        "video_quality",
        "audio_quality",
        "x264_preset",
        "encoder_threads",
        "output_fps",
        "gapless_concat",
        "source_has_audio",
    ):
        if stored.get(key) != current.get(key):
            logger.info(
                f"Resume: manifest mismatch on {key}: "
                f"stored={stored.get(key)!r} current={current.get(key)!r}"
            )
            return False
    # Source identity (path/size/mtime/head_tail_hash). ``head_tail_hash``
    # may be absent in manifests written by pipeline_version<5 — treat a
    # missing key as a mismatch (wipe and re-encode once) so a v4 manifest
    # never silently short-circuits a v5 identity check.
    stored_src = stored.get("source") or {}
    current_src = current.get("source") or {}
    for key in ("path", "size", "mtime_ns", "head_tail_hash"):
        if stored_src.get(key) != current_src.get(key):
            logger.info(
                f"Resume: source {key} changed: "
                f"stored={stored_src.get(key)!r} current={current_src.get(key)!r}"
            )
            return False
    # Keep segments must match exactly -- different cut points = different
    # output. Stored segments were written by json.dump (lists of lists);
    # compare as tuples for float safety.
    stored_segs = stored.get("keep_segments") or []
    current_segs = current.get("keep_segments") or []
    if len(stored_segs) != len(current_segs):
        logger.info(
            f"Resume: keep_segments length differs ({len(stored_segs)} vs {len(current_segs)})"
        )
        return False
    for (s1, e1), (s2, e2) in zip(stored_segs, current_segs, strict=True):
        if abs(float(s1) - float(s2)) > 1e-9 or abs(float(e1) - float(e2)) > 1e-9:
            logger.info(f"Resume: keep_segments differ: {stored_segs} vs {current_segs}")
            return False
    return True


def _ensure_fresh_work_dir(
    work_dir: Path,
    current_manifest: dict,
) -> None:
    """Validate ``work_dir`` against ``current_manifest``; wipe if stale.

    The wipe is destructive -- it removes the entire work dir including
    partial segments. Call this BEFORE the resume-skip loop in
    ``_run_segment_concat`` / ``_run_batch_concat`` so the per-file
    checks see only artifacts that belong to the current run.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    if _validate_manifest(work_dir, current_manifest):
        return
    if work_dir.exists() and any(work_dir.iterdir()):
        logger.info(
            f"Resume: invalidating work dir {work_dir} "
            f"(manifest mismatch or missing); re-encoding from scratch"
        )
        shutil.rmtree(work_dir, ignore_errors=True)
        work_dir.mkdir(parents=True, exist_ok=True)
    _write_manifest(work_dir, current_manifest)
