"""CLI entry point using Typer."""

import logging
import shutil
import signal
from pathlib import Path
from typing import Any

import typer
from rich.progress import (
    BarColumn,
    Progress,
    TaskID,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

from stream2video.cli_config import load_config as _load_config_impl
from stream2video.pipeline_controller import (
    PipelineConfig as _PipelineConfig,
)
from stream2video.pipeline_controller import (
    validate_pipeline_config as _validate_pipeline_config,
)
from stream2video.pipeline_controller import validate_pipeline_config as _validate_pipeline_config
from stream2video.cli_helpers import (
    ParameterSource,
    _check_ffmpeg,
    _console_handler,
    _make_file_handler,
    _make_sigint_cancel,
    app,
    console,
    logger,
)
from stream2video.concat import CancelledError, ConcatError, cut_and_concat
from stream2video.config import (
    CONFIG_DEFAULTS,
    DEFAULT_PRESET,
    PRESET_NAMES,
    VALID_DOWNLOAD_QUALITIES,
    VALID_ENCODERS,
    VALID_METHODS,
    VALID_OUTPUT_FORMATS,
    VALID_OUTPUT_FPS,
    VALID_QUALITIES,
    VALID_SOFTWARE_FALLBACKS,
    VALID_X264_PRESETS,
    apply_preset,
)
from stream2video.download import (
    DiskSpaceError,
    DownloadCancelledError,
    DownloadError,
    DownloadProgress,
    DownloadTimeoutError,
    PermissionDeniedError,
    URLValidationError,
    VideoNotAvailableError,
    download,
)
from stream2video.gui_helpers import build_download_status
from stream2video.memory import check_memory_reserve
from stream2video.paths import apply_per_video_dir
from stream2video.silence import (
    SilenceCancelledError,
    SilenceDetectionError,
    detect_silence,
    load_silence_cache,
    save_silence_cache,
)


def load_config(config_file: Path | None) -> dict:
    """Thin wrapper: ``cli_config.load_config`` + the module-level ``console``.

    Kept as a thin adapter so ``from stream2video.cli import load_config``
    continues to work, and so a test patching ``stream2video.cli.console``
    would see the output policy the test configured (the helper has no
    console of its own).
    """
    return _load_config_impl(config_file, console)


@app.command()
def main(
    ctx: typer.Context,
    input_video: str = typer.Argument(..., help="URL or path to input video"),
    output_dir: Path = typer.Option(
        Path("./compressed_videos"),
        "--output",
        "-o",
        help="Output directory for compressed video",
    ),
    config_file: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to YAML config file",
    ),
    force: bool | None = typer.Option(
        None,
        "--force",
        "-f",
        help="Re-detect silence, ignore cache. If not passed, falls back to "
        "the config file's `force` key (default False).",
    ),
    method: str = typer.Option(
        CONFIG_DEFAULTS["method"],
        "--method",
        "-m",
        help="Concat method: 'segment' (fast, ~1.5h), 'batch' (select/aselect filter, ~6-7h), "
        "or 'cut_then_encode' (best quality, one encode pass after lossless cut). "
        "If not passed, the config file's `method` key is used.",
    ),
    encoder: str = typer.Option(
        CONFIG_DEFAULTS["encoder"],
        "--encoder",
        "-e",
        help="Video encoder: 'h264_nvenc' (NVIDIA), 'h264_amf' (AMD), 'h264_mf' "
        "(Media Foundation, default), or 'libx264' (CPU fallback). If not passed, "
        "the config file's `encoder` key is used.",
    ),
    video_quality: str = typer.Option(
        CONFIG_DEFAULTS["video_quality"],
        "--video-quality",
        "-vq",
        help="Encode quality preset: 'source' (encoder defaults), 'high' (10000k / CRF 18), "
        "'medium' (7000k / CRF 23, default), or 'low' (3500k / CRF 28). If not passed, "
        "the config file's `video_quality` key is used.",
    ),
    audio_quality: str = typer.Option(
        CONFIG_DEFAULTS["audio_quality"],
        "--audio-quality",
        "-aq",
        help="Audio (AAC) quality preset: 'source' (codec defaults + native rate/channels), "
        "'high' (256k), 'medium' (192k, default), or 'low' (128k). If not passed, "
        "the config file's `audio_quality` key is used.",
    ),
    download_quality: str = typer.Option(
        CONFIG_DEFAULTS["download_quality"],
        "--download-quality",
        "-dq",
        help="Download quality preset (Twitch/YouTube, ignored for local files): "
        "'best' (default), '1080p', '720p', '480p', '360p'. If not passed, the "
        "config file's `download_quality` key is used.",
    ),
    software_fallback: str = typer.Option(
        CONFIG_DEFAULTS["software_fallback"],
        "--software-fallback",
        help="What to do when the requested hardware encoder is unavailable "
        "or fails mid-run: 'ask' (default — refuse silent fallback to libx264; "
        "the run fails with a clear error), 'disabled' (fail immediately), or "
        "'enabled' (silently retry with libx264, legacy behaviour). If not "
        "passed, the config file's `software_fallback` key is used.",
    ),
    x264_preset: str = typer.Option(
        CONFIG_DEFAULTS["x264_preset"],
        "--x264-preset",
        help="libx264 preset (ultrafast..slow, default 'medium'). Faster presets "
        "reduce CPU load at the cost of file size / quality. Use 'ultrafast' or "
        "'veryfast' on an unstable / overclocked CPU. If not passed, the config "
        "file's `x264_preset` key is used.",
    ),
    encoder_threads: str = typer.Option(
        CONFIG_DEFAULTS["encoder_threads"],
        "--encoder-threads",
        help="Encoder thread count: 'auto' (default — let ffmpeg pick, usually "
        "one per logical core) or a positive int to cap libx264's thread pool. "
        "Lowering this reduces peak CPU at the cost of slower encode. If not "
        "passed, the config file's `encoder_threads` key is used.",
    ),
    x264_low_memory: bool = typer.Option(
        False,
        "--x264-low-memory/--no-x264-low-memory",
        help="Reduce x264's frame-buffer footprint via rc-lookahead=10, ref=1, "
        "bframes=0. Produces slightly larger files but uses significantly less "
        "RAM during encode. Useful on memory-constrained machines (4-8 GB).",
    ),
    use_crf: bool = typer.Option(
        False,
        "--use-crf/--no-use-crf",
        help="Use quality-fixed encoding instead of bitrate-fixed "
        "(-b:v source/10000k/7000k/3500k). libx264 uses CRF, NVENC/AMF use "
        "CQ/QP-style modes, and MF uses quality mode. File size varies by "
        "content and encoder. Default off (bitrate parity between encoders).",
    ),
    gapless_concat: bool = typer.Option(
        False,
        "--gapless-concat/--no-gapless-concat",
        help="Re-encode audio in the final concat pass so per-segment AAC "
        "priming (~21ms per segment) doesn't accumulate as A/V drift on "
        "multi-segment outputs. Default off (concat demuxer, faster). "
        "Video is stream-copied; only audio is re-encoded. Equivalent to "
        "cut_then_encode's gapless property but with frame accuracy "
        "(cut_then_encode sacrifices it via -c copy keyframe snap).",
    ),
    low_process_priority: bool = typer.Option(
        False,
        "--low-process-priority/--no-low-process-priority",
        help="Spawn ffmpeg at a lower scheduling priority so a long-running "
        "encode doesn't starve interactive applications. On Windows: "
        "BELOW_NORMAL_PRIORITY_CLASS; on Linux/macOS: nice +10. Useful for "
        "unattended batch processing on shared/desktop machines. Default "
        "off (normal priority, faster encoding).",
    ),
    rlimit_as_mb: int = typer.Option(
        0,
        "--rlimit-as-mb",
        help=(
            "POSIX-only. When >0, cap each spawned ffmpeg subprocess's "
            "virtual address space to this many MiB via RLIMIT_AS in "
            "preexec_fn. malloc/mmap return ENOMEM (and ffmpeg bails) "
            "before the OS swaps or the Linux OOM killer kicks in -- a "
            "hard, kernel-enforced complement to the in-process "
            "--memory-limit-mb pre-flight check (which only samples "
            "RSS between wall-clock polls and can miss a fast spike). "
            "On Windows this flag is ignored (no portable equivalent "
            "of RLIMIT_AS). Default 0 disables the cap."
        ),
    ),
    preset: str = typer.Option(
        DEFAULT_PRESET,
        "--preset",
        help=(
            "Resource preset — bundle of tunables (x264_low_memory, "
            "memory_limit_mb, batch_chunk_size, low_process_priority). "
            "'low_memory' trades speed for stability on 4-8 GB machines "
            "(x264_low_memory=True, batch_chunk_size=20, "
            "low_process_priority=True). 'balanced' (default) reproduces "
            "the historical defaults. 'maximum_performance' trades RAM "
            "for throughput (x264_low_memory=False, memory_limit_mb=0, "
            "batch_chunk_size=80). The preset is applied first, then any "
            "explicit --flag overrides win — so `--preset low_memory "
            "--no-low-process-priority` keeps low_memory's other "
            "tunables but flips low_process_priority back off."
        ),
    ),
    delete_after: bool | None = typer.Option(
        None,
        "--delete-after",
        help="Delete downloaded source file after successful compression. If not "
        "passed, falls back to the config file's `delete_after` key (default False).",
    ),
    per_video_dir: bool | None = typer.Option(
        None,
        "--per-video-dir/--no-per-video-dir",
        help="Group all artifacts (source, WAV cache, JSON cache, output, log) "
        "into a per-video subdirectory. Default follows config/per_video_dir.",
    ),
    log_level: str = typer.Option(
        "INFO",
        "--log-level",
        "-l",
        help="Logging level (DEBUG, INFO, WARNING, ERROR)",
    ),
    download_timeout: int = typer.Option(
        CONFIG_DEFAULTS["download_timeout"],
        "--download-timeout",
        help="Absolute ceiling for the whole download in seconds (default 28800 = 8h, "
        "sized for big VODs). Lower for quick test runs; raise for very large "
        "streams. Ignored for local files.",
    ),
    connect_timeout: int = typer.Option(
        CONFIG_DEFAULTS["connect_timeout"],
        "--connect-timeout",
        help="Seconds to wait for the first progress event (DNS+TLS+handshake+first "
        "byte) before killing yt-dlp with a clear timeout error. Default 300s "
        "(5 min). Increase on very slow / satellite links.",
    ),
    no_progress_timeout: int = typer.Option(
        CONFIG_DEFAULTS["no_progress_timeout"],
        "--no-progress-timeout",
        help="Seconds of silence mid-download before killing yt-dlp (stalled "
        "connection watchdog). Default 1800s (30 min). Increase for very "
        "slow / unstable links where mid-download pauses are normal.",
    ),
    proxy: str = typer.Option(
        CONFIG_DEFAULTS["proxy"],
        "--proxy",
        help="Proxy server to use for downloads, e.g. http://127.0.0.1:8080 "
        "or socks5://user:pass@host:1080. Empty (default) = direct "
        "connection. Passed to yt-dlp.",
    ),
    memory_limit_mb: str = typer.Option(
        str(CONFIG_DEFAULTS["memory_limit_mb"]),
        "--memory-limit-mb",
        help="RAM budget for the encode pipeline: 'auto' (60%% of total RAM, "
        "default) or a positive MB value. Exceeding 95%% of the budget cancels "
        "the running ffmpeg (only ffmpeg's own RSS, not other apps' pressure). "
        "0 disables the budget check. If not passed, the config file's "
        "`memory_limit_mb` key is used.",
    ),
    memory_reserve_mb: int = typer.Option(
        CONFIG_DEFAULTS["memory_reserve_mb"],
        "--memory-reserve-mb",
        help="Available-RAM warning floor in MB. Below this the pipeline logs a "
        "warning but keeps running (cancelling on transient system-wide dips "
        "would lose encode progress). Pre-flight still refuses to start a new "
        "phase below this floor. Default 2048 (2 GB). If not passed, the "
        "config file's `memory_reserve_mb` key is used.",
    ),
    segment_encode_timeout: int = typer.Option(
        CONFIG_DEFAULTS["segment_encode_timeout"],
        "--segment-timeout",
        help="Per-segment encode timeout in seconds (default 600 = 10 min). "
        "Raise for very long segments or slow hardware. If not passed, "
        "the config file's `segment_encode_timeout` key is used.",
    ),
    final_concat_timeout: int = typer.Option(
        CONFIG_DEFAULTS["final_concat_timeout"],
        "--final-concat-timeout",
        help="Final concat-demuxer timeout in seconds (default 86400 = 24 h). "
        "Absolute ceiling on the final concat pass. If not passed, the "
        "config file's `final_concat_timeout` key is used.",
    ),
    silence_timeout: int = typer.Option(
        CONFIG_DEFAULTS["silence_timeout"],
        "--silence-timeout",
        help="Silence detection ceiling in seconds (default 36000 = 10 h). "
        "If not passed, the config file's `silence_timeout` key is used.",
    ),
    stall_kill_timeout: int = typer.Option(
        CONFIG_DEFAULTS["stall_kill_timeout"],
        "--stall-timeout",
        help="No-progress kill timeout in seconds (default 300 = 5 min). "
        "ffmpeg is killed if no progress line arrives within this window. "
        "If not passed, the config file's `stall_kill_timeout` key is used.",
    ),
    waveform_timeout: int = typer.Option(
        CONFIG_DEFAULTS["waveform_timeout"],
        "--waveform-timeout",
        help="Waveform preview decode timeout in seconds (default 300 = 5 min). "
        "If not passed, the config file's `waveform_timeout` key is used.",
    ),
    batch_chunk_size: int = typer.Option(
        CONFIG_DEFAULTS["batch_chunk_size"],
        "--batch-chunk-size",
        help="Number of keep-segments per batch filter invocation (default 40). "
        "Scaled down dynamically for large segment counts. If not passed, "
        "the config file's `batch_chunk_size` key is used.",
    ),
    min_part_bytes: int = typer.Option(
        CONFIG_DEFAULTS["min_part_bytes"],
        "--min-part-bytes",
        help="Minimum bytes for a resumed part to be considered valid "
        "(default 1024). Smaller files are re-encoded. If not passed, the "
        "config file's `min_part_bytes` key is used.",
    ),
    output_fps: str = typer.Option(
        CONFIG_DEFAULTS["output_fps"],
        "--output-fps",
        help="Output FPS policy: 'source' preserves the source cadence; "
        "'24', '25', '30', '50', or '60' force CFR conversion via ffmpeg's "
        "fps filter. If not passed, the config file's `output_fps` key is used.",
    ),
    output_format: str = typer.Option(
        CONFIG_DEFAULTS["output_format"],
        "--output-format",
        help="Output container/codec: 'video' (default — H.264 + AAC MP4), "
        "or an audio-only format: 'mp3' (libmp3lame), 'opus' (libopus), "
        "'aac' (m4a), 'wav' (PCM 16-bit, lossless), 'flac' (lossless). "
        "Audio-only outputs drop the video stream entirely. If not passed, "
        "the config file's `output_format` key is used.",
    ),
) -> None:
    """
    Compress stream recording by removing silence segments.

    Processes:
    1. Download video (if URL provided)
    2. Detect silence segments
    3. Cut and concatenate video
    """
    # Configure root logging ONCE at entry — see P2.9 in the fix plan.
    # Previously ``logging.basicConfig`` ran at import time, which
    # hijacked the root logger of any host application that imported
    # stream2video.cli (tests, GUI embeds, downstream tools). Doing it
    # here keeps the CLI's user-facing logging behaviour (Rich stderr
    # handler + DEBUG-level root) for ``stream2video`` invocations
    # while leaving importers' logging untouched.
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(message)s",
        handlers=[_console_handler],
    )

    # Verify ffmpeg is available
    _check_ffmpeg()

    # Set log level (apply to console handler, not the logger itself, so the
    # file handler still receives DEBUG regardless of the user's choice).
    valid_levels = ("DEBUG", "INFO", "WARNING", "ERROR")
    level = log_level.upper()
    if level not in valid_levels:
        console.print(
            f"[red]Invalid log level:[/red] {log_level!r} (use DEBUG, INFO, WARNING, or ERROR)"
        )
        raise typer.Exit(1)
    _console_handler.setLevel(level)

    # Ensure output directory exists
    output_dir.mkdir(parents=True, exist_ok=True)
    log_file = output_dir / "stream2video.log"

    console.print("\n[bold cyan]stream2video[/bold cyan] - Compress by removing silence")
    console.print(f"Logs saved to: {log_file}\n")

    # ``signal.getsignal`` / ``signal.signal`` can only be called from the
    # interpreter's main thread; a host embedding ``cli.main`` in a worker
    # thread would otherwise crash here with ``ValueError``. When that
    # happens, fall back to ``None`` so the ``finally``-block restore path
    # uses ``SIG_DFL`` (and even that restore will be guarded below).
    try:
        prev_handler: Any = signal.getsignal(signal.SIGINT)
    except (ValueError, OSError) as e:
        logger.warning(f"Could not read current SIGINT handler: {e}")
        prev_handler = None
    _cancel_event, cancel_cb = _make_sigint_cancel()

    fh = None
    try:
        fh = _make_file_handler(log_file)
        logger.addHandler(fh)

        # Load configuration. ``load_config`` strictly validates BOTH numeric
        # ranges (CONFIG_RANGES) AND enum keys (method/encoder/...), so a
        # bad YAML value for any of these cannot sneak through here.
        config = load_config(config_file)

        # Apply the resource preset. The preset bundles tunables
        # (x264_low_memory, memory_limit_mb, batch_chunk_size,
        # low_process_priority) into a named profile; each subsequent
        # ``_resolved_*`` below reads the merged config AND checks
        # ``ParameterSource.COMMANDLINE``, so an explicit --flag wins
        # over the preset's override. The preset itself is read via
        # ParameterSource too — COMMANDLINE --preset wins, otherwise
        # the YAML key ``preset`` (if present) is used, else
        # DEFAULT_PRESET.
        def _resolved_preset(flag_value: str) -> str:
            if ParameterSource is None:
                value = str(config.get("preset", DEFAULT_PRESET))
            else:
                src = ctx.get_parameter_source("preset")
                if src == ParameterSource.COMMANDLINE:
                    value = flag_value
                else:
                    value = str(config.get("preset", DEFAULT_PRESET))
            if value not in PRESET_NAMES:
                console.print(
                    f"[red]Invalid preset:[/red] {value!r} "
                    f"(use {' or '.join(repr(p) for p in PRESET_NAMES)})"
                )
                raise typer.Exit(1)
            return value

        resolved_preset = _resolved_preset(preset)
        config = apply_preset(config, resolved_preset)

        # Resolve each CLI flag against the config. A flag that the user
        # passed explicitly (ParameterSource.COMMANDLINE) wins; one that
        # the user left at its default falls back to the config value
        # (which came from YAML if provided, else CONFIG_DEFAULTS, with
        # preset overrides already applied above).
        def _resolved_str(name: str, flag_value: str, valid: list[str]) -> str:
            if ParameterSource is None:
                value = config[name]
            else:
                src = ctx.get_parameter_source(name)
                if src == ParameterSource.COMMANDLINE:
                    value = flag_value
                else:
                    value = config[name]
            if value not in valid:
                console.print(
                    f"[red]Invalid {name}:[/red] {value!r} "
                    f"(use {' or '.join(repr(v) for v in valid)})"
                )
                raise typer.Exit(1)
            return value

        method = _resolved_str("method", method, VALID_METHODS)
        encoder = _resolved_str("encoder", encoder, VALID_ENCODERS)
        video_quality = _resolved_str("video_quality", video_quality, VALID_QUALITIES)
        download_quality = _resolved_str(
            "download_quality", download_quality, VALID_DOWNLOAD_QUALITIES
        )
        audio_quality = _resolved_str("audio_quality", audio_quality, VALID_QUALITIES)
        software_fallback = _resolved_str(
            "software_fallback", software_fallback, VALID_SOFTWARE_FALLBACKS
        )
        x264_preset = _resolved_str("x264_preset", x264_preset, VALID_X264_PRESETS)
        output_fps = _resolved_str("output_fps", output_fps, VALID_OUTPUT_FPS)
        output_format = _resolved_str("output_format", output_format, VALID_OUTPUT_FORMATS)

        # Output filename extension follows the chosen output_format.
        # ``video`` keeps the historical ``_compressed.mp4`` name; the
        # audio-only formats use the codec's native extension (mp3, opus,
        # m4a, wav, flac). The suffix mapping lives in
        # ``OUTPUT_FORMAT_SPECS`` so the extension and the codec stay in
        # sync — a future format added there automatically gets the right
        # filename here.
        from stream2video.config import OUTPUT_FORMAT_SPECS

        if output_format == "video":
            output_suffix = "compressed.mp4"
        else:
            spec = OUTPUT_FORMAT_SPECS.get(output_format)
            if spec is None:
                # Unreachable: _resolved_str already validated against
                # VALID_OUTPUT_FORMATS. Defensive guard so a future
                # format added to VALID_OUTPUT_FORMATS but missing from
                # OUTPUT_FORMAT_SPECS produces a clear error rather than
                # a confusing ``None`` lookup.
                console.print(
                    f"[red]Internal error:[/red] no spec for output_format {output_format!r}"
                )
                raise typer.Exit(1)
            output_suffix = f"compressed.{spec['ext']}"

        # ``encoder_threads`` accepts ``"auto"`` or a positive int (see
        # ``config.coerce_typed_value``). The CLI flag arrives as a
        # string; the config-file value arrives as int (already validated).
        # Resolve: CLI override parses to int when possible, else "auto".
        def _resolved_encoder_threads(
            flag_value: str,
        ) -> str | int:
            if ParameterSource is None:
                return config.get("encoder_threads", "auto")
            src = ctx.get_parameter_source("encoder_threads")
            if src == ParameterSource.COMMANDLINE:
                v = flag_value.strip()
                if v == "auto":
                    return "auto"
                try:
                    n = int(v)
                except (TypeError, ValueError) as e:
                    console.print(
                        f"[red]Invalid encoder-threads:[/red] {flag_value!r} "
                        f"(use 'auto' or a positive integer)"
                    )
                    raise typer.Exit(1) from e
                if n <= 0:
                    console.print(
                        f"[red]Invalid encoder-threads:[/red] {n} (must be > 0 or 'auto')"
                    )
                    raise typer.Exit(1)
                return n
            # Fall back to config value (already int-or-"auto" validated).
            return config.get("encoder_threads", "auto")

        resolved_encoder_threads: str | int = _resolved_encoder_threads(encoder_threads)

        def _resolved_memory_limit_mb(flag_value: str) -> str | int:
            if ParameterSource is None:
                return config.get("memory_limit_mb", "auto")
            src = ctx.get_parameter_source("memory_limit_mb")
            if src == ParameterSource.COMMANDLINE:
                v = flag_value.strip()
                if v == "auto":
                    return "auto"
                try:
                    n = int(v)
                except (TypeError, ValueError) as e:
                    console.print(
                        f"[red]Invalid memory-limit-mb:[/red] {flag_value!r} "
                        f"(use 'auto' or a non-negative integer)"
                    )
                    raise typer.Exit(1) from e
                if n < 0:
                    console.print(
                        f"[red]Invalid memory-limit-mb:[/red] {n} (must be >= 0 or 'auto')"
                    )
                    raise typer.Exit(1)
                return n
            return config.get("memory_limit_mb", "auto")

        resolved_memory_limit_mb: str | int = _resolved_memory_limit_mb(memory_limit_mb)

        def _resolved_memory_reserve_mb(flag_value: int) -> int:
            if ParameterSource is None:
                return int(config.get("memory_reserve_mb", 2048))
            src = ctx.get_parameter_source("memory_reserve_mb")
            if src == ParameterSource.COMMANDLINE:
                if flag_value < 0:
                    console.print(
                        f"[red]Invalid memory-reserve-mb:[/red] {flag_value} (must be >= 0)"
                    )
                    raise typer.Exit(1)
                return flag_value
            return int(config.get("memory_reserve_mb", 2048))

        resolved_memory_reserve_mb: int = _resolved_memory_reserve_mb(memory_reserve_mb)

        def _resolved_x264_low_memory(flag_value: bool) -> bool:
            if ParameterSource is None:
                return bool(config.get("x264_low_memory", False))
            src = ctx.get_parameter_source("x264_low_memory")
            if src == ParameterSource.COMMANDLINE:
                return flag_value
            return bool(config.get("x264_low_memory", False))

        resolved_x264_low_memory: bool = _resolved_x264_low_memory(x264_low_memory)

        def _resolved_use_crf(flag_value: bool) -> bool:
            if ParameterSource is None:
                return bool(config.get("use_crf", False))
            src = ctx.get_parameter_source("use_crf")
            if src == ParameterSource.COMMANDLINE:
                return flag_value
            return bool(config.get("use_crf", False))

        resolved_use_crf: bool = _resolved_use_crf(use_crf)

        def _resolved_gapless_concat(flag_value: bool) -> bool:
            if ParameterSource is None:
                return bool(config.get("gapless_concat", False))
            src = ctx.get_parameter_source("gapless_concat")
            if src == ParameterSource.COMMANDLINE:
                return flag_value
            return bool(config.get("gapless_concat", False))

        resolved_gapless_concat: bool = _resolved_gapless_concat(gapless_concat)

        def _resolved_low_process_priority(flag_value: bool) -> bool:
            if ParameterSource is None:
                return bool(config.get("low_process_priority", False))
            src = ctx.get_parameter_source("low_process_priority")
            if src == ParameterSource.COMMANDLINE:
                return flag_value
            return bool(config.get("low_process_priority", False))

        resolved_low_process_priority: bool = _resolved_low_process_priority(low_process_priority)

        def _resolved_bool(name: str, flag_value: bool | None) -> bool:
            if ParameterSource is None:
                return bool(config.get(name, False))
            src = ctx.get_parameter_source(name)
            if src == ParameterSource.COMMANDLINE:
                return bool(flag_value)
            # Config already type-checked in load_config; bool is enforced.
            # `config.get(name, False)` so a future CONFIG_DEFAULTS edit
            # that drops a bool key doesn't raise KeyError here.
            return bool(config.get(name, False))

        force = _resolved_bool("force", force)
        delete_after = _resolved_bool("delete_after", delete_after)
        per_video_dir_resolved = _resolved_bool("per_video_dir", per_video_dir)
        # Push the resolved value back into config so downstream code that
        # reads config["per_video_dir"] (e.g. paths.apply_per_video_dir)
        # sees the same value as the CLI path.
        config["per_video_dir"] = per_video_dir_resolved

        def _resolved_int(name: str, flag_value: int) -> int:
            if ParameterSource is None:
                return int(config.get(name, CONFIG_DEFAULTS.get(name, flag_value)))
            src = ctx.get_parameter_source(name)
            if src == ParameterSource.COMMANDLINE:
                return int(flag_value)
            # Config already validated in load_config; preset overrides
            # have been applied above so ``config.get(name)`` reflects
            # the preset-transformed value.
            return int(config.get(name, CONFIG_DEFAULTS.get(name, flag_value)))

        # batch_chunk_size is a preset-tunable, so honour the preset
        # override unless the user passed --batch-chunk-size explicitly.
        batch_chunk_size = _resolved_int("batch_chunk_size", batch_chunk_size)
        # rlimit_as_mb is also a preset-tunable (well, CLI-only, but
        # the same fallback semantics apply).
        resolved_rlimit_as_mb: int = _resolved_int("rlimit_as_mb", rlimit_as_mb)

        # P1: pipeline timeouts + network tunables. These were the
        # 9 yaml keys silently ignored before — the CLI used to rely on
        # typer defaults populated from CONFIG_DEFAULTS at import time,
        # so a user ``silence_timeout: 60`` in config.yaml had no effect.
        resolved_download_timeout: int = _resolved_int("download_timeout", download_timeout)
        resolved_connect_timeout: int = _resolved_int("connect_timeout", connect_timeout)
        resolved_no_progress_timeout: int = _resolved_int("no_progress_timeout", no_progress_timeout)
        resolved_silence_timeout: int = _resolved_int("silence_timeout", silence_timeout)
        resolved_segment_encode_timeout: int = _resolved_int(
            "segment_encode_timeout", segment_encode_timeout
        )
        resolved_final_concat_timeout: int = _resolved_int(
            "final_concat_timeout", final_concat_timeout
        )
        resolved_stall_kill_timeout: int = _resolved_int("stall_kill_timeout", stall_kill_timeout)
        resolved_min_part_bytes: int = _resolved_int("min_part_bytes", min_part_bytes)

        # P1: proxy — honour YAML + CLI. ``--proxy URL`` (COMMANDLINE)
        # wires the value AND enables it (the user's intent is clear).
        # YAML's ``proxy: url`` is gated by ``proxy_active: true`` so a
        # config file doesn't silently change networking (matches the
        # GUI's checkbox contract — pipeline_worker.py line ~197).
        _proxy_src = (
            ctx.get_parameter_source("proxy") if ParameterSource is not None else None
        )
        if _proxy_src == getattr(ParameterSource, "COMMANDLINE", None):
            resolved_proxy = proxy
        else:
            resolved_proxy = (
                config.get("proxy", "") if config.get("proxy_active", False) else ""
            )
        if isinstance(resolved_proxy, bool) or (
            not isinstance(resolved_proxy, str) and resolved_proxy is not None
        ):
            console.print(
                f"[red]Invalid proxy:[/red] {resolved_proxy!r} (must be a string URL or empty)"
            )
            raise typer.Exit(1)

        # Defensive re-validation through the shared pipeline validator.
        # ``_resolved_*`` + ``load_config`` already rejected every
        # CLI-visible bad key; this second pass runs the final resolved
        # values through the same ``validate_pipeline_config`` the GUI's
        # worker uses, so a value that bypasses the per-key checks here
        # (future config key, regression in ``_resolved_*``) still fails
        # early with a clear message instead of mid-pipeline.
        _pcfg = _PipelineConfig(
            input_raw=input_video,
            output_dir=output_dir,
            method=method,
            encoder=encoder,
            video_quality=video_quality,
            audio_quality=audio_quality,
            download_quality=download_quality,
            software_fallback=software_fallback,
            x264_preset=x264_preset,
            encoder_threads=resolved_encoder_threads,
            output_fps=output_fps,
            output_format=output_format,
            force=force,
            delete_after=delete_after,
            per_video_dir=per_video_dir_resolved,
            threshold=float(config["threshold"]),
            min_silence=float(config["min_silence"]),
            margin=float(config["margin"]),
            memory_limit_mb=resolved_memory_limit_mb,
            memory_reserve_mb=resolved_memory_reserve_mb,
            x264_low_memory=resolved_x264_low_memory,
            use_crf=resolved_use_crf,
            gapless_concat=resolved_gapless_concat,
            low_process_priority=resolved_low_process_priority,
            rlimit_as_mb=resolved_rlimit_as_mb,
            download_timeout=resolved_download_timeout,
            connect_timeout=resolved_connect_timeout,
            no_progress_timeout=resolved_no_progress_timeout,
            proxy=resolved_proxy,
            segment_encode_timeout=resolved_segment_encode_timeout,
            final_concat_timeout=resolved_final_concat_timeout,
            silence_timeout=resolved_silence_timeout,
            stall_kill_timeout=resolved_stall_kill_timeout,
            min_part_bytes=resolved_min_part_bytes,
        )
        _cfg_errors = _validate_pipeline_config(_pcfg)
        if _cfg_errors:
            for _err in _cfg_errors:
                console.print(f"[red]Invalid configuration:[/red] {_err}")
            raise typer.Exit(1)

        # P1: reify ``software_fallback="ask"``. The callback typer
        # confirms with (``--force``-style defaults can't short-circuit
        # because the sigint-cancel has already fired for Ctrl+C-era
        # reproducibility) closes the gap between the CLI and the
        # GUI's consent dialog.
        def _make_fallback_consent() -> "Callable[[], bool] | None":
            if software_fallback != "ask":
                return None

            def _consent() -> bool:
                try:
                    import typer as _typer

                    return _typer.confirm(
                        f"Selected encoder {encoder!r} is unavailable or failed. "
                        "Fall back to libx264 (CPU-heavy, ~3-5x slower)?",
                        default=False,
                    )
                except Exception:
                    # Non-interactive tty / EOF / headless: refuse the
                    # fallback (``ask`` must not silently switch).
                    return False

            return _consent

        progress_columns = [
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
        ]

        with Progress(*progress_columns, console=console) as progress:
            # Step 1: Download video (indeterminate: yt-dlp does not report progress)
            task1 = progress.add_task("[cyan]Downloading video...", total=None)

            def _download_progress_cb(p: DownloadProgress) -> None:
                """Update the Rich task with yt-dlp progress.

                Called from the stdout drain thread — Rich's Progress.update
                is thread-safe (it locks internally). Maps the downloaded /
                total bytes to the task, and refreshes the description with
                percent + speed + ETA. Falls back to the indeterminate bar
                (no total) when yt-dlp reports NA for the total size.
                """
                if p.total_bytes:
                    progress.update(
                        task1,
                        total=p.total_bytes,
                        completed=p.downloaded_bytes or 0.0,
                    )
                else:
                    # Unknown total — show indeterminate bar (no total) so
                    # it reads as "spinning" rather than as "0%". We still
                    # surface the bytes received below so the description
                    # advances meaningfully.
                    progress.update(task1, total=None, completed=0.0)

                # P2.7: delegate the description formatting to the shared
                # helper so the CLI and GUI stay in sync. Previously the
                # CLI rolled its own percent/speed/ETA string here; when
                # the GUI's format diverged (different separator, different
                # rounding) users saw inconsistent output between the two
                # entry points. ``build_download_status`` is pure and
                # unit-tested in tests/test_gui_helpers.py.
                description = "[cyan]" + build_download_status(
                    downloaded_bytes=p.downloaded_bytes,
                    total_bytes=p.total_bytes,
                    speed=p.speed,
                    eta=p.eta,
                    pct=(100.0 * (p.downloaded_bytes or 0.0) / p.total_bytes)
                    if p.total_bytes
                    else None,
                )
                progress.update(task1, description=description)

            try:
                logger.info(f"Processing: {input_video}")
                download_result = download(
                    input_video,
                    output_dir,
                    cancel_callback=cancel_cb,
                    quality=download_quality,
                    progress_callback=_download_progress_cb,
                    download_timeout=resolved_download_timeout,
                    connect_timeout=resolved_connect_timeout,
                    no_progress_timeout=resolved_no_progress_timeout,
                    proxy=resolved_proxy,
                )
                video_path = download_result.path
                if download_result.is_downloaded:
                    progress.update(
                        task1, total=1, completed=1, description="[green]+[/green] Video downloaded"
                    )
                else:
                    progress.update(
                        task1,
                        total=1,
                        completed=1,
                        description="[green]+[/green] Local file (download skipped)",
                    )

            except DownloadCancelledError:
                console.print("[yellow]Download cancelled.[/yellow]")
                raise typer.Exit(130) from None
            except URLValidationError as e:
                # Caught before the generic DownloadError so the user gets
                # a clear "this isn't a URL or local file" message.
                console.print(f"[red]Invalid input:[/red] {e}")
                console.print("  Expected an http(s):// URL or an existing local file path.")
                raise typer.Exit(2) from None
            except VideoNotAvailableError as e:
                console.print(f"[red]Video unavailable:[/red] {e}")
                console.print("  The video may be private, deleted, or region-restricted.")
                raise typer.Exit(1) from None
            except DownloadTimeoutError as e:
                console.print(f"[red]Download timed out:[/red] {e}")
                console.print("  Try again later or check your connection.")
                raise typer.Exit(1) from None
            except DiskSpaceError as e:
                console.print(f"[red]Disk space error:[/red] {e}")
                console.print("  Free up disk space and try again.")
                raise typer.Exit(1) from None
            except PermissionDeniedError as e:
                console.print(f"[red]Permission denied:[/red] {e}")
                console.print("  Check file permissions and try again.")
                raise typer.Exit(1) from None
            except DownloadError as e:
                console.print(f"[red]Download failed:[/red] {e}")
                logger.exception("Download error")
                raise typer.Exit(1) from None

            # Step 1.5: Apply per-video project directory. The function
            # honours the per_video_dir flag itself, so no outer gate.
            new_output, video_path = apply_per_video_dir(
                output_dir,
                video_path,
                download_result.is_downloaded,
                per_video_dir=per_video_dir_resolved,
            )
            if new_output != output_dir:
                if download_result.is_downloaded:
                    logger.info(f"Moved source into project dir: {video_path}")
                if fh is not None:
                    # Safe swap: detach the old handler, then attach the
                    # new one. If addHandler fails after removeHandler +
                    # close, `fh` is rolled back to the still-attached old
                    # handler reference (already closed, but the outer
                    # finally's removeHandler/close is idempotent) — and
                    # we never leave a dangling closed handler attached
                    # to the logger (which would double-log on the next
                    # run).
                    old_fh = fh
                    new_log = new_output / "stream2video.log"
                    # Close + detach the old handler BEFORE moving the
                    # log file: on Windows the open FileHandler holds a
                    # lock that blocks shutil.move (WinError 32) until
                    # the handle is released.
                    logger.removeHandler(old_fh)
                    old_fh.close()
                    if new_log.exists():
                        new_log.unlink()
                    if log_file.exists():
                        shutil.move(str(log_file), str(new_log))
                    new_fh = _make_file_handler(new_log)
                    try:
                        logger.addHandler(new_fh)
                    except Exception:
                        # addHandler raised: new_fh is detached, old_fh
                        # is already removed+closed. Roll back `fh` to
                        # old_fh so the outer finally's removeHandler()
                        # is a no-op and close() is a harmless second
                        # close on an already-closed handler. Re-raise.
                        new_fh.close()
                        fh = old_fh
                        raise
                    fh = new_fh
                output_dir = new_output
                console.print(f"Project directory: [cyan]{output_dir}[/cyan]")

            # Step 2: Detect silence (with cache support)
            task2 = progress.add_task("[cyan]Detecting silence segments...", total=100)

            # Pre-flight memory-reserve check, same as the GUI controller.
            # Refuse to start a heavy phase when available RAM is already
            # below the configured reserve.
            if not check_memory_reserve(resolved_memory_reserve_mb, "silence detection"):
                console.print(
                    f"[red]Not enough free RAM:[/red] below reserve "
                    f"{resolved_memory_reserve_mb} MB — refusing to start silence detection."
                )
                raise typer.Exit(1)

            try:
                silence_segments = None
                if not force:
                    silence_segments = load_silence_cache(video_path, output_dir, config)

                if silence_segments is None:

                    def silence_progress(f: float) -> None:
                        progress.update(task2, completed=min(f * 100, 100))

                    # Resume cache: the CLI now wires the same resume
                    # path the GUI uses, so a Ctrl+C mid-detection can
                    # pick up from the last throttled checkpoint instead
                    # of restarting from t=0. Without this the CLI
                    # silently discarded any resume state the GUI had
                    # written for the same source/output_dir pair (see
                    # P1.8 in the fix plan).
                    resume_cache_path = output_dir / f"{video_path.stem}_silence_cache.json.resume"
                    # ``--force`` invalidates the resume cache the same
                    # way it invalidates the final cache, so a forced
                    # re-detection doesn't pick up segments from a
                    # prior run with different (threshold/margin) params.
                    if force and resume_cache_path.exists():
                        try:
                            resume_cache_path.unlink()
                        except OSError as e:
                            logger.warning(f"Could not remove stale resume cache: {e}")

                    silence_segments = detect_silence(
                        video_path,
                        threshold=config["threshold"],
                        min_silence=config["min_silence"],
                        margin=config["margin"],
                        output_dir=output_dir,
                        progress_callback=silence_progress,
                        cancel_callback=cancel_cb,
                        resume_cache_path=resume_cache_path,
                        timeout=resolved_silence_timeout,
                    )
                    save_silence_cache(video_path, silence_segments, output_dir, config)
                    # Detection succeeded → the final cache is the
                    # source of truth, the resume file can be removed.
                    try:
                        resume_cache_path.unlink(missing_ok=True)
                    except OSError as e:
                        logger.warning(f"Could not remove stale resume cache: {e}")

                # By here `silence_segments` is non-None — either loaded
                # from cache or freshly detected. Narrow the type so the
                # length read below is unambiguous to the reader / mypy.
                assert silence_segments is not None

                progress.update(
                    task2,
                    completed=100,
                    description=f"[green]+[/green] Found {len(silence_segments)} silence segments",
                )

            except SilenceCancelledError:
                console.print("[yellow]Silence detection cancelled.[/yellow]")
                raise typer.Exit(130) from None
            except SilenceDetectionError as e:
                console.print(f"[red]Silence detection failed:[/red] {e}")
                logger.exception("Silence detection error")
                raise typer.Exit(1) from None

            # Step 3: Cut + 4: Concatenate — atomic phases so the
            # CLI's bar/label shows which one stalled (e.g. gapless tree
            # L0 G0 vs segment encodes). The 0.9/0.1 split mirrors the
            # 0..0.9 cutting / 0.9..1.0 concatenating convention in
            # pipeline_controller + concat/segment.py.
            task_cut = progress.add_task("[cyan]Cutting segments...", total=100)
            task_concat: TaskID | None = None

            def _on_phase_cli(name: str, f: float) -> None:
                nonlocal task_concat
                if name == "cutting":
                    progress.update(task_cut, completed=min(f * 100, 100))
                else:
                    if task_concat is None:
                        progress.update(task_cut, completed=100, description="[green]+[/green] Cutting done")
                        task_concat = progress.add_task("[cyan]Concatenating...", total=100)
                    progress.update(task_concat, completed=min(f * 100, 100))

            def update_progress(fraction: float) -> None:
                # Legacy 0..1 path (cut 0..0.9, concat 0.9..1.0)
                nonlocal task_concat
                if fraction < 0.9:
                    progress.update(task_cut, completed=min(fraction / 0.9 * 100, 100))
                else:
                    if task_concat is None:
                        progress.update(task_cut, completed=100, description="[green]+[/green] Cutting done")
                        task_concat = progress.add_task("[cyan]Concatenating...", total=100)
                    progress.update(task_concat, completed=min((fraction - 0.9) / 0.1 * 100, 100))

            try:
                if not check_memory_reserve(resolved_memory_reserve_mb, "concat phase"):
                    console.print(
                        f"[red]Not enough free RAM:[/red] below reserve "
                        f"{resolved_memory_reserve_mb} MB — refusing to start concat."
                    )
                    raise typer.Exit(1)
                output_video = output_dir / f"{video_path.stem}_{output_suffix}"

                cut_and_concat(
                    video_path,
                    silence_segments,
                    output_video,
                    progress_callback=update_progress,
                    on_phase=_on_phase_cli,
                    method=method,
                    encoder=encoder,
                    video_quality=video_quality,
                    audio_quality=audio_quality,
                    cancel_callback=cancel_cb,
                    software_fallback=software_fallback,
                    fallback_consent=_make_fallback_consent(),
                    x264_preset=x264_preset,
                    encoder_threads=resolved_encoder_threads,
                    output_fps=output_fps,
                    output_format=output_format,
                    memory_limit_mb=resolved_memory_limit_mb,
                    memory_reserve_mb=resolved_memory_reserve_mb,
                    x264_low_memory=resolved_x264_low_memory,
                    use_crf=resolved_use_crf,
                    gapless_concat=resolved_gapless_concat,
                    low_process_priority=resolved_low_process_priority,
                    rlimit_as_mb=resolved_rlimit_as_mb,
                    segment_encode_timeout=resolved_segment_encode_timeout,
                    final_concat_timeout=resolved_final_concat_timeout,
                    stall_kill_timeout=resolved_stall_kill_timeout,
                    stall_warning_timeout=config.get("stall_warning_timeout", 120),
                    batch_chunk_size=batch_chunk_size,
                    min_part_bytes=resolved_min_part_bytes,
                )

                # Mark whichever task is live as done
                if task_concat is not None:
                    progress.update(task_concat, completed=100, description="[green]+[/green] Concatenating done")
                else:
                    progress.update(task_cut, completed=100, description="[green]+[/green] Cutting done")
                    # No concat phase (e.g. single segment) — still show it
                    tc = progress.add_task("[cyan]Concatenating...", total=100)
                    progress.update(tc, completed=100, description="[green]+[/green] Concatenating done")

            except CancelledError:
                # ``CancelledError`` is a subclass of ``ConcatError`` so
                # it MUST be caught first — otherwise the generic
                # ``ConcatError`` handler would run, re-check the cancel
                # event, and emit a misleading "concatenation failed"
                # log line for what was actually a clean cancel. This
                # mirrors the silence-detection handler above which
                # catches ``SilenceCancelledError`` (subclass of
                # ``SilenceDetectionError``) before the generic handler.
                console.print("[yellow]Concatenation cancelled.[/yellow]")
                raise typer.Exit(130) from None
            except ConcatError as e:
                console.print(f"[red]Concatenation failed:[/red] {e}")
                logger.exception("Concatenation error")
                raise typer.Exit(1) from None

        # Summary
        console.print("\n[bold green]+ Compression complete![/bold green]")
        console.print(f"Output: [cyan]{output_video}[/cyan]")

        if download_result.is_downloaded:
            if delete_after:
                try:
                    video_path.unlink()
                    console.print(f"Deleted source: [dim]{video_path}[/dim]")
                    logger.info(f"Deleted source: {video_path}")
                except OSError as e:
                    console.print(f"[yellow]Warning:[/yellow] Could not delete source: {e}")
                    logger.warning(f"Could not delete source: {e}")
            else:
                logger.info(f"Temporary download file: {video_path}")

        logger.info(f"Successfully compressed video to {output_video}")

    except typer.Exit:
        raise

    except Exception as e:
        console.print(f"[red]Unexpected error:[/red] {e}")
        logger.exception("Unexpected error")
        # Preserve the original exception so a developer running with
        # `RICH_TRACEBACK=1` (or a debugger) sees the actual cause; the
        # user-facing message above is the only thing they see by default.
        raise typer.Exit(1) from e

    finally:
        # `signal.getsignal` can return `None` on some interpreters when no
        # handler has been installed; restoring `None` would raise TypeError
        # (which the prior `except (OSError, ValueError)` did not catch).
        # Treat `None` as "no explicit handler installed" → restore SIG_DFL
        # for a clean state. SIG_IGN / SIG_DFL are restored as-is.
        restore_to: Any = signal.SIG_DFL if prev_handler is None else prev_handler
        if restore_to not in (signal.SIG_IGN, signal.SIG_DFL):
            try:
                signal.signal(signal.SIGINT, restore_to)
            except (OSError, ValueError, TypeError) as e:
                # signal.signal raises ValueError when called from a
                # non-main thread; some platforms raise OSError. Log
                # rather than silently swallow so a host that runs the
                # CLI in a worker thread can diagnose why SIGINT wasn't
                # restored.
                logger.warning(f"Could not restore SIGINT handler: {e}")
        if fh is not None:
            logger.removeHandler(fh)
            fh.close()


if __name__ == "__main__":
    app()
