"""Shared configuration defaults and validation ranges."""

import json
import os
from pathlib import Path
from typing import Any, Dict, List

CONFIG_DEFAULTS: Dict[str, Any] = {
    "threshold": -60.0,
    "min_silence": 2.0,
    "margin": 0.5,
    "method": "segment",
    "encoder": "h264_mf",
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

# Keys that are user-tunable defaults (exclude per-session state like
# output_dir / recent_projects / input_path). Used by the GUI's
# "Save current as defaults" button.
USER_DEFAULT_KEYS: List[str] = [
    "threshold",
    "min_silence",
    "margin",
    "method",
    "encoder",
    "force",
    "delete_after",
    "per_video_dir",
    "theme",
]


def user_defaults_path() -> Path:
    """Path to the per-user defaults file. Lives next to settings.json."""
    portable = Path(__file__).parent.parent / "_portable"
    if portable.exists():
        return portable / "user_defaults.json"
    return Path(__file__).parent.parent / "user_defaults.json"


def load_user_defaults() -> Dict[str, Any]:
    """Read user_defaults.json and return a dict of overrides, applied
    on top of CONFIG_DEFAULTS. Missing or invalid file = no overrides.
    Unknown keys are ignored. Type validation: a key is accepted only
    if its value type matches the default's type."""
    path = user_defaults_path()
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    result: Dict[str, Any] = {}
    for key, value in data.items():
        if key not in CONFIG_DEFAULTS:
            continue
        default = CONFIG_DEFAULTS[key]
        # Type guard: don't let a corrupt file change a list to a string, etc.
        if isinstance(default, bool):
            if not isinstance(value, bool):
                continue
        elif isinstance(default, (int, float)):
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
        elif isinstance(default, str):
            if not isinstance(value, str):
                continue
        elif isinstance(default, list):
            if not isinstance(value, list):
                continue
        else:
            continue
        result[key] = value
    return result


def save_user_defaults(values: Dict[str, Any]) -> None:
    """Persist a subset of values (filtered to USER_DEFAULT_KEYS) to
    user_defaults.json. Missing keys are dropped (not written as nulls)."""
    payload: Dict[str, Any] = {}
    for key in USER_DEFAULT_KEYS:
        if key in values:
            payload[key] = values[key]
    path = user_defaults_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def effective_defaults() -> Dict[str, Any]:
    """CONFIG_DEFAULTS overlaid with user_defaults.json overrides."""
    out = CONFIG_DEFAULTS.copy()
    out.update(load_user_defaults())
    return out

