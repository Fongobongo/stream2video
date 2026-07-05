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
from typer._click.core import ParameterSource

from stream2video.concat import ConcatError, cut_and_concat
from stream2video.config import (
    CONFIG_DEFAULTS,
    CONFIG_RANGES,
    VALID_DOWNLOAD_QUALITIES,
    VALID_ENCODERS,
    VALID_METHODS,
    VALID_QUALITIES,
    VALID_THEMES,
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
from stream2video.formatters import fmt_size, fmt_speed
from stream2video.paths import apply_per_video_dir
from stream2video.silence import (
    SilenceCancelledError,
    SilenceDetectionError,
    detect_silence,
    load_silence_cache,
    save_silence_cache,
)

# Setup logging
_console_handler = RichHandler(rich_tracebacks=True)
logging.basicConfig(
    level=logging.DEBUG,
    format="%(message)s",
    handlers=[_console_handler],
)
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

    def _handler(signum, frame):
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


def _check_ffmpeg():
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

    if config_file:
        if not config_file.exists():
            console.print(f"[yellow]Warning:[/yellow] Config file not found: {config_file}")
        else:
            try:
                with open(config_file) as f:
                    file_config = yaml.safe_load(f) or {}

                if not isinstance(file_config, dict):
                    raise ValueError("Config file must contain a dictionary")

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

    # Validate enum keys against their VALID_* lists. A bad value in
    # either the YAML or CONFIG_DEFAULTS is rejected here so downstream
    # code can assume the value is one of the allowed tokens.
    enum_specs = [
        ("method", VALID_METHODS),
        ("encoder", VALID_ENCODERS),
        ("video_quality", VALID_QUALITIES),
        ("download_quality", VALID_DOWNLOAD_QUALITIES),
        ("theme", VALID_THEMES),
    ]
    for key, valid in enum_specs:
        v: Any = config.get(key)
        if v is None:
            continue
        if v not in valid:
            console.print(
                f"[red]Invalid {key}:[/red] {v!r} "
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
        "segment",
        "--method",
        "-m",
        help="Concat method: 'segment' (fast, ~1.5h) or 'batch' (select/aselect filter, ~6-7h). "
        "If not passed, the config file's `method` key is used.",
    ),
    encoder: str = typer.Option(
        "h264_mf",
        "--encoder",
        "-e",
        help="Video encoder: 'h264_nvenc' (NVIDIA), 'h264_amf' (AMD), 'h264_mf' "
        "(Media Foundation, default), or 'libx264' (CPU fallback). If not passed, "
        "the config file's `encoder` key is used.",
    ),
    video_quality: str = typer.Option(
        "medium",
        "--video-quality",
        "-vq",
        help="Encode quality preset: 'high' (10000k / CRF 18), 'medium' (7000k / CRF 23, default), "
        "or 'low' (3500k / CRF 28). If not passed, the config file's `video_quality` key is used.",
    ),
    download_quality: str = typer.Option(
        "best",
        "--download-quality",
        "-dq",
        help="Download quality preset (Twitch/YouTube, ignored for local files): "
        "'best' (default), '1080p', '720p', '480p', '360p'. If not passed, the "
        "config file's `download_quality` key is used.",
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
):
    """
    Compress stream recording by removing silence segments.

    Processes:
    1. Download video (if URL provided)
    2. Detect silence segments
    3. Cut and concatenate video
    """
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
    cancel_event, cancel_cb = _make_sigint_cancel()

    fh = None
    try:
        fh = _make_file_handler(log_file)
        logger.addHandler(fh)

        # Load configuration. ``load_config``严格-validates BOTH numeric
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

        def _resolved_bool(name: str, flag_value: bool | None) -> bool:
            src = ctx.get_parameter_source(name)
            if src == ParameterSource.COMMANDLINE:
                return bool(flag_value)
            # Config already type-checked in load_config; bool is enforced.
            return bool(config[name])

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
                elif p.downloaded_bytes:
                    # Known downloaded, unknown total — show at least the
                    # bytes that came in so the bar visibly advances.
                    progress.update(task1, total=None, completed=0.0)
                    progress.update(
                        task1,
                        description=f"[cyan]Downloading... {fmt_size(int(p.downloaded_bytes))} "
                        f"at {fmt_speed(p.speed)}",
                    )
                    return
                pct = (
                    100.0 * (p.downloaded_bytes or 0.0) / p.total_bytes
                    if p.total_bytes
                    else 0.0
                )
                progress.update(
                    task1,
                    description=f"[cyan]Downloading... {pct:.1f}% "
                    f"at {fmt_speed(p.speed)} ETA {int(p.eta or 0)}s",
                )

            try:
                logger.info(f"Processing: {input_video}")
                download_result = download(
                    input_video,
                    output_dir,
                    cancel_callback=cancel_cb,
                    quality=download_quality,
                    progress_callback=_download_progress_cb,
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

            # Step 1.5: Apply per-video project directory (if enabled).
            if per_video_dir_resolved:
                new_output, video_path = apply_per_video_dir(
                    output_dir, video_path, download_result.is_downloaded
                )
                if new_output != output_dir:
                    if download_result.is_downloaded:
                        logger.info(f"Moved source into project dir: {video_path}")
                    if fh is not None:
                        fh.close()
                        logger.removeHandler(fh)
                        new_log = new_output / "stream2video.log"
                        if new_log.exists():
                            new_log.unlink()
                        if log_file.exists():
                            shutil.move(str(log_file), str(new_log))
                        fh = _make_file_handler(new_log)
                        logger.addHandler(fh)
                    output_dir = new_output
                    console.print(f"Project directory: [cyan]{output_dir}[/cyan]")

            # Step 2: Detect silence (with cache support)
            task2 = progress.add_task("[cyan]Detecting silence segments...", total=100)

            try:
                silence_segments = None
                if not force:
                    silence_segments = load_silence_cache(video_path, output_dir, config)

                if silence_segments is None:

                    def silence_progress(f: float):
                        progress.update(task2, completed=min(f * 100, 100))

                    silence_segments = detect_silence(
                        video_path,
                        threshold=config["threshold"],
                        min_silence=config["min_silence"],
                        margin=config["margin"],
                        output_dir=output_dir,
                        progress_callback=silence_progress,
                        cancel_callback=cancel_cb,
                    )
                    save_silence_cache(video_path, silence_segments, output_dir, config)

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

            def update_progress(fraction: float):
                progress.update(task3, completed=min(fraction * 100, 100))

            try:
                output_video = output_dir / f"{video_path.stem}_compressed.mp4"

                cut_and_concat(
                    video_path,
                    silence_segments,
                    output_video,
                    progress_callback=update_progress,
                    method=method,
                    encoder=encoder,
                    video_quality=video_quality,
                    cancel_callback=cancel_cb,
                )

                progress.update(
                    task3, completed=100, description="[green]+[/green] Video compressed"
                )

            except ConcatError as e:
                if cancel_event.is_set():
                    console.print("[yellow]Concatenation cancelled.[/yellow]")
                    raise typer.Exit(130) from None
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
            except (OSError, ValueError, TypeError):
                pass
        if fh is not None:
            logger.removeHandler(fh)
            fh.close()


if __name__ == "__main__":
    app()
