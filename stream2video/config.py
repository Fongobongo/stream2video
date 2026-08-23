"""Shared configuration defaults and validation ranges.

Audit round 31 P3: the tunable metadata used to live TWICE — the
defaults / ranges / enum whitelists below AND inside
``param_specs.PARAM_SPECS`` — and the two tables had to be kept in
sync by hand. ``param_specs`` is now the single source of truth for
every pipeline parameter (default, type, bounds, choices, CLI flag),
and this module derives its public views from it:

  * ``CONFIG_DEFAULTS``  = PARAM_SPECS defaults + session-only keys;
  * ``CONFIG_RANGES``    = PARAM_SPECS min/max column;
  * ``ENUM_VALIDATORS``  = PARAM_SPECS enum choices (+ the GUI theme).

Old consumers keep working unchanged — the derived views carry the
exact same names, shapes and values. Only the duplication is gone.

What stays EXPLICIT here (deliberately, audit "what not to touch"):
the PipelineConfig dataclass fields, the session/GUI state keys
(``output_dir`` / ``theme`` / ``recent_projects`` are NOT pipeline
parameters), ``OUTPUT_FORMAT_SPECS`` and the loader/coercion logic.
"""

import json
import logging
import math
import os
from pathlib import Path
from typing import Any

from stream2video.param_specs import (
    DEFAULT_PRESET,
    PRESET_NAMES,
    PRESETS,
    SPEC_DEFAULTS,
    SPEC_ENUM_CHOICES,
    SPEC_RANGES,
    VALID_DOWNLOAD_QUALITIES,
    VALID_ENCODERS,
    VALID_METHODS,
    VALID_OUTPUT_FORMATS,
    VALID_OUTPUT_FPS,
    VALID_QUALITIES,
    VALID_SOFTWARE_FALLBACKS,
    VALID_THEMES,
    VALID_X264_PRESETS,
)

# Re-exported so existing ``from stream2video.config import ...``
# consumers (GUI modules, encoders, the pipeline validator) keep
# working after the param_specs consolidation. Do not add NEW imports
# of these names via config — import them from param_specs.
__all__ = [
    "AUTO_OR_INT_KEYS",
    "CONFIG_DEFAULTS",
    "CONFIG_RANGES",
    "DEFAULT_PRESET",
    "ENUM_VALIDATORS",
    "OUTPUT_FORMAT_SPECS",
    "PRESETS",
    "PRESET_NAMES",
    "USER_DEFAULT_KEYS",
    "VALID_DOWNLOAD_QUALITIES",
    "VALID_ENCODERS",
    "VALID_METHODS",
    "VALID_OUTPUT_FORMATS",
    "VALID_OUTPUT_FPS",
    "VALID_QUALITIES",
    "VALID_SOFTWARE_FALLBACKS",
    "VALID_THEMES",
    "VALID_X264_PRESETS",
    "apply_preset",
    "coerce_typed_value",
    "effective_defaults",
    "load_user_defaults",
    "save_user_defaults",
    "settings_path",
    "user_default_overrides",
    "user_defaults_path",
]

logger = logging.getLogger(__name__)

# Meaning of the non-obvious defaults (the values themselves live in
# ``param_specs.PARAM_SPECS`` — see the ``default`` column there):
#
#   * encoder_threads="auto": let ffmpeg decide (-threads 0, usually
#     one per logical core); an int caps it. ``auto`` preserves the
#     historical behaviour (no thread hint).
#   * output_fps="source": preserve the input's frame cadence — no
#     -r / -fps_mode is added, so a 30 FPS source comes out at 30 FPS
#     without frame duplication. Integer values force a CFR conversion
#     via the ``fps`` filter (docs warn about the size/quality cost of
#     duplicated frames).
#   * memory_limit_mb="auto": 60% of total RAM at the start of the
#     run; a positive int is a MB cap; 0/None disables the budget
#     check (only the OS reserve remains).
#   * memory_reserve_mb=2048: warning floor for available RAM. A
#     pre-flight check or the encode-time monitor logs below it but
#     does NOT cancel running work — cancelling a multi-minute encode
#     on a transient system-wide dip would lose the work already done,
#     and Windows recovers from memory pressure by trimming standby /
#     paging long before a real failure. Only the ffmpeg process's own
#     RSS budget cancels a running encode. The pre-flight still refuses
#     to START a new heavy phase below the floor.
#   * x264_low_memory=False: when True, adds
#     ``-x264-params rc-lookahead=10:ref=1:bframes=0`` — slightly worse
#     compression for significantly lower peak RAM (4-8 GB machines).
#   * use_crf=False: quality-fixed video encoding instead of
#     bitrate-fixed targets (libx264 CRF, NVENC/AMF CQ/QP, MF quality
#     mode). False preserves bitrate parity (10M/7M/3.5M or source).
#   * gapless_concat=True: the segment path's final join uses the
#     ``concat`` filter (re-encode) instead of the demuxer (stream
#     copy) — per-segment AAC priming (~21 ms at 48 kHz) accumulates
#     as A/V drift on multi-segment outputs; the filter adds priming
#     once. ``cut_then_encode`` already achieves this but sacrifices
#     frame accuracy (-c copy snaps to keyframes); ``gapless_concat``
#     keeps frame accuracy AND gapless audio.
#   * low_process_priority=False: when True, ffmpeg subprocesses are
#     spawned at BELOW_NORMAL_PRIORITY_CLASS on Windows and nice +10
#     on POSIX so a long encode doesn't starve interactive apps.
#   * rlimit_as_mb=0: RLIMIT_AS cap for ffmpeg subprocesses
#     (POSIX-only). malloc/mmap return ENOMEM before the OS swaps or
#     the OOM killer kicks in — a hard kernel-enforced cap
#     complementing the in-process ``memory_limit_mb`` pre-flight.
#     No-op on Windows.
#   * download/connect/no_progress timeouts: absolute ceiling +
#     two-stage watchdog so a stalled connection doesn't wait the full
#     ceiling.
#   * proxy=""/proxy_active=False: the proxy address is always kept
#     (so it isn't lost when temporarily disabled); only when the gate
#     is on is it passed to yt-dlp as --proxy.
#   * output_format="video": the audio-only values produce a standalone
#     audio file (video stream dropped) using the codec that matches
#     the container's conventional codec choice — see
#     OUTPUT_FORMAT_SPECS for the mapping. ``audio_quality`` controls
#     the bitrate for lossy formats; lossless formats (wav, flac)
#     ignore it. ``source`` omits the bitrate and the -ar/-ac policy
#     so ffmpeg keeps the native sample rate/channel layout.
CONFIG_DEFAULTS: dict[str, Any] = {
    # Every PARAM_SPECS entry carries its own default (derived view —
    # audit round 31 P3): adding a tunable to the spec table adds its
    # default here automatically.
    **SPEC_DEFAULTS,
    # Session-only / GUI-state keys — deliberately OUTSIDE PARAM_SPECS
    # (not pipeline parameters, no CLI flags, not validated as config):
    "output_dir": "",
    "theme": "dark",
    "recent_projects": [],
}

# ---------------------------------------------------------------------------
# Resource presets (low_memory / low_cpu / balanced /
# maximum_performance): see ``param_specs.PRESETS`` for the tunable
# rationale — the table moved there in audit round 31 P3 so it lives
# next to the spec entries it overrides; ``apply_preset`` stays here
# because it is a config-layer transform.
# ---------------------------------------------------------------------------


def apply_preset(
    config: dict[str, Any],
    preset: str,
    explicit_keys: frozenset[str] | None = None,
    protected_keys: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Return a new config dict with the preset's tunables applied.

    Pure: doesn't mutate ``config``. Unknown ``preset`` raises
    ``ValueError`` so the CLI / GUI can surface the typo.

    Presets only overlay the keys listed in ``PRESETS[preset]``;
    everything else the user set is preserved verbatim (YAML values,
    GUI checkbox choices). ``balanced`` is the identity preset — the
    returned dict differs from ``config`` only in the ``preset`` key.

    ``explicit_keys`` is the set of keys the user EXPLICITLY wrote (the
    YAML file, per ``load_config``). The preset applies first as a
    baseline, then each explicitly-written preset-managed key is
    re-applied on top — "explicit keys win per-key" (audit round 13 P1:
    ``preset: low_memory`` + ``batch_chunk_size: 50`` in one YAML file
    used to run ``batch_chunk_size=20`` because the preset overlay ran
    after the merge and won).

    ``protected_keys`` is the set of keys carrying a deliberate user
    choice that must survive the preset even when NOT explicitly written
    in the YAML — the GUI's "Save current as defaults" values
    (user_defaults.json). Without it, ``--preset low_memory`` silently
    overwrote e.g. a saved ``x264_low_memory: false`` while the exact
    same choice in a YAML file was honoured. See
    :func:`user_default_overrides` for how the set is derived.
    """
    if preset not in PRESETS:
        raise ValueError(
            f"Unknown preset {preset!r} (use {' or '.join(repr(p) for p in PRESET_NAMES)})"
        )
    out = dict(config)
    overrides = PRESETS[preset]
    out.update(overrides)
    keep = (explicit_keys or frozenset()) | (protected_keys or frozenset())
    if keep:
        for key in overrides:
            if key in keep:
                out[key] = config[key]
    out["preset"] = preset
    return out


def user_default_overrides(effective: dict[str, Any] | None = None) -> frozenset[str]:
    """Keys whose value in ``effective`` DIFFERS from the stock default.

    ``effective_defaults()`` = ``CONFIG_DEFAULTS`` overlaid with
    user_defaults.json, so this diff is exactly the set of the user's
    saved GUI choices with a non-default value — the set
    :func:`apply_preset` must protect.
    """
    eff = effective_defaults() if effective is None else effective
    return frozenset(
        k for k, v in eff.items() if k in CONFIG_DEFAULTS and v != CONFIG_DEFAULTS[k]
    )


# Numeric bounds, derived from the PARAM_SPECS min/max column (audit
# round 31 P3). The old hand-maintained table carried one bound entry
# per key; the spec table now owns them, so this view CANNOT drift.
# Semantic notes that belonged to the bounds:
#
#   * phase timeouts: 1 s floor rejects typos / accidental zero; the
#     7-day ceiling accommodates pathological long-running encodes
#     without disabling the watchdog;
#   * stall_kill/stall_warning floors: a typo'd sub-floor timeout would
#     turn the watchdog into a kill-on-startup on slow media;
#   * memory budgets: 0 disables the cap, the ceiling is a pure
#     overflow guard, far beyond any real machine;
#   * encoder_threads: the 1024 ceiling is a typo guard — a stray digit
#     would spawn a thread storm on any real box.
CONFIG_RANGES: dict[str, tuple[float, float]] = dict(SPEC_RANGES)

# Keys whose value may be the literal ``"auto"`` instead of a number —
# the numeric ``CONFIG_RANGES`` bound only applies to the int form.
# Single source of truth for every place that must skip ``"auto"`` before
# a numeric comparison: ``cli_config.load_config`` (would otherwise crash
# on the default config value at every startup), ``pipeline_controller
# .validate_pipeline_config`` (the loop over CONFIG_RANGES), and the GUI.
AUTO_OR_INT_KEYS: frozenset[str] = frozenset({"encoder_threads", "memory_limit_mb"})

# All VALID_* whitelists moved to ``param_specs`` in audit round 31 P3
# and are re-exported at the top of this module — see the import list
# there. They live next to the PARAM_SPECS entries that reference them.

# Per-format encoder/container spec used by the audio-extract path in
# concat.py. Keys mirror the entries in ``VALID_OUTPUT_FORMATS``
# (except ``video``, which doesn't go through the audio path).
#
# Each spec carries:
#   * ``codec``    — ffmpeg encoder name (e.g. ``libmp3lame``);
#   * ``ext``      — file extension without the dot;
#   * ``lossless`` — True for wav/flac (``audio_quality`` bitrate is
#     ignored, encoder runs in its native lossless mode);
#   * ``extra_opts`` — output-side options appended after ``-c:a``
#     (e.g. ``-compression_level 5`` for flac). Empty list for codecs
#     that don't need extra tuning.
#
# The bitrate knob (``-b:a``) is added by the audio-extract code path
# from ``_audio_bitrate_opts()`` so it stays consistent with the existing
# ``audio_quality`` presets; lossless formats and ``source`` skip it.
OUTPUT_FORMAT_SPECS: dict[str, dict[str, Any]] = {
    "mp3": {
        "codec": "libmp3lame",
        "ext": "mp3",
        "lossless": False,
        "extra_opts": [],
    },
    "opus": {
        "codec": "libopus",
        "ext": "opus",
        "lossless": False,
        # libopus defaults to 48 kHz stereo; the ``-application audio``
        # hint biases the encoder toward music-quality VBR rather than
        # voip-low-delay. ``-vbr on`` is the default but kept explicit
        # so a future ffmpeg build that changes the default doesn't
        # silently change the output quality.
        "extra_opts": ["-application", "audio", "-vbr", "on"],
    },
    "aac": {
        "codec": "aac",
        "ext": "m4a",
        "lossless": False,
        # MP4 container requires +faststart for HTTP progressive
        # playback; without it the moov atom sits at the end and the
        # browser must download the whole file before playing.
        "extra_opts": ["-movflags", "+faststart"],
    },
    "wav": {
        "codec": "pcm_s16le",
        "ext": "wav",
        "lossless": True,
        "extra_opts": [],
    },
    "flac": {
        "codec": "flac",
        "ext": "flac",
        "lossless": True,
        # 0=fastest, 12=smallest; 5 is ffmpeg's default and the sweet
        # spot for music sources (encoding ~2-3x realtime on a modern
        # CPU, ~50-60% the size of the WAV).
        "extra_opts": ["-compression_level", "5"],
    },
}

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
    "preset",
    "software_fallback",
    "x264_preset",
    "encoder_threads",
    "output_fps",
    "output_format",
    "memory_limit_mb",
    "memory_reserve_mb",
    "x264_low_memory",
    "use_crf",
    "gapless_concat",
    "low_process_priority",
    "rlimit_as_mb",
    "download_timeout",
    "connect_timeout",
    "no_progress_timeout",
    "proxy",
    "proxy_active",
    # Pipeline phase timeouts + tuning
    "segment_encode_timeout",
    "final_concat_timeout",
    "silence_timeout",
    "stall_kill_timeout",
    "stall_warning_timeout",
    "waveform_timeout",
    "batch_chunk_size",
    "min_part_bytes",
    "force",
    "delete_after",
    "per_video_dir",
    "completion_sound",
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
    element type is also validated — a list containing non-str entries
    (e.g. ``[42, null, Path('/x')]``) is dropped entirely so a later
    ``json.dump`` in the GUI can't crash on a non-serialisable element.
    An empty list is accepted. The expected element type comes from
    ``_LIST_ELEMENT_TYPES`` (sampling ``default[0]`` doesn't work: the
    only list default is empty, so the sample-based check was dead
    code).
    """
    if key not in CONFIG_DEFAULTS:
        return None
    default = CONFIG_DEFAULTS[key]
    # Special case: ``encoder_threads`` accepts ``"auto"`` (str default)
    # OR a positive int from the user. The two types are both legitimately
    # expressions of the same setting, so accept either explicitly. A
    # non-positive int is dropped (it would be a no-op or harmful hint
    # to ffmpeg's thread pool — negative values raise on the CLI side).
    # ``"auto"`` is matched case-insensitively and normalised to the
    # canonical lowercase form — the same rule the CLI flag
    # (cli_resolver) and the GUI's Advanced entry (settings_io) apply,
    # so one value has one spelling rule across all surfaces.
    if key == "encoder_threads":
        lo, hi = CONFIG_RANGES["encoder_threads"]
        if isinstance(value, str) and value.strip().lower() == "auto":
            return "auto"
        if isinstance(value, bool):
            return None
        if isinstance(value, int) and lo <= value <= hi:
            return value
        return None
    # ``memory_limit_mb`` accepts ``"auto"`` or a non-negative int
    # (0 = disable). A negative int is rejected; an INTEGRAL float is
    # coerced to int (ffmpeg memory budgets are inherently
    # coarse-grained) while a fractional float (``1.9``) is rejected —
    # silently flooring a limit to 1 MB is a dangerous quiet change.
    if key == "memory_limit_mb":
        lo, hi = CONFIG_RANGES["memory_limit_mb"]
        if isinstance(value, str) and value.strip().lower() == "auto":
            return "auto"
        if isinstance(value, bool):
            return None
        if isinstance(value, int) and lo <= value <= hi:
            return value
        if isinstance(value, float) and value.is_integer() and lo <= value <= hi:
            return int(value)
        return None
    if key == "preset":
        return value if isinstance(value, str) and value in PRESET_NAMES else None
    if isinstance(default, bool):
        return value if isinstance(value, bool) else None
    if isinstance(default, int | float):
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        # ``json.load`` accepts the non-standard NaN / Infinity tokens,
        # so a hand-edited settings.json / user_defaults.json can smuggle
        # a non-finite float past every surface fixed in audit round 15
        # P1 (CLI flag, YAML, slider parser, pipeline validator) and
        # poison the GUI's startup state — where 'Save current as
        # defaults' could then re-persist it (audit round 16 P1). Drop
        # non-finite values here so the loaders can never return them.
        if isinstance(value, float) and not math.isfinite(value):
            return None
        # YAML/JSON numbers have no int/float distinction the way
        # Python does — ``2048.0`` parses as float. For an INT-typed
        # default, coerce integral floats back to int so a
        # ``memory_reserve_mb: 2048.0`` doesn't leak a float into an
        # int-typed PipelineConfig slot (mirrors the ``memory_limit_mb``
        # special case above). A NON-integral float on an int-typed
        # key (``download_timeout: 1.9``) is REJECTED rather than
        # silently floored to ``1`` — for timeouts and limits a quiet
        # round-down changes behaviour the user never asked for.
        if isinstance(default, int) and isinstance(value, float):
            if not value.is_integer():
                return None
            value = int(value)
        # Range-check EVERY numeric key against the same CONFIG_RANGES
        # the CLI YAML path and ``validate_pipeline_config`` enforce
        # (audit round 18 P2) — an out-of-range but finite value
        # (``batch_chunk_size: 999999``, ``stall_kill_timeout: 1``) in
        # settings.json / user_defaults.json used to load into the GUI
        # and effective defaults, then only fail later at pipeline
        # validation, leaving the saved defaults unusable without manual
        # file surgery. Keys without a CONFIG_RANGES entry keep the
        # unbounded contract.
        lo_f: float = -math.inf
        hi_f: float = math.inf
        if key in CONFIG_RANGES:
            lo_f, hi_f = CONFIG_RANGES[key]
        if not lo_f <= value <= hi_f:
            return None
        return value
    if isinstance(default, str):
        if not isinstance(value, str):
            return None
        # Enum-valued string keys (method, encoder, *_quality, theme,
        # ...) are checked against their VALID_* whitelist so a
        # hand-edited settings.json / user_defaults.json can't smuggle
        # a bogus value past the GUI's comboboxes and crash the run
        # mid-pipeline with an obscure ffmpeg/validator error.
        valid = ENUM_VALIDATORS.get(key)
        if valid is not None and value not in valid:
            return None
        return value
    if isinstance(default, list):
        if not isinstance(value, list):
            return None
        # Validate element types against the expected element type.
        # Sampling ``type(default[0])`` only works for NON-empty
        # defaults — the sole list default (``recent_projects``) is
        # empty, so ``if default:`` was always False and element types
        # were never actually validated. Use the registry first; the
        # sample remains as a fallback for any future non-empty list
        # default that forgets to register.
        elem_type = _LIST_ELEMENT_TYPES.get(key)
        if elem_type is None and default:
            elem_type = type(default[0])
        if elem_type is not None and not all(isinstance(e, elem_type) for e in value):
            return None
        return value
    return None


# Expected element type for list-typed defaults whose CONFIG_DEFAULTS
# entry is EMPTY (so the entry itself can't reveal the element type).
# Keep in sync with CONFIG_DEFAULTS; used by coerce_typed_value.
_LIST_ELEMENT_TYPES: dict[str, type] = {"recent_projects": str}

# Enum-valued string keys validated by coerce_typed_value against their
# whitelists. Keys WITHOUT an entry keep the historical type-only check.
# Derived from the PARAM_SPECS enum column (audit round 31 P3), plus the
# GUI-only ``theme`` key (session state, deliberately not a PARAM_SPECS
# entry). The CLI derives its config-file enum checks from this view
# (see cli_config.load_config, which drops the ``theme`` key).
ENUM_VALIDATORS: dict[str, tuple[str, ...]] = {
    **SPEC_ENUM_CHOICES,
    "theme": tuple(VALID_THEMES),
}


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
    # tempfile.mkstemp instead of a deterministic `<name>.tmp`: a
    # GUI-close autosave racing a "Save current as defaults" click in
    # the same process used to open the same pathname twice and
    # interleave writes, leaving a mixed JSON that ``load_user_defaults``
    # then failed to decode → user lost their defaults. mkstemp gives
    # each call its own file and ``os.replace`` serialises publication.
    import contextlib
    import tempfile

    fd, tmp_str = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            # allow_nan=False: a NaN reaching the writer (a hand-edited
            # value that slipped past loading, or a programmatic caller)
            # must fail the save loudly instead of persisting a token
            # that ``json.load`` will happily read back — re-creating
            # the poisoned startup state (audit round 16 P1).
            json.dump(payload, f, indent=2, ensure_ascii=False, allow_nan=False)
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def effective_defaults() -> dict[str, Any]:
    """CONFIG_DEFAULTS overlaid with user_defaults.json overrides."""
    out = {k: (list(v) if isinstance(v, list) else v) for k, v in CONFIG_DEFAULTS.items()}
    out.update(load_user_defaults())
    return out
