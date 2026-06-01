"""CLI entry point using Typer."""

import logging
import shutil
from pathlib import Path
from typing import Optional

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

from stream2video.download import download, DownloadError
from stream2video.silence import (
    detect_silence,
    save_silence_cache,
    load_silence_cache,
    SilenceDetectionError,
)
from stream2video.concat import cut_and_concat, ConcatError

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[RichHandler(rich_tracebacks=True)],
)
logger = logging.getLogger("stream2video")

console = Console()
app = typer.Typer(help="Compress stream recordings by removing silence")


def _check_ffmpeg():
    """Warn if ffmpeg is missing."""
    if not shutil.which("ffmpeg"):
        console.print("[red]Error:[/red] ffmpeg not found in PATH")
        console.print("  Install: [cyan]winget install Gyan.FFmpeg[/cyan]")
        console.print("  Or run:  [cyan]setup.ps1[/cyan] (Windows)")
        raise typer.Exit(1)


# Config validation ranges
CONFIG_RANGES = {
    "threshold": (-60, -5),
    "min_silence": (0.1, 60),
    "margin": (-3, 5),
}

# Config defaults
CONFIG_DEFAULTS = {
    "threshold": -20,
    "min_silence": 1.0,
    "margin": -0.5,
}


def load_config(config_file: Optional[Path]) -> dict:
    """Load and validate configuration file."""
    config = CONFIG_DEFAULTS.copy()

    if config_file and config_file.exists():
        try:
            with open(config_file) as f:
                file_config = yaml.safe_load(f) or {}

            if not isinstance(file_config, dict):
                raise ValueError("Config file must contain a dictionary")

            # Update with file config
            config.update(file_config)

            logger.info(f"Loaded config from {config_file}")

        except yaml.YAMLError as e:
            console.print(f"[red]Error parsing config file:[/red] {e}")
            raise typer.Exit(1)

        except Exception as e:
            console.print(f"[red]Error loading config file:[/red] {e}")
            raise typer.Exit(1)

    # Validate ranges
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

            except (ValueError, TypeError) as e:
                console.print(f"[red]Invalid {key}:[/red] {config[key]} is not a number")
                raise typer.Exit(1)

    logger.debug(f"Final config: {config}")
    return config


@app.command()
def main(
    input_video: str = typer.Argument(..., help="URL or path to input video"),
    output_dir: Path = typer.Option(
        Path("./compressed_videos"),
        "--output",
        "-o",
        help="Output directory for compressed video",
    ),
    config_file: Optional[Path] = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to YAML config file",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Re-detect silence, ignore cache",
    ),
    method: str = typer.Option(
        "batch",
        "--method",
        "-m",
        help="Concat method: 'segment' (fast, ~1.5h) or 'batch' (original select/aselect, ~6-7h)",
    ),
    encoder: str = typer.Option(
        "libx264",
        "--encoder",
        "-e",
        help="Video encoder: 'h264_nvenc' (NVIDIA), 'h264_amf' (AMD), 'h264_mf' (Media Foundation), or 'libx264' (CPU, default)",
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
    # Validate method and encoder
    if method not in ("segment", "batch"):
        console.print(f"[red]Invalid method:[/red] {method!r} (use 'segment' or 'batch')")
        raise typer.Exit(1)
    if encoder not in ("h264_nvenc", "h264_amf", "h264_mf", "libx264"):
        console.print(f"[red]Invalid encoder:[/red] {encoder!r} (use h264_nvenc, h264_amf, h264_mf, or libx264)")
        raise typer.Exit(1)

    # Verify ffmpeg is available
    _check_ffmpeg()

    # Set log level
    logger.setLevel(log_level.upper())

    # Ensure output directory exists
    output_dir.mkdir(parents=True, exist_ok=True)
    log_file = output_dir / "stream2video.log"

    # Add file handler for logs
    fh = logging.FileHandler(log_file)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    logger.addHandler(fh)

    console.print(f"\n[bold cyan]stream2video[/bold cyan] - Compress by removing silence")
    console.print(f"Logs saved to: {log_file}\n")

    try:
        # Load configuration
        config = load_config(config_file)

        progress_columns = [
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
        ]

        with Progress(*progress_columns, console=console) as progress:
            # Step 1: Download video
            task1 = progress.add_task("[cyan]Downloading video...", total=None)

            try:
                logger.info(f"Processing: {input_video}")
                video_path = download(input_video, output_dir)
                progress.update(task1, completed=True, description="[green]+[/green] Video downloaded")

            except DownloadError as e:
                console.print(f"[red]Download failed:[/red] {e}")
                logger.exception("Download error")
                raise typer.Exit(1)

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
                        progress_callback=silence_progress,
                    )
                    save_silence_cache(video_path, silence_segments, output_dir, config)

                progress.update(task2, completed=100, description=f"[green]+[/green] Found {len(silence_segments)} silence segments")

            except SilenceDetectionError as e:
                console.print(f"[red]Silence detection failed:[/red] {e}")
                logger.exception("Silence detection error")
                raise typer.Exit(1)

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
                )

                progress.update(task3, completed=100, description="[green]+[/green] Video compressed")

            except ConcatError as e:
                console.print(f"[red]Concatenation failed:[/red] {e}")
                logger.exception("Concatenation error")
                raise typer.Exit(1)

        # Summary
        console.print("\n[bold green]+ Compression complete![/bold green]")
        console.print(f"Output: [cyan]{output_video}[/cyan]")

        if video_path != Path(input_video):
            logger.info(f"Temporary download file: {video_path}")

        logger.info(f"Successfully compressed video to {output_video}")

    except typer.Exit:
        raise

    except Exception as e:
        console.print(f"[red]Unexpected error:[/red] {e}")
        logger.exception("Unexpected error")
        raise typer.Exit(1)

    finally:
        logger.removeHandler(fh)
        fh.close()


if __name__ == "__main__":
    app()
