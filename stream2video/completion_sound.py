"""Short optional GUI completion sounds.

The chimes are synthesized with the Python standard library so the app
doesn't need to ship or license external audio assets.
"""

from __future__ import annotations

import logging
import math
import shutil
import struct
import subprocess
import sys
import wave
from pathlib import Path

from stream2video.config import settings_path

logger = logging.getLogger(__name__)

SAMPLE_RATE = 44_100
_AMPLITUDE = 0.22
_SUCCESS_CHIME_FILENAME = "completion_success.wav"
_ATTENTION_CHIME_FILENAME = "completion_attention.wav"
_VALID_KINDS = frozenset({"success", "attention"})


def completion_chime_path(kind: str = "success") -> Path:
    """Return the cached path used for the generated completion chime."""
    if kind not in _VALID_KINDS:
        raise ValueError(f"Unknown completion sound kind: {kind!r}")
    filename = _SUCCESS_CHIME_FILENAME if kind == "success" else _ATTENTION_CHIME_FILENAME
    return settings_path().parent / filename


def ensure_completion_chime(path: Path | None = None, *, kind: str = "success") -> Path:
    """Create the bundled-free completion chime if needed and return it."""
    out = path or completion_chime_path(kind)
    if out.exists() and out.stat().st_size > 44:
        return out

    out.parent.mkdir(parents=True, exist_ok=True)
    samples = _build_chime_samples(kind)
    # Write via a temp file + atomic rename so two GUI instances that
    # race past the ``st_size > 44`` early-out above don't leave a
    # half-written WAV that the *other* process then plays as static.
    tmp = out.parent / f".{out.name}.{Path(__file__).name}"
    try:
        with wave.open(str(tmp), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(b"".join(struct.pack("<h", sample) for sample in samples))
        tmp.replace(out)
    except OSError:
        # If the rename fails (file locked by another player), fall back
        # to the un-atomic path — the chime is non-critical.
        with wave.open(str(out), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(b"".join(struct.pack("<h", sample) for sample in samples))
    finally:
        # Clean the temp file if the rename never ran.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return out


def _play_with_winsound(chime: Path) -> str | None:
    import winsound

    winsound.PlaySound(
        str(chime),
        winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
    )
    return None


# POSIX players in probe order. Each entry is (argv-template, executables) —
# the first executable found in PATH wins. All of these play unconditionally
# asynchronously and exit when the file ends, so no SND_ASYNC equivalent is
# needed.
_POSIX_PLAYERS: tuple[tuple[str, ...], ...] = (
    ("afplay",),  # macOS (preinstalled)
    ("paplay",),  # PulseAudio / PipeWire
    ("aplay", "-q"),  # ALSA
    ("ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"),  # ffmpeg fallback
)


def _play_with_posix(chime: Path) -> str | None:
    for entry in _POSIX_PLAYERS:
        exe = entry[0]
        if shutil.which(exe) is None:
            continue
        subprocess.Popen(
            [exe, *entry[1:], str(chime)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return None
    return (
        "No audio player found. Install one of: afplay (macOS), paplay "
        "(pulseaudio-utils), aplay (alsa-utils), or ffplay (ffmpeg)."
    )


def play_completion_sound(*, enabled: bool, kind: str = "success") -> str | None:
    """Play the completion chime asynchronously.

    Returns ``None`` on success/disabled, or a warning string if playback
    is unavailable. The pipeline treats sound as a best-effort nicety.

    Backends: Windows ``winsound``; macOS ``afplay``; Linux ``paplay`` /
    ``aplay`` / ``ffplay`` (first available in PATH).
    """
    if not enabled:
        return None
    if kind not in _VALID_KINDS:
        return f"Unknown completion sound kind: {kind}"

    try:
        chime = ensure_completion_chime(kind=kind)
        if sys.platform == "win32":
            return _play_with_winsound(chime)
        return _play_with_posix(chime)
    except Exception as exc:
        logger.debug("Completion sound playback failed", exc_info=True)
        return f"Could not play completion sound: {exc}"


def _build_chime_samples(kind: str) -> list[int]:
    notes: tuple[tuple[float, float, float], ...]
    if kind == "attention":
        notes = (
            (440.00, 0.00, 0.26),
            (329.63, 0.22, 0.34),
        )
        total_duration = 0.64
    else:
        notes = (
            (523.25, 0.00, 0.30),
            (659.25, 0.16, 0.32),
            (783.99, 0.32, 0.42),
        )
        total_duration = 0.86
    total_samples = int(SAMPLE_RATE * total_duration)
    samples: list[int] = []

    for index in range(total_samples):
        t = index / SAMPLE_RATE
        value = 0.0
        for freq, start, duration in notes:
            local_t = t - start
            if 0.0 <= local_t <= duration:
                envelope = _note_envelope(local_t, duration)
                value += math.sin(2.0 * math.pi * freq * local_t) * envelope
        value *= _AMPLITUDE / len(notes)
        samples.append(int(max(-1.0, min(1.0, value)) * 32767))
    return samples


def _note_envelope(t: float, duration: float) -> float:
    attack = 0.025
    release = 0.16
    if t < attack:
        return t / attack
    if t > duration - release:
        return max(0.0, (duration - t) / release)
    return 1.0
