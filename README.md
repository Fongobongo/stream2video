# stream2video

Compress stream recordings by removing silence segments.

Downloads VOD from YouTube/Twitch, detects silence via audio analysis, cuts out quiet parts, and concatenates the remaining video.

## Features

- **Automatic silence detection** — ffmpeg `silencedetect` filter
- **Cut methods**: `segment` (fast, per-segment encode + concat demuxer) or `batch` (frame-exact, select/aselect filter)
- **Hardware encoders**: NVIDIA NVENC, AMD AMF, Windows Media Foundation
- **Smart retry** — falls back to libx264 if hardware encoder fails
- **Two-layer cache** — audio-extract WAV + silence-segments, with safe fallback on broken timestamps
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
| `-e, --encoder` | `h264_mf` | `h264_nvenc`, `h264_amf`, `h264_mf`, `libx264` |
| `-m, --method` | `segment` | `segment` (per-segment encode + concat demuxer) or `batch` (frame-exact select/aselect) |
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
| `threshold` (dB) | -60 to -5 | -60.0 | Audio below this level = silence |
| `min_silence` (s) | 0.1 to 60 | 2.0 | Minimum silence duration to cut |
| `margin` (s) | -3 to 5 | 0.5 | How much to shrink silence zones. Positive = shrink silence (keep more audio around phrases). Negative = expand silence (cut more aggressively). `0` = no adjustment. |

## Performance & Caching

stream2video caches work in two layers so that re-running on the same video is fast, even with different `threshold` / `min_silence` / `margin` settings.

### Two cache files (in `{output_dir}/`)

| File | Key | What it stores |
|------|-----|----------------|
| `{stem}_audio.wav` | Source video mtime | Mono 16 kHz PCM audio extracted from the video (~10 MB per hour). Reused for any silence-detect parameters, and ready-made input for Phase 2 STT. |
| `{stem}_silence_cache.json` | `(threshold, min_silence, margin)` | Parsed silence segments. Skip re-running ffmpeg entirely when parameters haven't changed. |

### What happens on each run

**1st run, new video** (WAV cache miss, silence cache miss)
1. Extract audio to `{stem}_audio.wav` (with `-copyts` to preserve original timestamps).
2. Run ffmpeg `silencedetect` on the **WAV** (fast — audio-only, small file).
3. **Sample-verify**: run ffmpeg `silencedetect` on the **first 60 s of the original video** (with `-t 60`) and compare against the corresponding window of the WAV-based result.
   - Match → trust the WAV result, keep the cache.
   - Mismatch (rare — broken timestamps, unexpected `itsoffset`, etc.) → delete the WAV, fall back to a full direct detection on the video. A `WARNING` is logged naming the mismatch.

**2nd+ run, same video, same parameters** (WAV + silence cache hit)
- Load silence segments straight from JSON. No ffmpeg runs. **~instant.**

**2nd+ run, same video, different `threshold` / `min_silence`** (WAV hit, silence cache miss)
- Skip audio extract, skip sample-verify, skip video decode.
- Run ffmpeg `silencedetect` on the cached WAV only.
- Save the new segments to JSON.

### Typical speedups

On a 30 min 17 MB test video (lavfi sine + black):

| Run | Time | vs A-only |
|-----|------|-----------|
| 1st (cache miss) | 4.7 s | 0.74× |
| 2nd+ on cached WAV (different `threshold`) | 0.6 s | **10× faster** |
| A-only baseline (no WAV cache) | 6.4 s | 1.0× |

The first run costs slightly less than A-only because silencedetect on a small WAV is faster than on a full video even with one extract. Subsequent runs skip the expensive video decode entirely.

### Forcing a fresh run

- **GUI**: enable the `Force` checkbox.
- **CLI**: pass `-f` / `--force`.

This skips the silence-segment cache (the WAV is still kept and reused if its mtime is still valid).

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
