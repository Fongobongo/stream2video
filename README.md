# stream2video

Compress stream recordings by removing silence segments.

Downloads VOD from YouTube/Twitch, detects silence via audio analysis, cuts out quiet parts, and concatenates the remaining video.

## Features

- **Automatic silence detection** — ffmpeg `silencedetect` filter
- **Cut methods**: `segment` (fast, per-segment encode + concat demuxer) or `batch` (frame-exact, select/aselect filter)
- **Hardware encoders**: NVIDIA NVENC, AMD AMF, Windows Media Foundation
- **Smart retry** — falls back to libx264 if hardware encoder fails
- **Silence cache** — skips re-detection if parameters haven't changed
- **Progress bars** + detailed logging
- **Cross-platform GUI** (CustomTkinter)
- **Portable mode** — self-contained `_portable/` with venv + ffmpeg

## Installation

### Windows (portable)

```cmd
run_gui.cmd
```

The script auto-installs Python 3.13 + ffmpeg into `_portable/` on first run.

### Manual

```bash
pip install -e .

# Install ffmpeg (required)
# Windows: winget install Gyan.FFmpeg
# macOS:   brew install ffmpeg
# Linux:   sudo apt install ffmpeg
```

## Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| **yt-dlp** | >=2024.01.01 | Download videos from YouTube/Twitch |
| **ffmpeg** | system | Silence detection + video cutting |
| **typer** | >=0.12.0 | CLI framework |
| **pyyaml** | >=6.0 | Config file parsing |
| **rich** | >=13.0.0 | Progress bars and logging |
| **customtkinter** | >=5.2.0 | GUI (optional, `[gui]` extra) |

## CLI Usage

```bash
stream2video <input> [options]
```

### Basic

```bash
stream2video https://www.youtube.com/watch?v=VIDEO_ID
stream2video /path/to/video.mp4
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `-o, --output` | `./compressed_videos` | Output directory |
| `-e, --encoder` | `libx264` | `h264_nvenc`, `h264_amf`, `h264_mf`, `libx264` |
| `-m, --method` | `batch` | `segment` (per-segment encode + concat demuxer) or `batch` (frame-exact select/aselect) |
| `-f, --force` | — | Re-detect silence, ignore cache |
| `-c, --config` | — | YAML config file |
| `-l, --log-level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

### Examples

```bash
# Choose encoder
stream2video video.mp4 --encoder h264_nvenc

# Specify output directory
stream2video video.mp4 -o ./output --method batch

# Custom config
stream2video video.mp4 --config my_config.yaml
```

## Configuration

Parameters can be set via YAML config file:

```yaml
threshold: -25
min_silence: 0.7
margin: 0.15
```

| Parameter | Range | Default | Description |
|-----------|-------|---------|-------------|
| `threshold` (dB) | -60 to -5 | -20 | Audio below this level = silence |
| `min_silence` (s) | 0.1 to 60 | 1.0 | Minimum silence duration to cut |
| `margin` (s) | -3 to 5 | -0.5 | Extra padding around cuts (positive shrinks silence, negative expands it) |

## GUI

Cross-platform desktop GUI built with CustomTkinter.

### Launch

**Windows** (portable, self-contained):

```cmd
run_gui.cmd
```

**Any platform** (with Python):

```bash
pip install -e ".[gui]"
python -m stream2video.gui
```

### GUI Features

- **Input**: Local file (Browse) or URL
- **Output**: Select output directory (defaults to `./compressed_videos`)
- **Sliders**: Threshold, Min Silence, Margin
- **Method**: segment (per-segment encode + concat demuxer) or batch (frame-exact select/aselect)
- **Encoder**: h264_nvenc, h264_amf, h264_mf, libx264
- **Test encoder** button
- **Progress bar** + **log panel** with real-time output
- **Theme**: dark/light/system
- **Copy CLI command** — copies current params as CLI command to clipboard
- **Persistent settings** — remembers last used values

## Output

Compressed video: `{filename}_compressed.mp4`
Logs: `{output_dir}/stream2video.log`

## Error Handling

| Error | Cause | Solution |
|-------|-------|----------|
| Video unavailable | URL invalid/private | Check URL is public |
| ffmpeg not found | ffmpeg missing | Install via winget/brew/apt |
| Disk full | Not enough space | Free up disk space |
| Encoder failed | HW encoder unavailable | Auto-falls back to libx264 |

## Development

```bash
pytest -v
stream2video video.mp4 --log-level DEBUG
```

## License

MIT
