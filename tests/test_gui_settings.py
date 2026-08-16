"""Tests for stream2video.gui_settings (settings.json I/O).

Pure functions extracted from gui.py so the serialisation + type
validation can be unit-tested without driving the Tk main loop. The
on-disk format is plain JSON; keys are validated against
CONFIG_DEFAULTS via coerce_typed_value.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from stream2video.gui_settings import GUI_SESSION_KEYS, load_settings, save_settings


@pytest.fixture
def _settings_at(tmp_path: Path):
    """Patch settings_path() to point inside tmp_path."""
    fake_path = tmp_path / "gui_settings.json"
    with patch("stream2video.gui_settings.settings_path", return_value=fake_path):
        yield fake_path


class TestSaveSettings:
    def test_creates_parent_dir_and_writes_json(self, _settings_at: Path):
        cfg = {"threshold": -30.0, "method": "segment", "force": False}
        save_settings(cfg)
        assert _settings_at.exists()
        loaded = json.loads(_settings_at.read_text(encoding="utf-8"))
        assert loaded["threshold"] == -30.0
        assert loaded["method"] == "segment"
        assert loaded["force"] is False

    def test_atomic_write_via_tmp_file(self, _settings_at: Path):
        # The temp file is created and renamed atomically; no leftover
        # .tmp file should remain after save_settings returns.
        save_settings({"threshold": -30.0})
        tmp_files = list(_settings_at.parent.glob("*.tmp"))
        assert tmp_files == []

    def test_preserves_unicode(self, _settings_at: Path):
        cfg = {"input_path": "/home/user/видео.mp4"}
        save_settings(cfg)
        loaded = json.loads(_settings_at.read_text(encoding="utf-8"))
        assert loaded["input_path"] == "/home/user/видео.mp4"

    def test_overwrites_existing(self, _settings_at: Path):
        save_settings({"threshold": -30.0})
        save_settings({"threshold": -25.0})
        loaded = json.loads(_settings_at.read_text(encoding="utf-8"))
        assert loaded["threshold"] == -25.0

    def test_non_finite_raises_and_keeps_previous_file(self, _settings_at: Path):
        # Audit round 16 P1: the GUI's _save_settings must never write
        # NaN/Infinity into settings.json (they'd reappear in the next
        # run's sliders as "no value"). json.dump(allow_nan=False) raises
        # ValueError, which gui_lifecycle._save_settings catches and
        # downgrades to a WARN — and the previous file must stay intact.
        save_settings({"threshold": -30.0})
        original_text = _settings_at.read_text(encoding="utf-8")
        with pytest.raises(ValueError):
            save_settings({"threshold": float("inf")})
        assert _settings_at.read_text(encoding="utf-8") == original_text
        assert list(_settings_at.parent.glob("*.tmp")) == []


class TestLoadSettings:
    def test_missing_file_returns_empty_dict(self, _settings_at: Path):
        assert load_settings() == {}

    def test_round_trip(self, _settings_at: Path):
        cfg = {
            "threshold": -25.0,
            "min_silence": 1.5,
            "margin": 0.3,
            "method": "batch",
            "encoder": "libx264",
            "force": True,
            "input_path": "/tmp/video.mp4",
            "window_geometry": "1080x680+24+42",
        }
        save_settings(cfg)
        loaded = load_settings()
        for key, value in cfg.items():
            assert loaded[key] == value, f"round-trip mismatch on {key}"

    def test_drops_wrong_typed_keys(self, _settings_at: Path):
        # A corrupt file with ``threshold: "abc"`` (str instead of
        # float) should drop that key, not crash.
        _settings_at.write_text(
            json.dumps({"threshold": "abc", "method": "segment"}),
            encoding="utf-8",
        )
        loaded = load_settings()
        assert "threshold" not in loaded
        assert loaded["method"] == "segment"

    def test_drops_unknown_keys(self, _settings_at: Path):
        _settings_at.write_text(
            json.dumps({"made_up_key": "x", "method": "segment"}),
            encoding="utf-8",
        )
        loaded = load_settings()
        assert "made_up_key" not in loaded
        assert loaded["method"] == "segment"

    def test_gui_session_keys_accepted(self, _settings_at: Path):
        _settings_at.write_text(
            json.dumps(
                {
                    "input_path": "/tmp/video.mp4",
                    "window_geometry": "1080x680",
                }
            ),
            encoding="utf-8",
        )
        loaded = load_settings()
        assert loaded["input_path"] == "/tmp/video.mp4"
        assert loaded["window_geometry"] == "1080x680"

    def test_gui_session_keys_wrong_type_dropped(self, _settings_at: Path):
        _settings_at.write_text(
            json.dumps(
                {
                    "input_path": 42,  # int instead of str — reject
                    "window_geometry": ["1080x680"],  # list instead of str
                }
            ),
            encoding="utf-8",
        )
        loaded = load_settings()
        assert "input_path" not in loaded
        assert "window_geometry" not in loaded

    def test_corrupt_json_returns_empty(self, _settings_at: Path):
        _settings_at.write_text("not json {{{", encoding="utf-8")
        assert load_settings() == {}

    def test_non_object_json_returns_empty(self, _settings_at: Path):
        _settings_at.write_text("[1, 2, 3]", encoding="utf-8")
        assert load_settings() == {}

    def test_non_finite_literals_dropped(self, _settings_at: Path):
        # Audit round 16 P1: settings.json written by an older build
        # (plain json.dump) can contain NaN/Infinity/-Infinity tokens;
        # load_settings must drop those keys, not pass them to the GUI.
        _settings_at.write_text(
            '{"threshold": NaN, "min_silence": Infinity, "margin": -Infinity, "method": "segment"}',
            encoding="utf-8",
        )
        loaded = load_settings()
        assert "threshold" not in loaded
        assert "min_silence" not in loaded
        assert "margin" not in loaded
        assert loaded == {"method": "segment"}

    def test_out_of_range_ints_dropped(self, _settings_at: Path):
        # Audit round 18 P2: int-typed settings outside their
        # CONFIG_RANGES bound must not load into the GUI (previously
        # only float-typed defaults were range-checked on load).
        _settings_at.write_text(
            json.dumps(
                {
                    "batch_chunk_size": 999999,
                    "stall_kill_timeout": 1,
                    "segment_encode_timeout": 60,
                }
            ),
            encoding="utf-8",
        )
        loaded = load_settings()
        assert "batch_chunk_size" not in loaded
        assert "stall_kill_timeout" not in loaded
        assert loaded == {"segment_encode_timeout": 60}


class TestGuiSessionKeys:
    def test_documented_keys(self):
        # Anyone editing GUI_SESSION_KEYS should know what they're
        # accepting; pin the current set so a typo doesn't silently
        # drop session state.
        assert set(GUI_SESSION_KEYS.keys()) == {"input_path", "window_geometry"}
        assert all(v is str for v in GUI_SESSION_KEYS.values())
