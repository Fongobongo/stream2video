"""Project directory resolution for per-video subdirectory support.

When `per_video_dir=True` is set in the config, all artifacts (downloaded
source, audio WAV, silence cache JSON, compressed output, log file, temp
segment dirs) for a given video are collected into a single subdirectory
named after the video's artifact stem, instead of living in the user's
flat `output_dir`.

The artifact stem is ``<stem>_<path-hash>``: the file stem plus a short
hash of the resolved source path. The hash is what makes two local files
that share a name but live in different directories (``/a/clip.mp4`` vs
``/b/clip.mp4``) independent — without it they would resolve to the same
project dir and overwrite each other's output and caches.

Layout comparison (per_video_dir=True):
    output_dir/
        <id>/                       # downloaded source: project dir = epochless yt-dlp id
            <id>.mp4                # downloaded source (renamed, epoch stripped)
            <id>_<path-hash>_audio.wav
            <id>_<path-hash>_silence_cache.json
            <id>_<path-hash>_compressed.mp4
            stream2video.log
            _<id>_<path-hash>_segments/   # temp, cleaned on success
            _<id>_<path-hash>_batch/      # temp, cleaned on success
        <stem>_<path-hash>/         # local file: project dir = stem + source-path hash
            <stem>_<path-hash>_audio.wav
            <stem>_<path-hash>_silence_cache.json
            <stem>_<path-hash>_compressed.mp4
            stream2video.log
            _<stem>_<path-hash>_segments/   # temp, cleaned on success
            _<stem>_<path-hash>_batch/      # temp, cleaned on success

Local input files are NEVER moved or copied — the source stays where the
user put it, but WAV / JSON / compressed / log / temp dirs all go into
the per-video subdir. Downloaded sources ARE moved (and epoch-stripped,
see ``_epochless``) so a re-run of the same URL reuses the project dir
and caches instead of forking per download.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# Max length of the displayed name in a Recent Projects row. Long
# filenames (e.g. "<id>_compressed_4_30_<more>") are truncated with
# an ellipsis so the column doesn't grow to fit the longest name.
# The full name is still available on hover via the tooltip.
# Kept as a module constant (not in gui.py) so tests can pin the
# behaviour without importing the GUI stack (which transitively
# pulls in Pillow via waveform.py).
RECENT_NAME_MAX = 24


def truncate_recent_name(text: str, max_len: int = RECENT_NAME_MAX) -> str:
    """Truncate ``text`` to ``max_len`` chars, appending an ellipsis if cut.

    If ``text`` is shorter than or equal to ``max_len``, it is returned
    unchanged. Otherwise the first ``max_len - 1`` characters are kept
    and "…" is appended (so the result is exactly ``max_len`` chars).
    Used by the GUI's Recent Projects row label.
    """
    if len(text) <= max_len:
        return text
    prefix = text[: max_len - 1]
    # A lone high-surrogate at the end of the prefix means we sliced
    # inside a surrogate pair — drop it so the ellipsis doesn't sit
    # next to an invalid half-character (Tk renders it as a box glyph).
    while prefix and 0xD800 <= ord(prefix[-1]) <= 0xDBFF:
        prefix = prefix[:-1]
    return prefix + "\u2026"


# Marker file written into every directory the application creates or
# claims as a project dir (``ensure_project_dir``). The GUI's Recent
# Projects "delete" button only ever rmtree()s a directory that carries
# this marker: ``recent_projects`` in settings.json is plain config data
# (hand-editable, and a swapped settings file can put any path in it),
# so a bare path string must never be trusted as a deletable target.
# The marker is what turns "a path that was once stored" into "a
# directory this application created and owns".
PROJECT_MARKER_FILENAME = ".stream2video_project.json"

# Fixed payload. ``app`` / ``kind`` are validated on read so a
# coincidental file with the same name in an unrelated directory is
# not treated as a project marker; ``version`` is reserved for future
# migrations of the marker format.
PROJECT_MARKER_PAYLOAD: dict[str, object] = {
    "app": "stream2video",
    "kind": "project_dir",
    "version": 1,
}


def mark_project_dir(path: Path) -> None:
    """Write the project marker into ``path`` (best-effort).

    Called right after a directory is created/claimed as a project dir.
    The write is deliberately non-fatal: if it fails, processing still
    works — the only consumer of the marker is the GUI's Recent Projects
    delete button, which degrades to "refuse to delete and drop the
    entry" (a safe failure mode) when the marker is missing.
    """
    try:
        (path / PROJECT_MARKER_FILENAME).write_text(
            json.dumps(PROJECT_MARKER_PAYLOAD, indent=2),
            encoding="utf-8",
        )
    except OSError as e:
        logger.warning("Could not write project marker in %s: %s", path, e)


def is_marked_project_dir(path: Path) -> bool:
    """True if ``path`` is a directory carrying the app's project marker.

    The marker file's content is validated (not just its name) so a
    stray file in an unrelated directory can't make that directory
    deletable. Returns False on any OSError / unreadable marker — the
    safe direction for the delete gate.
    """
    marker = path / PROJECT_MARKER_FILENAME
    try:
        if not marker.is_file():
            return False
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (
        isinstance(data, dict)
        and data.get("app") == PROJECT_MARKER_PAYLOAD["app"]
        and data.get("kind") == PROJECT_MARKER_PAYLOAD["kind"]
    )


# Standard user-profile subdirectories that must never be recursively
# deleted even if they somehow carry a marker (defence in depth; the
# marker check above is the primary gate).
_USER_SUBDIRS = (
    "Desktop",
    "Documents",
    "Downloads",
    "Pictures",
    "Music",
    "Videos",
    "AppData",
    "Application Data",
)


def _user_home() -> Path:
    """The current user's home directory (seam for tests).

    Routed through a module-level function instead of calling
    ``Path.home()`` inline: tests monkeypatch this function instead of
    a stdlib classmethod, whose internals CPython keeps re-plumbing
    between releases (the forge-marker test that patches ``Path.home``
    stopped intercepting on a newer 3.13 patch release, so the guard
    silently used the REAL home and allowed the delete). Patching a
    module seam is version-stable and makes the test's intent explicit.
    """
    return Path.home()


def is_sensitive_delete_target(path: Path) -> bool:
    """True if ``path`` must never be passed to ``shutil.rmtree``.

    Blocks filesystem roots, the user's home directory, the standard
    user-profile subdirectories, and the application's own directory
    (deleting the install/config dir would remove the settings file
    the GUI is currently writing to). Comparisons are case-insensitive
    on Windows via ``os.path.normcase`` and short-name-insensitive via
    ``os.path.realpath`` (a Windows 8.3 alias such as ``RUNNER~1``
    must hit the same blocklist entry as the long profile path).
    """
    try:
        norm = os.path.normcase(os.path.realpath(str(path)))
    except (OSError, ValueError):
        return True
    home = os.path.normcase(os.path.realpath(str(_user_home())))
    blocked = {
        os.path.normcase(os.path.realpath(str(Path(path.anchor)))),
        home,
        os.path.normcase(os.path.realpath(str(Path(__file__).parent.parent))),
    }
    for sub in _USER_SUBDIRS:
        blocked.add(os.path.join(home, os.path.normcase(sub)))
    return norm in blocked


def validate_project_delete(path: Path | str) -> tuple[bool, str]:
    """Decide whether ``path`` may be recursively deleted from the GUI.

    Returns ``(True, "")`` only when ``path`` resolves to an existing
    directory that (a) is not a sensitive target (drive root, home,
    user-profile subdirs, the app's own directory) and (b) carries the
    app's project marker — i.e. it was actually created/claimed as a
    project dir by this application. Any other path is refused with a
    human-readable reason; the caller must NOT call ``rmtree`` in that
    case (it may drop the entry from Recent Projects instead).

    ``recent_projects`` is untrusted config data (hand-editable, and a
    swapped settings.json can put any path in it), so this check — not
    the stored string — is the boundary for destructive actions.
    """
    try:
        resolved = Path(path).resolve()
    except OSError:
        resolved = Path(os.path.abspath(str(path)))
    if not resolved.is_dir():
        return False, "directory does not exist"
    if is_sensitive_delete_target(resolved):
        return False, "refusing to delete a system or user directory"
    if not is_marked_project_dir(resolved):
        return False, "not a project directory created by this application"
    return True, ""


def source_path_key(video_path: Path) -> str:
    """Short hash of the resolved source path — the per-source discriminator.

    Two local files that share a stem but live in different directories
    (``/a/clip.mp4`` vs ``/b/clip.mp4``) must never share a project dir
    or artifact names; the stem alone is not a unique source identity
    for local inputs. The hash is computed from the resolved absolute
    path, so it is stable across runs for the same file (caches and
    outputs stay reusable) and case-normalised (``os.path.normcase``) so
    Windows path-casing differences of the same file don't fork the key.
    """
    try:
        resolved = video_path.expanduser().resolve()
    except OSError:
        resolved = Path(os.path.abspath(str(video_path)))
    digest = hashlib.sha256(os.path.normcase(str(resolved)).encode("utf-8", "replace")).hexdigest()[
        :8
    ]
    return digest


# yt-dlp's outtmpl is ``%(id)s-%(epoch)s[-<run-token>].%(ext)s`` — the
# run token (8 hex chars, added in audit round 24 P2) makes the name
# unique per invocation even within the same second. Both the modern
# ``-<10 digits>-<8 hex>`` and the legacy ``-<10 digits>`` suffixes are
# stripped, so a downloaded file keeps the stable ``<id>`` identity
# across runs and alias URLs of the same video.
_EPOCH_SUFFIX_RE = re.compile(r"-\d{10}(?:-[0-9a-f]{8})?$")


def _epochless(stem: str) -> str:
    """Strip yt-dlp's per-run suffix from a filename stem.

    yt-dlp's outtmpl is ``%(id)s-%(epoch)s[-<run-token>].%(ext)s``, so a
    downloaded file is ``<id>-<10-digit-epoch>[-<8-hex-token>].mp4`` — a
    DIFFERENT name every run. Keying artifacts on the raw stem would
    fork the identity per run: every re-download lands in a new project
    dir and the silence / WAV / resume caches miss forever
    (re-detection + re-download of the same URL every time). Stripping
    the trailing ``-<10 digits>[-<8 hex>]`` restores a stable per-URL
    identity.

    Local files whose names happen to end in the pattern are stripped
    too — a deliberate trade-off: the pattern is distinctive enough
    that a false positive is unlikely (and such names are typically
    yt-dlp exports anyway), while the alternative is a per-run cache
    miss for every URL download.
    """
    stripped = _EPOCH_SUFFIX_RE.sub("", stem)
    return stripped if stripped else stem


def downloaded_identity(stem: str, extractor_key: str | None = None) -> str:
    """Stable project identity for a DOWNLOADED source.

    ``stem`` is the epochless video id; ``extractor_key`` (the yt-dlp
    site namespace, e.g. ``YouTube``) disambiguates ids that are only
    unique within one site — two different services returning the same
    id ``12345`` must never share a project dir, caches or the
    post-download project lock (audit round 25 P2). Returns
    ``<extractor>_<id>`` when the extractor is known, else the bare id
    (the historical layout, kept for extractor-less callers and
    local-file naming where the path hash already disambiguates).
    """
    if extractor_key:
        return f"{extractor_key}_{stem}"
    return stem


def artifact_stem(video_path: Path) -> str:
    """Per-source base name: ``<stem>_<path-hash>``.

    Drives the whole naming scheme: the project subdirectory, the final
    output, the WAV cache, the silence cache and the resume cache all
    embed it, so a single source identifier keeps artifacts of same-named
    sources in different directories independent.

    The stem is epoch-stripped (see ``_epochless``): yt-dlp names a
    download ``<id>-<epoch>.mp4`` with a fresh epoch per run, so without
    the strip every artifact would re-key on every re-download of the
    same URL.
    """
    return f"{_epochless(video_path.stem)}_{source_path_key(video_path)}"


def project_dir(output_dir: Path, video_stem: str, per_video_dir: bool) -> Path:
    """Compute the per-project directory path. Does not create it.

    Args:
        output_dir: The user's base output directory.
        video_stem: Video filename stem (e.g. 'myvideo' for 'myvideo.mp4').
        per_video_dir: If True, return ``output_dir / video_stem``;
                       otherwise return ``output_dir`` as-is.

    Returns:
        The directory that should hold this video's artifacts.
    """
    if per_video_dir:
        return output_dir / video_stem
    return output_dir


def ensure_project_dir(output_dir: Path, video_stem: str, per_video_dir: bool) -> Path:
    """Compute the per-project directory and create it (with parents) if missing.

    Returns:
        The project directory. Always exists on return.
    """
    p = project_dir(output_dir, video_stem, per_video_dir)
    p.mkdir(parents=True, exist_ok=True)
    # Record that this directory is an app-owned project dir — the GUI's
    # Recent Projects delete button only deletes marked directories.
    mark_project_dir(p)
    return p


def move_into_project(file_path: Path, project_dir: Path, dest_name: str | None = None) -> Path:
    """Move ``file_path`` into ``project_dir`` (same filename by default).

    ``dest_name`` overrides the destination filename — used to strip
    the per-run yt-dlp epoch suffix from a downloaded source so the
    moved file's identity (and every cache keyed on it) is stable
    across runs of the same URL.

    If the target already exists, it is *replaced* (the incoming file wins):
    on a retry of a fresh download we must not silently keep the previous
    run's stale video. If ``file_path`` is already inside ``project_dir``
    under the target name, returns it unchanged.

    Uses :func:`shutil.move` (not :meth:`Path.rename`) so a cross-drive
    download dir → project dir move (e.g. temp on C:, project on D:)
    works instead of raising ``OSError``.

    Audit #5: the replacement is atomic. The historical implementation
    unlinked the destination BEFORE moving, so a cross-volume copy
    failure, disk-full, permission error or antivirus lock destroyed the
    previous good copy while the new file sat half-copied in the source
    directory. Now the incoming file is first moved to a temporary
    sibling of the target (same volume → the final step is a rename),
    fsynced, and swapped in with :func:`os.replace` — the old
    destination is only lost at the instant the new file is fully in
    place. If the swap fails, the source is restored so a retry still
    has it.

    Raises ``FileNotFoundError`` if ``file_path`` does not exist —
    callers that expect the source to be present (e.g. after a download)
    should see a clear error rather than a silent unlink of nothing.
    """
    file_path = Path(file_path)
    project_dir = Path(project_dir)
    if not file_path.exists():
        raise FileNotFoundError(f"Cannot move into project: source not found: {file_path}")
    if dest_name is None:
        dest_name = file_path.name
    if file_path.parent == project_dir and file_path.name == dest_name:
        return file_path
    new_path = project_dir / dest_name
    project_dir.mkdir(parents=True, exist_ok=True)
    # Same volume as the target, so the final step is a rename, not a
    # copy. Unique name so a concurrent run's temp file can't collide.
    tmp_path = new_path.with_name(new_path.name + f".tmp-{secrets.token_hex(4)}")
    try:
        shutil.move(str(file_path), str(tmp_path))
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    try:
        _fsync_best_effort(tmp_path)
        os.replace(tmp_path, new_path)
    except BaseException:
        # Swap failed (target locked by AV/indexer, disk error...).
        # Restore the source so the user's retry still has the file
        # instead of it being stranded in a .tmp sibling.
        try:
            shutil.move(str(tmp_path), str(file_path))
        except OSError as e:
            logger.warning(f"Could not restore {file_path} after failed move: {e}")
        raise
    return new_path


def _fsync_best_effort(path: Path) -> None:
    """Flush a moved file's data to disk before the atomic swap.

    Best-effort: an OSError here (locked handle, exotic filesystem) is
    not worth failing the move over — os.replace still guarantees the
    old target survives until the swap.
    """
    try:
        with open(path, "rb") as f:
            os.fsync(f.fileno())
    except OSError:
        pass


def add_recent_project(
    recent: list[str],
    project_path: str | Path,
    max_keep: int = 5,
) -> list[str]:
    """Return a new list with ``project_path`` at the front.

    - Dedups: if the path is already in the list, it is moved to the front
      (most-recently-used semantics), not duplicated.
    - Caps at ``max_keep`` entries (oldest dropped).
    - The input list is not modified.

    Accepts either a str or a Path; always stores as str.
    """
    path_str = str(project_path)
    max_keep = max(1, max_keep)
    out = [path_str]
    for p in recent:
        if str(p) == path_str:
            continue
        # Cap BEFORE appending: the historical check sat after the
        # append, so ``max_keep=1`` returned a 2-element list (the new
        # path plus one dedup pass) — the cap only bit at >= 2.
        if len(out) >= max_keep:
            break
        out.append(str(p))
    return out


def prune_recent_projects(recent: list[str]) -> list[str]:
    """Return a new list with entries whose directory no longer exists removed.

    Also drops non-string entries defensively. ``Path.is_dir()`` can raise
    ``OSError`` on Windows (e.g. path longer than MAX_PATH without the
    ``\\\\?\\`` prefix, permission errors) — those entries are dropped
    rather than letting the whole prune abort and break the GUI's
    Recent Projects panel.

    The input list is not modified.
    """
    out: list[str] = []
    for p in recent:
        if not isinstance(p, str):
            continue
        try:
            if Path(p).is_dir():
                out.append(p)
        except OSError:
            continue
    return out


def apply_per_video_dir(
    output_dir: Path,
    video_path: Path,
    is_downloaded: bool,
    per_video_dir: bool = True,
    extractor_key: str | None = None,
) -> tuple[Path, Path]:
    """Resolve the per-video project directory and move the source if needed.

    Returns ``(output_dir, video_path)`` — the (possibly updated) output
    directory and the (possibly moved) source path. The downloaded source is
    moved into the project dir; local files are left untouched.

    When ``per_video_dir`` is False, the user opted out of per-video
    subdirectories: the function returns its inputs unchanged so callers
    don't need to gate the call themselves. When True (default), a
    subdirectory is created inside ``output_dir`` and the downloaded
    source is moved into it:

    - Downloaded sources get a project dir named after the *epochless*
      yt-dlp id, namespaced by the extractor/site when known
      (``YouTube_<id>/``) so two sites sharing an id never collide
      (audit round 25 P2), and are renamed to ``<extractor>_<id><ext>``
      inside it. The epochless name is what makes the identity — and
      every cache keyed on it — stable across re-runs of the same URL,
      instead of forking per download (see ``_epochless``); the
      extractor prefix keeps the identity site-scoped so the same id on
      another service can not collide.
    - Local files get a project dir named ``<stem>_<path-hash>`` (stem +
      source-path hash) and are never moved; the hash keeps two
      same-named files in different directories independent.

    When ``per_video_dir`` is False the project IS the flat ``output_dir``,
    and a DOWNLOADED source is still renamed to its epochless,
    extractor-namespaced ``<extractor>_<id><ext>`` name (atomic
    replace): ``artifact_stem()`` keys every cache and the output name
    on ``source_path_key()`` — a hash of the RAW filename — so a
    per-run ``<id>-<epoch>-<token>`` name would fork the identity (and
    every cache) on each re-download (audit round 24 P6), and two sites
    sharing an id would overwrite each other's file in the flat dir
    (audit round 25 P2). A local file is never renamed: its identity is
    its resolved path.
    """
    if not per_video_dir:
        if is_downloaded:
            dest_name = f"{downloaded_identity(_epochless(video_path.stem), extractor_key)}{video_path.suffix}"
            dest = output_dir / dest_name
            if video_path != dest:
                video_path = move_into_project(video_path, output_dir, dest_name=dest_name)
        return output_dir, video_path
    stem = _epochless(video_path.stem)
    if is_downloaded:
        project_name = downloaded_identity(stem, extractor_key)
    else:
        project_name = f"{stem}_{source_path_key(video_path)}"
    project_dir = ensure_project_dir(output_dir, project_name, True)
    if is_downloaded:
        video_path = move_into_project(
            video_path,
            project_dir,
            dest_name=f"{downloaded_identity(stem, extractor_key)}{video_path.suffix}",
        )
    return project_dir, video_path
