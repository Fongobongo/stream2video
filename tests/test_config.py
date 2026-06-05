"""Tests for stream2video.config — defaults, ranges, and user-defaults I/O."""
import json
import pytest
from pathlib import Path
from tempfile import TemporaryDirectory

from stream2video.config import (
    CONFIG_DEFAULTS,
    CONFIG_RANGES,
    USER_DEFAULT_KEYS,
    effective_defaults,
    load_user_defaults,
    save_user_defaults,
    user_defaults_path,
)


class TestConfigDefaults:
    """Sanity checks on the static defaults dict."""

    def test_per_video_dir_is_true_by_default(self):
        # As of the user-defaults feature, per_video_dir defaults to True.
        assert CONFIG_DEFAULTS["per_video_dir"] is True

    def test_required_keys_present(self):
        for key in (
            "threshold", "min_silence", "margin", "method", "encoder",
            "force", "delete_after", "per_video_dir", "output_dir",
            "theme", "recent_projects",
        ):
            assert key in CONFIG_DEFAULTS, f"missing default for {key}"

    def test_ranges_cover_defaults(self):
        for key, (lo, hi) in CONFIG_RANGES.items():
            assert lo <= CONFIG_DEFAULTS[key] <= hi, (
                f"default for {key} ({CONFIG_DEFAULTS[key]}) is outside range "
                f"({lo}, {hi})"
            )

    def test_user_default_keys_are_a_subset_of_defaults(self):
        for key in USER_DEFAULT_KEYS:
            assert key in CONFIG_DEFAULTS, (
                f"USER_DEFAULT_KEYS references unknown key {key}"
            )
        # Per-session keys are NOT in USER_DEFAULT_KEYS
        assert "output_dir" not in USER_DEFAULT_KEYS
        assert "recent_projects" not in USER_DEFAULT_KEYS


class TestLoadUserDefaults:
    """load_user_defaults() — read user_defaults.json and apply type guards."""

    def test_no_file_returns_empty_dict(self, tmp_path, monkeypatch):
        fake = tmp_path / "user_defaults.json"
        monkeypatch.setattr(
            "stream2video.config.user_defaults_path", lambda: fake
        )
        assert load_user_defaults() == {}

    def test_valid_file_returns_parsed_overrides(self, tmp_path, monkeypatch):
        fake = tmp_path / "user_defaults.json"
        fake.write_text(json.dumps({"threshold": -50.0, "per_video_dir": True}))
        monkeypatch.setattr(
            "stream2video.config.user_defaults_path", lambda: fake
        )
        result = load_user_defaults()
        assert result == {"threshold": -50.0, "per_video_dir": True}

    def test_unknown_keys_ignored(self, tmp_path, monkeypatch):
        fake = tmp_path / "user_defaults.json"
        fake.write_text(json.dumps({
            "threshold": -40.0,
            "mystery_key": 123,
            "input_path": "/etc/passwd",
        }))
        monkeypatch.setattr(
            "stream2video.config.user_defaults_path", lambda: fake
        )
        result = load_user_defaults()
        assert "mystery_key" not in result
        assert "input_path" not in result
        assert result["threshold"] == -40.0

    def test_wrong_type_dropped(self, tmp_path, monkeypatch):
        # per_video_dir is a bool — reject strings/numbers.
        fake = tmp_path / "user_defaults.json"
        fake.write_text(json.dumps({
            "per_video_dir": "yes",          # wrong type
            "threshold": "loud",              # wrong type
            "method": 42,                     # wrong type
        }))
        monkeypatch.setattr(
            "stream2video.config.user_defaults_path", lambda: fake
        )
        result = load_user_defaults()
        assert result == {}

    def test_bool_not_accepted_for_numeric_keys(self, tmp_path, monkeypatch):
        # In Python, bool is a subclass of int — guard against that.
        fake = tmp_path / "user_defaults.json"
        fake.write_text(json.dumps({"threshold": True}))
        monkeypatch.setattr(
            "stream2video.config.user_defaults_path", lambda: fake
        )
        result = load_user_defaults()
        assert "threshold" not in result

    def test_corrupt_json_returns_empty(self, tmp_path, monkeypatch):
        fake = tmp_path / "user_defaults.json"
        fake.write_text("{not valid json")
        monkeypatch.setattr(
            "stream2video.config.user_defaults_path", lambda: fake
        )
        assert load_user_defaults() == {}

    def test_top_level_non_dict_returns_empty(self, tmp_path, monkeypatch):
        fake = tmp_path / "user_defaults.json"
        fake.write_text(json.dumps([1, 2, 3]))
        monkeypatch.setattr(
            "stream2video.config.user_defaults_path", lambda: fake
        )
        assert load_user_defaults() == {}


class TestSaveUserDefaults:
    """save_user_defaults() — atomic write, filter to USER_DEFAULT_KEYS."""

    def test_persists_subset_of_keys(self, tmp_path, monkeypatch):
        fake = tmp_path / "user_defaults.json"
        monkeypatch.setattr(
            "stream2video.config.user_defaults_path", lambda: fake
        )
        save_user_defaults({
            "threshold": -45.0,
            "min_silence": 1.5,
            "per_video_dir": True,
        })
        data = json.loads(fake.read_text())
        assert data == {
            "threshold": -45.0,
            "min_silence": 1.5,
            "per_video_dir": True,
        }

    def test_drops_per_session_keys(self, tmp_path, monkeypatch):
        fake = tmp_path / "user_defaults.json"
        monkeypatch.setattr(
            "stream2video.config.user_defaults_path", lambda: fake
        )
        save_user_defaults({
            "threshold": -50.0,
            "output_dir": "/tmp/secret",        # per-session, must be dropped
            "recent_projects": ["/etc/passwd"],  # per-session, must be dropped
            "input_path": "not-a-default",
        })
        data = json.loads(fake.read_text())
        assert "output_dir" not in data
        assert "recent_projects" not in data
        assert "input_path" not in data
        assert data["threshold"] == -50.0

    def test_round_trip(self, tmp_path, monkeypatch):
        fake = tmp_path / "user_defaults.json"
        monkeypatch.setattr(
            "stream2video.config.user_defaults_path", lambda: fake
        )
        original = {
            "threshold": -35.0,
            "min_silence": 3.0,
            "margin": -0.5,
            "method": "stream",
            "encoder": "libx264",
            "force": True,
            "delete_after": False,
            "per_video_dir": True,
            "theme": "light",
        }
        save_user_defaults(original)
        loaded = load_user_defaults()
        assert loaded == original

    def test_atomic_write_no_leftover_tmp(self, tmp_path, monkeypatch):
        fake = tmp_path / "user_defaults.json"
        monkeypatch.setattr(
            "stream2video.config.user_defaults_path", lambda: fake
        )
        save_user_defaults({"threshold": -42.0})
        # The atomic write uses a .tmp file then os.replace — verify no leftover
        siblings = list(tmp_path.iterdir())
        assert siblings == [fake]

    def test_creates_parent_dir(self, tmp_path, monkeypatch):
        # user_defaults_path() normally points to _portable/user_defaults.json
        # which already exists. Here we redirect to a deeper path.
        deep = tmp_path / "a" / "b" / "user_defaults.json"
        monkeypatch.setattr(
            "stream2video.config.user_defaults_path", lambda: deep
        )
        save_user_defaults({"threshold": -45.0})
        assert deep.exists()


class TestEffectiveDefaults:
    """effective_defaults() — CONFIG_DEFAULTS overlaid with user overrides."""

    def test_no_file_returns_copy_of_config_defaults(self, tmp_path, monkeypatch):
        fake = tmp_path / "user_defaults.json"  # does not exist
        monkeypatch.setattr(
            "stream2video.config.user_defaults_path", lambda: fake
        )
        result = effective_defaults()
        assert result == CONFIG_DEFAULTS
        # Must be a copy, not the same object
        assert result is not CONFIG_DEFAULTS

    def test_user_overrides_take_precedence(self, tmp_path, monkeypatch):
        fake = tmp_path / "user_defaults.json"
        fake.write_text(json.dumps({"threshold": -30.0, "theme": "light"}))
        monkeypatch.setattr(
            "stream2video.config.user_defaults_path", lambda: fake
        )
        result = effective_defaults()
        assert result["threshold"] == -30.0
        assert result["theme"] == "light"
        # Untouched keys keep their factory values
        assert result["method"] == CONFIG_DEFAULTS["method"]
        assert result["encoder"] == CONFIG_DEFAULTS["encoder"]

    def test_user_invalid_overrides_are_silently_dropped(self, tmp_path, monkeypatch):
        fake = tmp_path / "user_defaults.json"
        fake.write_text(json.dumps({
            "per_video_dir": "maybe",   # wrong type — dropped
            "threshold": -25.0,         # valid
        }))
        monkeypatch.setattr(
            "stream2video.config.user_defaults_path", lambda: fake
        )
        result = effective_defaults()
        assert result["per_video_dir"] == CONFIG_DEFAULTS["per_video_dir"]  # not overridden
        assert result["threshold"] == -25.0
