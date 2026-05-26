
# stream2video

CLI tool to compress stream recordings by removing silence segments.

Automatically downloads VOD from YouTube/Twitch, detects silence using audio analysis, cuts out quiet parts, and concatenates the remaining video.

## Features

- **Automatic silence detection** - Uses audio analysis to identify and remove silence
- **Multiple cutting profiles** - Choose aggressive, balanced, or gentle compression
- **Configuration file support** - YAML/JSON config files for preset options
- **Robust error handling** - Specific error types with recovery paths
- **Progress tracking** - Real-time progress bars and logging

## Installation

```bash
# Clone or enter the repository
cd stream2video

# Create virtual environment with Python 3.12 via uv
uv venv --python 3.12 .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install package and dependencies
uv pip install -e .

# Verify installation
stream2video --help
```

## Dependencies

- **yt-dlp** (>=2024.01.01) - Download videos from YouTube/Twitch
- **auto-editor** (>=29.0.0) - Silence detection
- **ffmpeg** (system) - Video cutting and concatenation
- **typer** (>=0.12.0) - CLI framework
- **pyyaml** (>=6.0) - Config file parsing
- **rich** (>=13.0.0) - Progress bars and logging

### System Requirements

Ensure ffmpeg and ffprobe are installed:

```bash
# macOS
brew install ffmpeg

# Ubuntu/Debian
sudo apt-get install ffmpeg

# Windows
choco install ffmpeg
```

## Quick Start

### Basic Usage

Compress a YouTube video:

```bash
stream2video https://www.youtube.com/watch?v=VIDEO_ID
```

Compress a local video file:

```bash
stream2video /path/to/video.mp4
```

### Using Config Profiles

Aggressive (removes more silence):

```bash
stream2video video.mp4 --config config_aggressive.yaml
```

Balanced (default):

```bash
stream2video video.mp4 --config config_balanced.yaml
```

Gentle (preserves more):

```bash
stream2video video.mp4 --config config_gentle.yaml
```

### Custom Config

Create a custom YAML file:

```yaml
# my_config.yaml
threshold: -25    # Silence threshold in dB (-60 to -5)
min_silence: 0.7  # Minimum silence duration in seconds (0.1 to 60)
margin: 0.15      # Margin around cuts in seconds (0 to 5)
```

Then use it:

```bash
stream2video video.mp4 --config my_config.yaml --output ./output
```

## Configuration Parameters

### `threshold` (dB)
- **Range**: -60 to -5
- **Default**: -20
- **Effect**: Lower values = more aggressive silence removal
- **Lower threshold** (-60): Only removes very loud silence
- **Higher threshold** (-5): Removes even slight background noise

### `min_silence` (seconds)
- **Range**: 0.1 to 60
- **Default**: 0.5
- **Effect**: Minimum duration of silence to remove
- **Lower values** (0.1): Removes very short pauses
- **Higher values** (5+): Only removes longer pauses

### `margin` (seconds)
- **Range**: 0 to 5
- **Default**: 0.1
- **Effect**: Safety margin around silence segments
- **0**: Cut exactly at silence boundary
- **Higher values**: Keep slight buffer around cuts

## Usage Examples

### Compress 6-hour stream (balanced):

```bash
stream2video https://www.twitch.tv/recordings/VIDEO_ID \
  --output ./streams \
  --config config_balanced.yaml
```

### Aggressive compression for very talkative content:

```bash
stream2video recording.mp4 \
  --config config_aggressive.yaml \
  --output ./compressed \
  --log-level DEBUG
```

### Gentle compression to preserve natural pauses:

```bash
stream2video video.mp4 \
  --output ./output \
  --config config_gentle.yaml
```

## Output

Compressed video is saved as: `{filename}_compressed.mp4`

Logs are saved to: `{output_dir}/stream2video.log`

## Error Handling

If compression fails, check the log file for details:

```bash
tail -f ./compressed_videos/stream2video.log
```

Common errors:

| Error | Cause | Solution |
|-------|-------|----------|
| Video not available | URL is invalid/private | Check URL is public |
| Insufficient disk space | Not enough storage | Free up disk space |
| ffmpeg not found | ffmpeg not installed | Install via brew/apt |
| Permission denied | No write access | Check directory permissions |

## Development

### Run tests

```bash
pytest -v
```

### Run with debug logging

```bash
stream2video video.mp4 --log-level DEBUG
```

## Roadmap

- **Phase 1** (Current): Silence detection and removal
- **Phase 2**: Speech-to-text based filler detection
- **Phase 3**: Content filtering and smart cutting

## License

MIT

## Support

For issues or questions:

1. Check the logs: `stream2video.log` in output directory
2. Run with `--log-level DEBUG` for detailed debugging
3. Verify ffmpeg is installed and in PATH
