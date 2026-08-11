"""Project directory resolution for per-video subdirectory support.

When `per_video_dir=True` is set in the config, all artifacts (downloaded
source, audio WAV, silence cache JSON, compressed output, log file, temp
segment dirs) for a given video are collected into a single subdirectory
named after the video stem, instead of living in the user's flat
`output_dir`.

Layout comparison (per_video_dir=True):
    output_dir/
        <stem>/
            <stem>.mp4           # downloaded source (or local file untouched)
            <stem>_audio.wav     # cached audio extract
            <stem>_silence_cache.json
            <stem>_compressed.mp4
            stream2video.log
            _<stem>_segments/    # temp, cleaned on success
            _<stem>_batch/       # temp, cleaned on success

Local input files are NEVER moved or copied — the source stays where the
user put it, but WAV / JSON / compressed / log / temp dirs all go into
the per-video subdir.
"""

from __future__ import annotations

import shutil
from pathlib import Path

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
    return p


def move_into_project(file_path: Path, project_dir: Path) -> Path:
    """Move ``file_path`` into ``project_dir`` (same filename). Returns new path.

    If the target already exists, it is *replaced* (the incoming file wins):
    on a retry of a fresh download we must not silently keep the previous
    run's stale video. If ``file_path`` is already inside ``project_dir``,
    returns it unchanged.

    Uses :func:`shutil.move` (not :meth:`Path.rename`) so a cross-drive
    download dir → project dir move (e.g. temp on C:, project on D:)
    works instead of raising ``OSError``.

    Raises ``FileNotFoundError`` if ``file_path`` does not exist —
    callers that expect the source to be present (e.g. after a download)
    should see a clear error rather than a silent unlink of nothing.
    """
    file_path = Path(file_path)
    project_dir = Path(project_dir)
    if not file_path.exists():
        raise FileNotFoundError(f"Cannot move into project: source not found: {file_path}")
    if file_path.parent == project_dir:
        return file_path
    new_path = project_dir / file_path.name
    project_dir.mkdir(parents=True, exist_ok=True)
    # os.rename inside shutil.move raises FileExistsError (WinError 183)
    # on Windows when dst exists — remove the stale target first so the
    # fresh download always wins.
    try:
        new_path.unlink(missing_ok=True)
    except OSError:
        # Target locked by another reader — let move() surface the error.
        pass
    # shutil.move handles both same-volume rename and the cross-drive
    # copy+unlink fallback the plain Path.rename would have raised on.
    shutil.move(str(file_path), str(new_path))
    return new_path


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
    out = [path_str]
    for p in recent:
        if str(p) != path_str:
            out.append(str(p))
        if len(out) >= max_keep:
            break
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
) -> tuple[Path, Path]:
    """Resolve the per-video project directory and move the source if needed.

    Returns ``(output_dir, video_path)`` — the (possibly updated) output
    directory and the (possibly moved) source path. The downloaded source is
    moved into the project dir; local files are left untouched.

    When ``per_video_dir`` is False, the user opted out of per-video
    subdirectories: the function returns its inputs unchanged so callers
    don't need to gate the call themselves. When True (default), a
    subdirectory named after ``video_path.stem`` is created inside
    ``output_dir`` and the downloaded source is moved into it.
    """
    if not per_video_dir:
        return output_dir, video_path
    project_dir = ensure_project_dir(output_dir, video_path.stem, True)
    if project_dir != output_dir:
        if is_downloaded:
            video_path = move_into_project(video_path, project_dir)
        return project_dir, video_path
    return output_dir, video_path
