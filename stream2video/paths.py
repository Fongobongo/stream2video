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
from pathlib import Path


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

    If the target already exists, ``file_path`` is removed and the existing
    target is kept (avoids clobbering on retry). If ``file_path`` is already
    inside ``project_dir``, returns it unchanged.
    """
    file_path = Path(file_path)
    project_dir = Path(project_dir)
    if file_path.parent == project_dir:
        return file_path
    new_path = project_dir / file_path.name
    if new_path.exists():
        file_path.unlink(missing_ok=True)
        return new_path
    project_dir.mkdir(parents=True, exist_ok=True)
    file_path.rename(new_path)
    return new_path
