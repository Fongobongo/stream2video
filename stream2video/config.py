"""Shared configuration defaults and validation ranges."""

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONFIG_DEFAULTS: dict[str, Any] = {
    "threshold": -30.0,
    "min_silence": 2.0,
    "margin": 0.5,
    "method": "segment",
    "encoder": "h264_mf",
    "video_quality": "medium",
    "audio_quality": "medium",
    "download_quality": "best",
    "software_fallback": "ask",
    "x264_preset": "medium",
    # Encoder thread budget. ``auto`` = let ffmpeg decide (-threads 0,
    # which usually picks one per logical core); an int caps it. ``auto``
    # preserves the historical behaviour (no thread hint) so an upgrade
    # doesn't quietly change the load profile of an existing user.
    "encoder_threads": "auto",
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

# Encoder fallback policy when the user-selected HW encoder (AMF/NVENC/MF)
# is unavailable or fails mid-run. ``ask`` (default) refuses silent
# fallback to libx264 — heavy CPU workload can overload an overclocked
# machine, so the user must explicitly confirm. ``disabled`` raises
# immediately. ``enabled`` preserves the legacy silent-fallback behaviour
# for users running on a known-stable CPU.
VALID_SOFTWARE_FALLBACKS: list[str] = ["ask", "disabled", "enabled"]

# x264 preset ladder. Kept narrow: ffmpeg accepts ultrafast..placebo but
# we only expose the slice that matches a CPU quality/speed/size trade-off
# the user can reason about. The CLI/GUI passes one of these verbatim to
# ffmpeg ``-preset``.
VALID_X264_PRESETS: list[str] = [
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
]

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
    "audio_quality",
    "download_quality",
    "software_fallback",
    "x264_preset",
    "encoder_threads",
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
    # Special case: ``encoder_threads`` accepts ``"auto"`` (str default)
    # OR a positive int from the user. The two types are both legitimate
    # expressions of the same setting, so accept either explicitly. A
    # non-positive int is dropped (it would be a no-op or harmful hint
    # to ffmpeg's thread pool — negative values raise on the CLI side).
    if key == "encoder_threads":
        if isinstance(value, str) and value == "auto":
            return value
        if isinstance(value, bool):
            return None
        if isinstance(value, int) and value > 0:
            return value
        return None
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
