"""Shared configuration defaults and validation ranges."""

import json
import os
from pathlib import Path
from typing import Any

CONFIG_DEFAULTS: dict[str, Any] = {
    "threshold": -30.0,
    "min_silence": 2.0,
    "margin": 0.5,
    "method": "segment",
    "encoder": "h264_mf",
    "video_quality": "medium",
    "download_quality": "best",
    "force": False,
    "delete_after": False,
    "per_video_dir": True,
    "output_dir": "",
    "theme": "dark",
    "recent_projects": [],
}

CONFIG_RANGES = {
    "threshold": (-60, -5),
    "min_silence": (0.1, 60),
    "margin": (-3, 5),
}

VALID_METHODS: list[str] = ["segment", "batch"]

VALID_ENCODERS: list[str] = ["h264_nvenc", "h264_amf", "h264_mf", "libx264"]

VALID_QUALITIES: list[str] = ["high", "medium", "low"]

VALID_DOWNLOAD_QUALITIES: list[str] = ["best", "1080p", "720p", "480p", "360p"]

VALID_THEMES: list[str] = ["dark", "light", "system"]

# Keys that are user-tunable defaults (exclude per-session state like
# output_dir / recent_projects / input_path). Used by the GUI's
# "Save current as defaults" button.
USER_DEFAULT_KEYS: list[str] = [
    "threshold",
    "min_silence",
    "margin",
    "method",
    "encoder",
    "video_quality",
    "download_quality",
    "force",
    "delete_after",
    "per_video_dir",
    "theme",
]


def user_defaults_path() -> Path:
    """Path to the per-user defaults file. Lives next to settings.json."""
    return _base_dir() / "user_defaults.json"


def settings_path() -> Path:
    """Path to the GUI settings file (gui_settings.json or settings.json in _portable)."""
    base = _base_dir()
    if base.name == "_portable":
        return base / "settings.json"
    return base / "gui_settings.json"


def _base_dir() -> Path:
    """Base directory for config files: ``_portable/`` if it exists, else the project root."""
    project_root = Path(__file__).parent.parent
    if (project_root / "_portable").exists():
        return project_root / "_portable"
    return project_root


def coerce_typed_value(key: str, value: Any) -> Any:
    """Return ``value`` if its type matches ``CONFIG_DEFAULTS[key]``, else None.

    Centralised type guard so load_user_defaults() and the GUI's
    _load_settings() apply the same strict-but-forgiving filter. A corrupt
    file with ``{"threshold": "abc"}`` silently drops that key instead of
    crashing the GUI later.

    For list-typed defaults (currently only ``recent_projects``), the
    element type is also validated against the default list's first
    element's type — a list containing non-str entries (e.g. ``[42, null,
    Path('/x')]``) is dropped entirely so a later ``json.dump`` in the GUI
    can't crash on a non-serialisable element. An empty list is accepted.
    """
    if key not in CONFIG_DEFAULTS:
        return None
    default = CONFIG_DEFAULTS[key]
    if isinstance(default, bool):
        return value if isinstance(value, bool) else None
    if isinstance(default, (int, float)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return value
    if isinstance(default, str):
        return value if isinstance(value, str) else None
    if isinstance(default, list):
        if not isinstance(value, list):
            return None
        # Validate element types against the default list's element type
        # (defaults are homogeneous lists, so we sample [0] when non-empty).
        if default:
            elem_type = type(default[0])
            if not all(isinstance(e, elem_type) for e in value):
                return None
        return value
    return None


def load_user_defaults() -> dict[str, Any]:
    """Read user_defaults.json and return a dict of overrides, applied
    on top of CONFIG_DEFAULTS. Missing or invalid file = no overrides.
    Unknown keys are ignored. Type validation: a key is accepted only
    if its value type matches the default's type."""
    path = user_defaults_path()
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        k: v for k, v in ((k, coerce_typed_value(k, v)) for k, v in data.items()) if v is not None
    }


def save_user_defaults(values: dict[str, Any]) -> None:
    """Persist a subset of values (filtered to USER_DEFAULT_KEYS) to
    user_defaults.json. Missing keys are dropped (not written as nulls)."""
    payload: dict[str, Any] = {}
    for key in USER_DEFAULT_KEYS:
        if key in values:
            payload[key] = values[key]
    path = user_defaults_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def effective_defaults() -> dict[str, Any]:
    """CONFIG_DEFAULTS overlaid with user_defaults.json overrides."""
    out = {k: (list(v) if isinstance(v, list) else v) for k, v in CONFIG_DEFAULTS.items()}
    out.update(load_user_defaults())
    return out
