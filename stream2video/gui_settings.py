"""Settings I/O extracted from ``gui.py`` (Этап 10 incremental).

Pure functions for serialising / deserialising the GUI's settings.json
so they can be unit-tested without instantiating the GUI. The GUI class
delegates JSON read/write to these helpers; the widget-touching part
(reading combo_method.get() etc.) stays in gui.py because that's the
only place Tk widgets can be safely accessed.

The on-disk format is plain JSON: a flat dict of {key: value}. Keys
are validated against CONFIG_DEFAULTS via ``coerce_typed_value`` so a
corrupt file with the wrong type for a key is silently dropped instead
of crashing the GUI on startup. Two GUI-only session-state keys
(``input_path``, ``window_geometry``) are NOT in CONFIG_DEFAULTS and
are handled explicitly so they survive a save/load round-trip.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from stream2video.config import coerce_typed_value, settings_path

logger = logging.getLogger(__name__)


# GUI-only session-state keys that live in settings.json but aren't in
# CONFIG_DEFAULTS. Listed explicitly so the load path can re-apply them
# without being rejected by coerce_typed_value (which drops unknown
# keys). Both are strings; their type check is done inline below.
GUI_SESSION_KEYS: dict[str, type] = {
    "input_path": str,
    "window_geometry": str,
}


def save_settings(config: dict[str, Any]) -> None:
    """Atomically write ``config`` to settings.json.

    Uses a temp file + ``os.replace`` so a crash mid-write leaves the
    previous file intact (atomic rename on the same filesystem).
    Parent directories are created if needed (first run).

    Raises propagate — the GUI catches and logs the warning. Tests
    use tmp_path so the global settings.json isn't touched.
    """
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # tempfile.mkstemp: a GUI-close autosave racing a "Save current as
    # defaults" click otherwise opens the same deterministic pathname
    # twice and interleaves writes, leaving a mixed JSON that
    # ``load_settings`` then drops entirely. mkstemp isolates each
    # write; ``os.replace`` serialises publication. No cleanup on
    # failure — the caller (gui_lifecycle/_on_close) logs it and
    # continues shutdown; the orphan tmp is GC'd on next start.
    import tempfile

    fd, tmp_str = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    tmp = os.fsdecode(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        os.replace(tmp, str(path))
    except Exception:
        import contextlib

        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def load_settings() -> dict[str, Any]:
    """Load and validate settings.json, returning a flat dict.

    Returns an empty dict when the file is missing, unreadable, or
    not a JSON object — callers (the GUI) fall back to CONFIG_DEFAULTS.

    Keys are validated:
      * CONFIG_DEFAULTS-typed keys go through ``coerce_typed_value``
        so a corrupt value (e.g. ``threshold: "abc"``) is dropped
        instead of crashing the GUI later.
      * GUI_SESSION_KEYS are checked against their expected type so a
        bad ``window_geometry: 42`` is rejected.
    """
    sp = settings_path()
    if not sp.exists():
        return {}
    try:
        with open(sp, encoding="utf-8") as f:
            loaded = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Failed to load settings: %s", e)
        return {}
    if not isinstance(loaded, dict):
        logger.warning("Settings file is not a JSON object; ignoring")
        return {}

    out: dict[str, Any] = {}
    for key, value in loaded.items():
        if key in GUI_SESSION_KEYS:
            expected = GUI_SESSION_KEYS[key]
            if isinstance(value, expected):
                out[key] = value
            else:
                logger.debug("Dropping settings[%r] with wrong type: %r", key, value)
            continue
        coerced = coerce_typed_value(key, value)
        if coerced is not None:
            out[key] = coerced
        else:
            logger.debug("Dropping settings[%r] with wrong type: %r", key, value)
    return out
