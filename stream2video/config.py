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
    "video_quality": "source",
    "audio_quality": "source",
    "download_quality": "best",
    "software_fallback": "ask",
    "x264_preset": "medium",
    # Encoder thread budget. ``auto`` = let ffmpeg decide (-threads 0,
    # which usually picks one per logical core); an int caps it. ``auto``
    # preserves the historical behaviour (no thread hint) so an upgrade
    # doesn't quietly change the load profile of an existing user.
    "encoder_threads": "auto",
    # Output FPS policy (P1.17). ``source`` (default) preserves the
    # input's frame cadence — no -r / -fps_mode is added to the encoder
    # command, so a 30 FPS source comes out at 30 FPS without frame
    # duplication. ``24`` / ``25`` / ``30`` / ``50`` / ``60`` force a
    # CFR conversion via the ``fps`` filter; the docs warn about the
    # size/quality cost of duplicated frames.
    "output_fps": "source",
    # RAM budget (P1.17 / Этап 8A). ``auto`` = 60% of total RAM at the
    # start of the run; a positive int is taken as a MB cap. ``None`` /
    # ``0`` disables the budget check (only the OS reserve remains).
    "memory_limit_mb": "auto",
    # Warning floor for available RAM. When a pre-flight check or the
    # encode-time monitor sees available RAM below this, it logs a
    # warning but does NOT cancel running work — cancelling a
    # multi-minute encode on a transient system-wide dip would lose the
    # work already done, and Windows recovers from memory pressure by
    # trimming standby / paging long before a real failure. Only the
    # ffmpeg process's own RSS budget (``memory_limit_mb``) cancels a
    # running encode. The pre-flight check still refuses to START a new
    # heavy phase below this floor. 2 GB matches the default Windows
    # commit limit behaviour for the System process; raise it on
    # memory-constrained laptops.
    "memory_reserve_mb": 2048,
    # Reduce x264 frame-buffer footprint when True. Adds
    # ``-x264-params rc-lookahead=10:ref=1:bframes=0`` to the encoder
    # command, which trades slightly worse compression for significantly
    # lower peak RAM during encode. Useful on memory-constrained machines
    # (4-8 GB RAM) where a long libx264 encode would otherwise push the
    # process into swap.
    "x264_low_memory": False,
    # Use quality-fixed video encoding instead of bitrate-fixed targets.
    # libx264 uses CRF, NVENC/AMF use CQ/QP-style modes, and MF uses its
    # quality rate-control mode. Default False preserves bitrate parity
    # across encoders (10M/7M/3.5M or source bitrate).
    "use_crf": False,
    # Gapless concat (AAC priming fix). When True, the segment path's
    # final join uses the ``concat`` filter (re-encode) instead of the
    # concat demuxer (stream copy). The concat demuxer preserves per-
    # segment AAC priming (~21ms per segment at 48kHz), which
    # accumulates as A/V drift on multi-segment outputs — 10 segments
    # drift ~170ms. The concat filter re-encodes through a single PCM
    # pipeline so priming is added only once (not per-segment), giving
    # gapless output at the cost of one extra audio encode pass.
    # ``cut_then_encode`` already achieves this (one encode pass total),
    # but it sacrifices frame accuracy (-c copy snaps to keyframes);
    # ``gapless_concat`` keeps frame accuracy AND gapless audio. Default
    # False preserves the historical behaviour (concat demuxer, faster).
    # Default True: per-segment AAC priming (~21ms at 48kHz) accumulates
    # as A/V drift on multi-segment outputs — the gapless concat filter
    # adds priming only once. Users who want the old (faster, concat
    # demuxer) behaviour can flip it off.
    "gapless_concat": True,
    # Lower ffmpeg scheduling priority (opt-in, P3.x). When True, ffmpeg
    # subprocesses are spawned at BELOW_NORMAL_PRIORITY_CLASS on Windows
    # and nice +10 on POSIX so a long-running encode doesn't starve
    # interactive applications. Useful for unattended batch processing
    # on shared/desktop machines. Default False preserves the historical
    # behaviour (normal priority, faster encoding).
    "low_process_priority": False,
    # RLIMIT_AS cap for ffmpeg subprocesses (POSIX-only, opt-in, P3.x).
    # When > 0, every spawned ffmpeg subprocess is forked with
    # ``resource.setrlimit(RLIMIT_AS, (cap, cap))`` in preexec_fn so
    # it cannot allocate more than this many MiB of virtual address
    # space. malloc / mmap return ENOMEM (and ffmpeg bails) before the
    # OS swaps or the Linux OOM killer kicks in. This is a hard,
    # kernel-enforced cap complementing the in-process
    # ``memory_limit_mb`` pre-flight check (which only samples RSS
    # *between* wall-clock polls and can miss a fast spike). No-op on
    # Windows (no portable equivalent; ``memory_limit_mb`` remains the
    # only memory door there). 0 disables the cap (default) and
    # preserves the historical behaviour.
    "rlimit_as_mb": 0,
    # Download watchdog timeouts (P1.6). Absolute ceiling + two-stage
    # watchdog so a stalled connection doesn't wait the full ceiling.
    # Exposed via --download-timeout / --connect-timeout /
    # --no-progress-timeout in the CLI; the GUI uses these defaults.
    "download_timeout": 28800,  # 8h
    "connect_timeout": 300,  # 5 min pre-first-byte
    "no_progress_timeout": 1800,  # 30 min mid-download stall
    # Proxy server used for downloads, e.g. "http://127.0.0.1:8080" or
    # "socks5://user:pass@host:1080". Empty string = no proxy address.
    # Passed to yt-dlp as --proxy when "proxy_active" is enabled.
    "proxy": "",
    # Whether the configured proxy is actually used for downloads. The
    # address in "proxy" is always kept (so it's not lost when the proxy
    # is temporarily disabled; the dialog re-opens prefilled).
    "proxy_active": False,
    # Pipeline phase timeouts (P3.4). Exposed via CLI flags
    # (--segment-timeout / --final-concat-timeout / --silence-timeout
    # / --stall-timeout) and plumbed through PipelineConfig; module-
    # level constants in concat.py / silence.py remain as fallbacks
    # for direct callers that don't pass config-derived values.
    "segment_encode_timeout": 600,  # 10 min per segment encode
    "final_concat_timeout": 86400,  # 24h absolute ceiling on final concat
    "silence_timeout": 36000,  # 10h silence detection ceiling
    "stall_kill_timeout": 300,  # 5 min no-progress -> kill ffmpeg
    "stall_warning_timeout": 120,  # 2 min no-progress -> warn
    # Waveform preview decode timeout (P3.4). Bounds the ffmpeg
    # invocation that reads peaks for the popup.
    "waveform_timeout": 300,  # 5 min
    # Batch chunk size (P3.4). Number of keep-segments per batch
    # filter invocation; scaled down dynamically for large counts.
    "batch_chunk_size": 40,
    # Minimum bytes for a resumed part file to be considered valid
    # (P3.4). Smaller files are treated as corrupt and re-encoded.
    "min_part_bytes": 1024,
    "preset": "balanced",
    "force": False,
    "delete_after": False,
    "per_video_dir": True,
    "completion_sound": True,
    "output_dir": "",
    # Output container / codec policy. ``video`` (default) preserves the
    # historical behaviour: H.264 video + AAC stereo audio muxed into
    # MP4. The audio-only values produce a standalone audio file (the
    # video stream is dropped) using the codec that matches the
    # container's conventional codec choice:
    #   * ``mp3``  → .mp3 + libmp3lame
    #   * ``opus`` → .opus + libopus
    #   * ``aac``  → .m4a + aac (native ffmpeg encoder, AAC-LC)
    #   * ``wav``  → .wav + pcm_s16le (lossless, 48 kHz / 16-bit)
    #   * ``flac`` → .flac + flac (lossless, compressed)
    # ``audio_quality`` controls the bitrate for lossy formats; lossless
    # formats (wav, flac) ignore it. ``source`` omits the bitrate and
    # ``-ar 48000 -ac 2`` policy so ffmpeg keeps the decoded stream's
    # native sample rate/channel layout where the output codec allows it.
    "output_format": "video",
    "theme": "dark",
    "recent_projects": [],
}

# ---------------------------------------------------------------------------
# Resource presets (P3.x). Bundle existing tunables (x264_low_memory,
# memory_limit_mb, memory_reserve_mb, batch_chunk_size, low_process_priority,
# encoder_threads) into three named profiles so a user can pick a goal at a
# glance instead of toggling six flags. ``balanced`` reproduces the
# historical defaults verbatim; the other two override only the tunables
# listed below — pipeline-only settings (method, encoder, *_quality,
# threshold, min_silence, margin, timeouts) always come from the user's
# existing config and are *never* touched by apply_preset.
#
# ``low_memory`` trades speed for stability on 4-8 GB machines:
#   * x264_low_memory=True → rc-lookahead=10 / ref=1 / bframes=0 (smaller
#     frame-buffer footprint, slightly larger files).
#   * batch_chunk_size=20 (was 40) → smaller filter graphs → fewer
#     decoded frames in RAM per batch invocation.
#   * low_process_priority=True → ffmpeg doesn't compete with the OS / GUI.
#
# ``low_cpu`` minimizes CPU usage for background/unattended encoding:
#   * x264_preset="ultrafast" → fastest encode, larger files.
#   * encoder_threads=2 → limits parallel frame processing.
#   * x264_low_memory=True → further reduces frame-buffer footprint.
#   * low_process_priority=True → ffmpeg runs at below-normal priority.
#
# ``maximum_performance`` trades RAM for throughput:
#   * x264_low_memory=False → full x264 defaults (larger frame buffer).
#   * memory_limit_mb=0 → disables the in-process pre-flight memory budget
#     (the OS reserve is still honoured). Only safe on machines that
#     won't swap; otherwise the Low memory preset is more appropriate.
#   * batch_chunk_size=80 (was 40) → larger batch chunks → fewer filter
#     invocations → less per-chunk startup overhead on long sources.
PRESETS: dict[str, dict[str, Any]] = {
    "low_memory": {
        "x264_low_memory": True,
        "batch_chunk_size": 20,
        "low_process_priority": True,
    },
    "low_cpu": {
        "x264_preset": "ultrafast",
        "encoder_threads": 2,
        "x264_low_memory": True,
        "low_process_priority": True,
    },
    "balanced": {"gapless_concat": True},
    "maximum_performance": {
        "x264_low_memory": False,
        "memory_limit_mb": 0,
        "batch_chunk_size": 80,
    },
}

PRESET_NAMES = tuple(PRESETS.keys())
DEFAULT_PRESET = "balanced"
PRESET_MANAGED_KEYS = frozenset(
    key for name, values in PRESETS.items() if name != DEFAULT_PRESET for key in values
)


def apply_preset(config: dict[str, Any], preset: str) -> dict[str, Any]:
    """Return a new config dict with the preset's tunables applied.

    Pure: doesn't mutate ``config``. Unknown ``preset`` raises
    ``ValueError`` so the CLI / GUI can surface the typo. When ``preset``
    equals ``balanced`` the returned dict resets preset-managed tunables
    to ``CONFIG_DEFAULTS``. This lets the GUI undo a previously selected
    resource preset without requiring a restart.

    Presets only touch the union of tunables listed in ``PRESETS``;
    anything else the user set is preserved. Callers that have explicit
    per-field overrides should apply them after this function.
    """
    if preset not in PRESETS:
        raise ValueError(
            f"Unknown preset {preset!r} (use {' or '.join(repr(p) for p in PRESET_NAMES)})"
        )
    out = dict(config)
    for key in PRESET_MANAGED_KEYS:
        out[key] = CONFIG_DEFAULTS[key]
    out.update(PRESETS[preset])
    out["preset"] = preset
    return out


CONFIG_RANGES = {
    "threshold": (-60, -5),
    "min_silence": (0.1, 60),
    "margin": (-3, 5),
    # Pipeline phase timeouts (P3.4). Lower bound 1s rejects typos /
    # accidental zero; upper bound 7 days accommodates pathological
    # long-running encodes without making the watchdog effectively
    # disabled.
    "segment_encode_timeout": (1, 604800),
    "final_concat_timeout": (1, 604800),
    "silence_timeout": (1, 604800),
    "stall_kill_timeout": (10, 3600),
    "stall_warning_timeout": (5, 1800),
    "waveform_timeout": (10, 3600),
    "batch_chunk_size": (1, 500),
    "min_part_bytes": (1, 10485760),
}

VALID_METHODS: list[str] = ["segment", "batch", "cut_then_encode"]

VALID_ENCODERS: list[str] = ["h264_nvenc", "h264_amf", "h264_mf", "libx264"]

VALID_QUALITIES: list[str] = ["source", "high", "medium", "low"]

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

# Output FPS policy (P1.17). ``source`` preserves the input's frame
# cadence; the integer values force a CFR conversion.
VALID_OUTPUT_FPS: list[str] = ["source", "24", "25", "30", "50", "60"]

# Output container/codec policy. ``video`` keeps the historical
# H.264 + AAC MP4 behaviour; the other values produce standalone
# audio files (video stream dropped). See CONFIG_DEFAULTS for the
# codec/container mapping.
VALID_OUTPUT_FORMATS: list[str] = ["video", "mp3", "opus", "aac", "wav", "flac"]

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
    # Pipeline phase timeouts + tuning (P3.4)
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
    # ``memory_limit_mb`` accepts ``"auto"`` or a non-negative int
    # (0 = disable). A negative int is rejected; float is coerced to
    # int since ffmpeg memory budgets are inherently coarse-grained.
    if key == "memory_limit_mb":
        if isinstance(value, str) and value == "auto":
            return value
        if isinstance(value, bool):
            return None
        if isinstance(value, int | float) and value >= 0:
            return int(value)
        return None
    if key == "preset":
        return value if isinstance(value, str) and value in PRESET_NAMES else None
    if isinstance(default, bool):
        return value if isinstance(value, bool) else None
    if isinstance(default, int | float):
        if isinstance(value, bool) or not isinstance(value, int | float):
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
