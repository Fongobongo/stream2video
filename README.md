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
| **Pillow** | >=10.0.0 | Waveform rendering in GUI (optional, `[gui]` extra) |

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
| `-vq, --video-quality` | `medium` | Encode quality preset: `high` (10000k / CRF 18), `medium` (7000k / CRF 23), `low` (3500k / CRF 28) |
| `-dq, --download-quality` | `best` | Download quality preset (Twitch/YouTube, ignored for local files): `best`, `1080p`, `720p`, `480p`, `360p` |
| `-m, --method` | `segment` | `segment` (per-segment encode + concat demuxer) or `batch` (frame-exact select/aselect) |
| `-f, --force` | — | Re-detect silence, ignore cache |
| `-c, --config` | — | YAML config file |
| `--delete-after` | — | Delete downloaded source after successful compression |
| `--per-video-dir` / `--no-per-video-dir` | (follows `per_video_dir` config) | Group all artifacts into `{output_dir}/{stem}/` |
| `-l, --log-level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

### Examples

```bash
# Choose encoder
stream2video video.mp4 --encoder h264_nvenc

# Download at 720p and encode at low quality (smaller output)
stream2video https://www.youtube.com/watch?v=VIDEO_ID --download-quality 720p --video-quality low

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
| `threshold` (dB) | -60 to -5 | -30.0 | Audio below this level = silence |
| `min_silence` (s) | 0.1 to 60 | 2.0 | Minimum silence duration to cut |
| `margin` (s) | -3 to 5 | 0.5 | How much to shrink silence zones. Positive = shrink silence (keep more audio around phrases). Negative = expand silence (cut more aggressively). `0` = no adjustment. |
| `video_quality` | `high`/`medium`/`low` | `medium` | Encode quality preset. Bitrate for HW encoders (10000k/7000k/3500k); CRF for libx264 (18/23/28). Also settable via `--video-quality`. |
| `download_quality` | `best`/`1080p`/`720p`/`480p`/`360p` | `best` | Max resolution to download from Twitch/YouTube (ignored for local files). Also settable via `--download-quality`. |
| `per_video_dir` | bool | `true` | When true, all artifacts (downloaded source, WAV, JSON, log, compressed, temp dirs) are collected into `{output_dir}/{stem}/` instead of living in the base `output_dir`. Local source files are never moved/copied — they stay where you put them. |

## Project directory

Set `per_video_dir: true` in config (or tick the checkbox in the GUI) to keep each video's artifacts in its own subdirectory:

```
output_dir/
└── myvideo/                       # per-video project dir
    ├── myvideo.mp4                # downloaded source (or local file untouched)
    ├── myvideo_audio.wav          # cached audio extract
    ├── myvideo_silence_cache.json
    ├── myvideo_compressed.mp4     # final output
    ├── stream2video.log           # per-video log
    ├── _myvideo_segments/         # temp dir (segment method), cleaned on success
    └── _myvideo_batch/            # temp dir (batch method), cleaned on success
```

Useful for keeping many videos in one `output_dir` without mixing their WAVs / logs / temp segments. Cache behavior is the same — just lives one level deeper. Local source files are never moved or copied.

### Recent Projects (GUI)

The GUI's left info panel has a **Recent Projects** section showing the 5 most recent project directories (those still on disk). Each entry has:

- **Click on the name** — opens the folder in your file manager.
- **Trash button (`X`)** — confirmation dialog ("This cannot be undone.") then recursively deletes the project dir, including the downloaded source (if applicable), the compressed output, the audio cache, the silence cache, and the log.

Entries are pruned automatically when their directory no longer exists. The list persists in `_portable/settings.json`.

### User defaults (GUI)

The GUI's left info panel has two related buttons:

- **Save current as defaults** — writes the current tunable settings (threshold, min_silence, margin, method, encoder, video_quality, download_quality, force, delete_after, per_video_dir, theme) to `_portable/user_defaults.json`. Per-session state (output_dir, recent_projects, input_path) is intentionally not saved.
- **Restore defaults** — restores those user defaults (or the factory `CONFIG_DEFAULTS` if no user defaults file exists).

This lets you set your preferred workflow once (e.g. `per_video_dir=True`, `encoder=libx264`) and have it stick across restarts, projects, and "Restore defaults" clicks — without having to edit code.

The file is plain JSON, type-validated on load, and atomic-written (via `os.replace`).

## Performance & Caching

stream2video caches work in two layers so that re-running on the same video is fast, even with different `threshold` / `min_silence` / `margin` settings.

### Two cache files

By default the cache files live in `{output_dir}/`. If `per_video_dir: true` is set, they live in `{output_dir}/{stem}/` instead (see "Project directory" below).

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
- **Per-video project directory** checkbox — group all of a video's artifacts into `{output_dir}/{stem}/`
- **Method**: segment (per-segment encode + concat demuxer) or batch (frame-exact select/aselect)
- **Encoder**: h264_nvenc, h264_amf, h264_mf, libx264
- **Video quality**: high / medium / low (bitrate for HW encoders, CRF for libx264)
- **Download quality**: best / 1080p / 720p / 480p / 360p (Twitch/YouTube, ignored for local files)
- **Test encoder** button
- **Progress bar** + **log panel** with real-time output
  - During download (Twitch/YouTube), the bar shows percent, downloaded/total size, speed, and ETA — parsed from yt-dlp's `--progress-template` output
- **Theme**: dark/light/system
- **Copy CLI command** — copies current params as CLI command to clipboard
- **Persistent settings** — remembers last used values
- **Save current as defaults** — snapshot current tunables to `user_defaults.json` for cross-session use
- **Recent Projects** — click-to-open or trash your last 5 project directories
- **Status line** — shows `elapsed / remaining` time per phase
- **Bottom overall label** — `Elapsed: X | Remaining: ~Y + ?` (or `Total: X` on completion)
- **Waveform preview tab** — visualises the audio with detected silence regions overlaid, so you can tune `threshold` / `min_silence` / `margin` without running a full encode

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
