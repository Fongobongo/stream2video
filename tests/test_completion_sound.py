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


def test_play_completion_sound_rejects_unknown_kind():
    warning = play_completion_sound(enabled=True, kind="weird")
    assert warning is not None and "Unknown completion sound kind" in warning


def test_play_completion_sound_uses_winsound_on_windows(monkeypatch, tmp_path: Path):
    import stream2video.completion_sound as cs

    monkeypatch.setattr(cs.sys, "platform", "win32")
    monkeypatch.setattr(cs, "ensure_completion_chime", lambda *a, **k: tmp_path / "chime.wav")
    called: dict[str, Path] = {}

    def fake_winsound(chime: Path):
        called["path"] = chime
        return None

    monkeypatch.setattr(cs, "_play_with_winsound", fake_winsound)
    assert play_completion_sound(enabled=True, kind="success") is None
    assert called["path"] == tmp_path / "chime.wav"


def test_play_completion_sound_uses_posix_on_non_windows(monkeypatch, tmp_path: Path):
    import stream2video.completion_sound as cs

    monkeypatch.setattr(cs.sys, "platform", "darwin")
    monkeypatch.setattr(cs, "ensure_completion_chime", lambda *a, **k: tmp_path / "chime.wav")
    called: dict[str, Path] = {}

    def fake_posix(chime: Path):
        called["path"] = chime
        return None

    monkeypatch.setattr(cs, "_play_with_posix", fake_posix)
    assert play_completion_sound(enabled=True, kind="success") is None
    assert called["path"] == tmp_path / "chime.wav"


def test_posix_fallback_warns_when_no_player(monkeypatch, tmp_path: Path):
    import stream2video.completion_sound as cs

    monkeypatch.setattr(cs.shutil, "which", lambda _exe: None)
    spawned: list[str] = []
    monkeypatch.setattr(cs.subprocess, "Popen", lambda cmd, **kw: spawned.append(cmd[0]))

    warning = cs._play_with_posix(tmp_path / "chime.wav")
    assert warning is not None and "No audio player found" in warning
    assert spawned == []


def test_posix_uses_first_available_player(monkeypatch, tmp_path: Path):
    import stream2video.completion_sound as cs

    # Pretend only aplay is installed.
    monkeypatch.setattr(
        cs.shutil, "which", lambda exe: "/usr/bin/aplay" if exe == "aplay" else None
    )
    spawned: list[list[str]] = []

    def fake_popen(cmd, **kw):
        spawned.append(cmd)
        return None

    monkeypatch.setattr(cs.subprocess, "Popen", fake_popen)
    assert cs._play_with_posix(tmp_path / "chime.wav") is None
    assert spawned == [["aplay", "-q", str(tmp_path / "chime.wav")]]
