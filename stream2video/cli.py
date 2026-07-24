"""CLI entry point using Typer."""

import logging
import shutil
import signal
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer
import yaml
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import (
    BarColumn,
    Progress,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

from stream2video.concat import CancelledError, ConcatError, cut_and_concat
from stream2video.config import (
    CONFIG_DEFAULTS,
    CONFIG_RANGES,
    VALID_DOWNLOAD_QUALITIES,
    VALID_ENCODERS,
    VALID_METHODS,
    VALID_OUTPUT_FORMATS,
    VALID_QUALITIES,
    VALID_SOFTWARE_FALLBACKS,
    VALID_X264_PRESETS,
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
from stream2video.paths import apply_per_video_dir
from stream2video.silence import (
    SilenceCancelledError,
    SilenceDetectionError,
    detect_silence,
    load_silence_cache,
    save_silence_cache,
)

# ``ParameterSource`` tells us whether a CLI flag came from the command
# line or a default. Its import path has moved across typer/click
# releases. Use a defensive try/except chain so the module keeps
# importing on all supported versions.
ParameterSource: Any = None
try:
    from click.core import ParameterSource as _PS  # click >= 8.0

    ParameterSource = _PS
except ImportError:  # pragma: no cover - legacy fallback
    try:
        from typer._click.core import ParameterSource as _PS2

        ParameterSource = _PS2
    except ImportError:  # pragma: no cover - very old typer
        pass

# Logging setup is deferred to ``main()`` so importing ``stream2video.cli``
# (e.g. from tests, or from a host application embedding the library)
# doesn't reconfigure the root logger. The historical ``basicConfig``
# at import time would override the host's own logging config, which is
# especially noisy for GUI embeds and pytest's caplog. See P2.9 in the
# fix plan.
_console_handler = RichHandler(rich_tracebacks=True)
logger = logging.getLogger("stream2video")

console = Console()
app = typer.Typer(help="Compress stream recordings by removing silence")


def _make_sigint_cancel() -> tuple[threading.Event, Callable[[], bool]]:
    """Wire SIGINT to a cancel event so Ctrl+C aborts running ffmpeg/yt-dlp.

    Returns (event, callback). The callback returns True once SIGINT has been
    received. The event is set by the signal handler in the main thread, but
    signal handlers in Python can only safely set an event/flag, not raise.
    """
    event = threading.Event()

    def _handler(signum: Any, frame: Any) -> None:
        event.set()

    signal.signal(signal.SIGINT, _handler)

    def _cb() -> bool:
        return event.is_set()

    return event, _cb


def _make_file_handler(path: Path) -> logging.FileHandler:
    """Create the CLI's per-run file handler with the canonical format.

    DEBUG-level so the file always gets the full trace; the user-facing
    console level is controlled separately by ``_console_handler.setLevel``.
    Format: ``%(asctime)s - %(name)s - %(levelname)s - %(message)s`` —
    matches what stream2video.log has always written so existing log-
    parsing scripts keep working across upgrades.
    """
    # Use UTF-8 explicitly so the log file is consistent across platforms
    # (Windows OEM codepages are often not UTF-8 and would raise
    # UnicodeEncodeError on non-ASCII paths/labels mid-run, swallowed
    # by logging.handleError and lost). Matches the cache writers in
    # silence.py / config.py.
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    return fh


def _check_ffmpeg() -> None:
    """Warn if ffmpeg or ffprobe is missing."""
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            console.print(f"[red]Error:[/red] {tool} not found in PATH")
            console.print("  Install: [cyan]winget install Gyan.FFmpeg[/cyan]")
            console.print("  Or run:  [cyan]setup.ps1[/cyan] (Windows)")
            raise typer.Exit(1)


def load_config(config_file: Path | None) -> dict:
    """Load and validate configuration file.

    Validates BOTH numeric ranges (``CONFIG_RANGES``) AND enum keys
    (``method``, ``encoder``, ``video_quality``, ``download_quality``,
    ``theme``) against their ``VALID_*`` lists. This is the single
    chokepoint for config-file validation — the CLI flag-path goes
    through its own ``_resolved_*`` check downstream, so an invalid
    YAML value is rejected here regardless of whether the matching
    CLI flag was passed.
    """
    config = CONFIG_DEFAULTS.copy()
    # Raw YAML dict (before merging into ``config``). Kept so bool-key
    # validation below can distinguish "user wrote 1 in YAML" (int,
    # rejected) from "default value absent" (skip). ``file_config`` is
    # only assigned inside the try/except when the file loads cleanly.
    file_config: dict = {}

    if config_file:
        if not config_file.exists():
            console.print(f"[yellow]Warning:[/yellow] Config file not found: {config_file}")
        else:
            try:
                with open(config_file) as f:
                    loaded = yaml.safe_load(f) or {}

                if not isinstance(loaded, dict):
                    raise ValueError("Config file must contain a dictionary")

                file_config = loaded
                config.update(file_config)

                logger.info(f"Loaded config from {config_file}")

            except yaml.YAMLError as e:
                console.print(f"[red]Error parsing config file:[/red] {e}")
                raise typer.Exit(1) from None

            except Exception as e:
                console.print(f"[red]Error loading config file:[/red] {e}")
                raise typer.Exit(1) from None

    # Validate numeric ranges.
    for key, (min_val, max_val) in CONFIG_RANGES.items():
        if key in config:
            try:
                value = float(config[key])

                if not min_val <= value <= max_val:
                    console.print(
                        f"[red]Invalid {key}:[/red] {value} not in range [{min_val}, {max_val}]"
                    )
                    raise typer.Exit(1)

                config[key] = value

            except (ValueError, TypeError):
                console.print(f"[red]Invalid {key}:[/red] {config[key]} is not a number")
                raise typer.Exit(1) from None

    # Validate bool keys. YAML booleans (``force: false``) parse to Python
    # bool, but quoted strings (``force: "false"``) parse to the string
    # ``"false"`` which is truthy under ``bool(...)`` — so ``_resolved_bool``
    # later in the run would read it as ``True`` even though the user wrote
    # ``false``. Same hazard for ``0``/``1`` ints: PyYAML keeps them as
    # integers, not bools. Reject any non-bool value the user explicitly
    # wrote in the YAML so downstream ``bool(value)`` matches intent. Keys
    # the user didn't write keep their bool default from CONFIG_DEFAULTS.
    bool_keys = ("force", "delete_after", "per_video_dir")
    for key in bool_keys:
        if key in file_config:
            bool_val = file_config[key]
            if not isinstance(bool_val, bool):
                console.print(f"[red]Invalid {key}:[/red] {bool_val!r} must be true or false")
                raise typer.Exit(1)

    # Validate enum keys against their VALID_* lists. A bad value in
    # either the YAML or CONFIG_DEFAULTS is rejected here so downstream
    # code can assume the value is one of the allowed tokens. ``theme`` is
    # GUI-only — the CLI never reads or applies it — so it's intentionally
    # excluded from the enum validation here (a bad theme in a YAML config
    # that the CLI loads shouldn't abort the run).
    enum_specs = [
        ("method", VALID_METHODS),
        ("encoder", VALID_ENCODERS),
        ("video_quality", VALID_QUALITIES),
        ("download_quality", VALID_DOWNLOAD_QUALITIES),
        ("software_fallback", VALID_SOFTWARE_FALLBACKS),
        ("x264_preset", VALID_X264_PRESETS),
        ("output_format", VALID_OUTPUT_FORMATS),
    ]
    for key, valid in enum_specs:
        enum_val: Any = config.get(key)
        if enum_val is None:
            continue
        if enum_val not in valid:
            console.print(
                f"[red]Invalid {key}:[/red] {enum_val!r} "
                f"(use {' or '.join(repr(x) for x in valid)})"
            )
            raise typer.Exit(1)

    logger.debug(f"Final config: {config}")
    return config


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
        help="Encode quality preset: 'high' (10000k / CRF 18), 'medium' (7000k / CRF 23, default), "
        "or 'low' (3500k / CRF 28). If not passed, the config file's `video_quality` key is used.",
    ),
    audio_quality: str = typer.Option(
        CONFIG_DEFAULTS["audio_quality"],
        "--audio-quality",
        "-aq",
        help="Audio (AAC) bitrate preset: 'high' (256k), 'medium' (192k, default), "
        "or 'low' (128k). If not passed, the config file's `audio_quality` "
        "key is used.",
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
    memory_limit_mb: str = typer.Option(
        str(CONFIG_DEFAULTS["memory_limit_mb"]),
        "--memory-limit-mb",
        help="RAM budget for the encode pipeline: 'auto' (60%% of total RAM, "
        "default) or a positive MB value. 0 disables the budget check (only "
        "the OS reserve remains). If not passed, the config file's "
        "`memory_limit_mb` key is used.",
    ),
    memory_reserve_mb: int = typer.Option(
        CONFIG_DEFAULTS["memory_reserve_mb"],
        "--memory-reserve-mb",
        help="Hard floor of available RAM in MB that the pipeline never violates. "
        "Default 2048 (2 GB). Raise on memory-constrained laptops. If not "
        "passed, the config file's `memory_reserve_mb` key is used.",
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

    prev_handler = signal.getsignal(signal.SIGINT)
    _cancel_event, cancel_cb = _make_sigint_cancel()

    fh = None
    try:
        fh = _make_file_handler(log_file)
        logger.addHandler(fh)

        # Load configuration. ``load_config`` strictly validates BOTH numeric
        # ranges (CONFIG_RANGES) AND enum keys (method/encoder/...), so a
        # bad YAML value for any of these cannot sneak through here.
        config = load_config(config_file)

        # Resolve each CLI flag against the config. A flag that the user
        # passed explicitly (ParameterSource.COMMANDLINE) wins; one that
        # the user left at its default falls back to the config value
        # (which came from YAML if provided, else CONFIG_DEFAULTS).
        def _resolved_str(name: str, flag_value: str, valid: list[str]) -> str:
            src = ctx.get_parameter_source(name)
            value = flag_value if src == ParameterSource.COMMANDLINE else config[name]
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
            src = ctx.get_parameter_source("x264_low_memory")
            if src == ParameterSource.COMMANDLINE:
                return flag_value
            return bool(config.get("x264_low_memory", False))

        resolved_x264_low_memory: bool = _resolved_x264_low_memory(x264_low_memory)

        def _resolved_bool(name: str, flag_value: bool | None) -> bool:
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
                    download_timeout=download_timeout,
                    connect_timeout=connect_timeout,
                    no_progress_timeout=no_progress_timeout,
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
                        timeout=silence_timeout,
                    )
                    save_silence_cache(video_path, silence_segments, output_dir, config)
                    # Detection succeeded → the final cache is the
                    # source of truth, the resume file can be removed.
                    resume_cache_path.unlink(missing_ok=True)

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

            # Step 3: Cut and concatenate (with progress bar)
            task3 = progress.add_task(
                "[cyan]Cutting and concatenating video...",
                total=100,
            )

            def update_progress(fraction: float) -> None:
                progress.update(task3, completed=min(fraction * 100, 100))

            try:
                output_video = output_dir / f"{video_path.stem}_{output_suffix}"

                cut_and_concat(
                    video_path,
                    silence_segments,
                    output_video,
                    progress_callback=update_progress,
                    method=method,
                    encoder=encoder,
                    video_quality=video_quality,
                    audio_quality=audio_quality,
                    cancel_callback=cancel_cb,
                    software_fallback=software_fallback,
                    x264_preset=x264_preset,
                    encoder_threads=resolved_encoder_threads,
                    output_format=output_format,
                    memory_limit_mb=resolved_memory_limit_mb,
                    memory_reserve_mb=resolved_memory_reserve_mb,
                    x264_low_memory=resolved_x264_low_memory,
                    segment_encode_timeout=segment_encode_timeout,
                    final_concat_timeout=final_concat_timeout,
                    stall_kill_timeout=stall_kill_timeout,
                    stall_warning_timeout=config.get("stall_warning_timeout", 120),
                    batch_chunk_size=batch_chunk_size,
                    min_part_bytes=min_part_bytes,
                )

                progress.update(
                    task3, completed=100, description="[green]+[/green] Video compressed"
                )

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
