"""Tests for generated GUI completion sounds."""

from __future__ import annotations

import wave
from pathlib import Path

from stream2video.completion_sound import (
    SAMPLE_RATE,
    completion_chime_path,
    ensure_completion_chime,
    play_completion_sound,
)


def test_generates_valid_success_and_attention_wavs(tmp_path: Path):
    success = ensure_completion_chime(tmp_path / "success.wav", kind="success")
    attention = ensure_completion_chime(tmp_path / "attention.wav", kind="attention")

    for path in (success, attention):
        assert path.exists()
        with wave.open(str(path), "rb") as wav:
            assert wav.getnchannels() == 1
            assert wav.getsampwidth() == 2
            assert wav.getframerate() == SAMPLE_RATE
            assert wav.getnframes() > 0

    assert success.read_bytes() != attention.read_bytes()


def test_completion_chime_path_uses_separate_files(tmp_path: Path, monkeypatch):
    settings_file = tmp_path / "gui_settings.json"
    monkeypatch.setattr("stream2video.completion_sound.settings_path", lambda: settings_file)

    assert completion_chime_path("success") == tmp_path / "completion_success.wav"
    assert completion_chime_path("attention") == tmp_path / "completion_attention.wav"


def test_play_completion_sound_returns_none_when_disabled():
    assert play_completion_sound(enabled=False, kind="attention") is None
