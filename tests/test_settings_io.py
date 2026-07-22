"""Tests for stream2video.settings_io (pure snapshot / YAML helpers
extracted from gui.py — Этап 10 incremental refactor).

The GUI's ``_save_settings`` / ``_save_user_defaults`` /
``_copy_cli_command`` previously inlined the field list / key ordering
and the YAML write. The pure helpers here let the test pin:

  * ``SAVE_SETTINGS_KEYS`` / ``USER_DEFAULTS_KEYS`` — the canonical key
    set so a missing / extra key surfaces as a focused failure instead
    of a regression a user discovers on next startup.
  * ``build_save_settings_snapshot`` / ``build_user_defaults_snapshot``
    — turn a widget-shaped dict into the persisted dict; identity-ish
    (key order preserved; bool casts left to the caller).
  * ``write_cli_config_yaml`` — three-line YAML in
    ``stream2video_cli_config.yaml`` so a "Copy CLI command" paste
    picks up the slider values. Returns ``None`` on filesystem errors.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from stream2video.settings_io import (
    SAVE_SETTINGS_KEYS,
    USER_DEFAULTS_KEYS,
    build_save_settings_snapshot,
    build_user_defaults_snapshot,
    write_cli_config_yaml,
)


class TestSaveSettingsKeys:
    def test_session_state_keys_present(self):
        # window_geometry + input_path + output_dir are session-only
        # but ARE in settings.json so the GUI reopens in the same
        # spot next time.
        for k in ("input_path", "output_dir", "window_geometry"):
            assert k in SAVE_SETTINGS_KEYS

    def test_tunables_present(self):
        # The same flat tunables the GUI's combobox enums expose.
        for k in (
            "method",
            "encoder",
            "video_quality",
            "audio_quality",
            "download_quality",
            "force",
            "delete_after",
            "per_video_dir",
            "x264_low_memory",
            "theme",
        ):
            assert k in SAVE_SETTINGS_KEYS


class TestUserDefaultsKeys:
    def test_session_state_keys_absent(self):
        # user_defaults.json is per-user factory defaults, NOT a
        # session snapshot — so input/output paths and recent
        # projects stay out.
        for k in ("input_path", "output_dir", "window_geometry", "recent_projects"):
            assert k not in USER_DEFAULTS_KEYS

    def test_slider_tunables_present(self):
        # threshold / min_silence / margin are slider values, NOT in
        # SAVE_SETTINGS_KEYS (they live on ``self.config`` and are
        # separately persisted) — but they ARE in user_defaults so a
        # user's slider tunables survive a GUI reset.
        for k in ("threshold", "min_silence", "margin"):
            assert k in USER_DEFAULTS_KEYS


class TestBuildSaveSettingsSnapshot:
    def test_returns_dict_with_canonical_keys(self):
        # Use a sentinel dict so the test catches a missing / extra key
        # in ``SAVE_SETTINGS_KEYS`` (snapshot would KeyError or expand).
        widgets = {key: f"value-{key}" for key in SAVE_SETTINGS_KEYS}
        snapshot = build_save_settings_snapshot(widgets)
        assert set(snapshot.keys()) == set(SAVE_SETTINGS_KEYS)

    def test_preserves_input_types(self):
        # Bools stay bools, strings stay strings — the helper does NOT
        # cast (the GUI already casts at widget-read time).
        widgets = {
            "input_path": "vid.mp4",
            "output_dir": "./out",
            "method": "segment",
            "encoder": "libx264",
            "video_quality": "medium",
            "audio_quality": "high",
            "download_quality": "best",
            "force": True,
            "delete_after": False,
            "per_video_dir": True,
            "x264_low_memory": False,
            "theme": "dark",
            "window_geometry": "1000x600+10+20",
        }
        snapshot = build_save_settings_snapshot(widgets)
        assert snapshot["force"] is True
        assert snapshot["delete_after"] is False
        assert snapshot["per_video_dir"] is True
        assert snapshot["x264_low_memory"] is False
        assert snapshot["theme"] == "dark"

    def test_key_order_matches_canonical(self):
        # The on-disk JSON is built from this dict — key order is
        # observable (the file diff is more readable if it stays
        # stable). Pin it.
        widgets = {key: i for i, key in enumerate(SAVE_SETTINGS_KEYS)}
        snapshot = build_save_settings_snapshot(widgets)
        assert list(snapshot.keys()) == list(SAVE_SETTINGS_KEYS)


class TestBuildUserDefaultsSnapshot:
    def test_returns_dict_with_canonical_keys(self):
        widgets = {key: f"value-{key}" for key in USER_DEFAULTS_KEYS}
        snapshot = build_user_defaults_snapshot(widgets)
        assert set(snapshot.keys()) == set(USER_DEFAULTS_KEYS)

    def test_includes_slider_values(self):
        widgets = {
            "threshold": -30.0,
            "min_silence": 2.0,
            "margin": 0.5,
            "method": "segment",
            "encoder": "h264_nvenc",
            "video_quality": "high",
            "audio_quality": "high",
            "download_quality": "best",
            "force": False,
            "delete_after": True,
            "per_video_dir": False,
            "x264_low_memory": True,
            "theme": "light",
        }
        snapshot = build_user_defaults_snapshot(widgets)
        assert snapshot["threshold"] == -30.0
        assert snapshot["margin"] == 0.5
        assert snapshot["x264_low_memory"] is True


class TestWriteCliConfigYaml:
    def test_writes_three_keys(self, tmp_path: Path):
        config_path = write_cli_config_yaml(tmp_path, threshold=-30.0, min_silence=2.0, margin=0.5)
        assert config_path is not None
        assert config_path.exists()
        text = config_path.read_text(encoding="utf-8")
        assert "threshold: -30.0" in text
        assert "min_silence: 2.0" in text
        assert "margin: 0.5" in text

    def test_uses_default_filename(self, tmp_path: Path):
        # The default filename is the one the GUI's ``build_cli_command``
        # references — keep the two in sync.
        config_path = write_cli_config_yaml(tmp_path, 0, 0, 0)
        assert config_path is not None
        assert config_path.name == "stream2video_cli_config.yaml"

    def test_custom_filename(self, tmp_path: Path):
        config_path = write_cli_config_yaml(tmp_path, 0, 0, 0, filename="custom.yaml")
        assert config_path is not None
        assert config_path.name == "custom.yaml"

    def test_creates_parent_dir_if_missing(self, tmp_path: Path):
        # The GUI's "Copy CLI command" may run before the user has
        # opened the output dir — the helper creates it.
        new_dir = tmp_path / "fresh" / "out"
        config_path = write_cli_config_yaml(new_dir, 0, 0, 0)
        assert config_path is not None
        assert config_path.exists()

    def test_returns_none_on_os_error(self, tmp_path: Path):
        # If the directory can't be written (permission denied),
        # return ``None`` so the caller logs and continues without the
        # ``--config`` flag. Patch ``open`` to raise OSError — easier
        # than constructing a permission-denied directory cross-platform.
        with patch("builtins.open", side_effect=OSError("denied")):
            config_path = write_cli_config_yaml(tmp_path, 0, 0, 0)
        assert config_path is None

    def test_returns_resolved_path(self, tmp_path: Path):
        # The path returned is ``resolve()``-ed so the "Copied command"
        # pastes an absolute path even when the GUI's ``out_raw`` was
        # relative.
        config_path = write_cli_config_yaml(tmp_path, 0, 0, 0)
        assert config_path is not None
        assert config_path.is_absolute()
