# stream2video

Compress stream recordings by removing silence segments.

Downloads VOD from YouTube/Twitch, detects silence via audio analysis, cuts out quiet parts, and concatenates the remaining video.

## Features

- **Automatic silence detection** — ffmpeg `silencedetect` filter
- **Cut methods**: `segment` (fast, per-segment encode + concat demuxer) or `batch` (frame-exact, trim+concat filter)
- **Hardware encoders**: NVIDIA NVENC, AMD AMF, Windows Media Foundation
- **Safe encoder fallback policy** — `ask` (default) refuses silent fallback to libx264; the user must consent before a CPU-heavy encode runs. `disabled` raises immediately, `enabled` preserves the legacy silent-fallback behaviour.
- **Audio quality presets** — `high` (256k), `medium` (192k, default), `low` (128k). A 192k/256k/320k source is no longer silently downgraded to 128k without the user's choice.
- **Output FPS policy** — `source` (default) preserves the input's frame cadence without duplication; `24`/`25`/`30`/`50`/`60` force CFR conversion via the `fps` filter (duplicated frames warn about file-size cost).
- **Resume integrity** — segment/batch working directories contain a `_manifest.json` that snapshots (source path/size/mtime, encoder, quality, keep segments, pipeline version); a mismatch wipes the work dir so old artifacts from an incompatible run cannot be reused. Each resumed chunk is ffprobe-validated for missing moov atoms.
- **Download watchdog** — `_CONNECT_TIMEOUT` (5 min, first byte), `_NO_PROGRESS_TIMEOUT` (30 min, mid-download stall), and `_DOWNLOAD_TIMEOUT` (8 h, absolute ceiling) catch hung connections before the user stares at a frozen bar. All three are now configurable via `--download-timeout` / `--connect-timeout` / `--no-progress-timeout`.
- **Memory monitor** — optional `psutil`-based watchdog (`[monitor]` extra) cancels a runaway encode when RSS exceeds a configurable budget or when available RAM drops below the OS reserve. Soft threshold (80% of budget) warns; hard threshold (95% or reserve violation) cancels via the existing cancel path.
- **Two-layer cache** — audio-extract WAV + silence-segments, with safe fallback on broken timestamps
- **Progress bars** + detailed logging
- **Cross-platform GUI** (CustomTkinter) with waveform preview, dry-run silence detection, and recent-projects panel
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
| **psutil** | >=5.9.0 | RAM monitor for runaway-encode guardrail (optional, `[monitor]` extra; without it the memory monitor degrades to a no-op and the historical behaviour is preserved) |

Python **3.13+** is required (matches `.python-version` and CI).

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
| `-aq, --audio-quality` | `medium` | Audio (AAC) bitrate preset: `high` (256k), `medium` (192k), `low` (128k) |
| `-dq, --download-quality` | `best` | Download quality preset (Twitch/YouTube, ignored for local files): `best`, `1080p`, `720p`, `480p`, `360p` |
| `-m, --method` | `segment` | `segment` (per-segment encode + concat demuxer) or `batch` (frame-exact trim+concat filter) |
| `--software-fallback` | `ask` | What happens when the requested HW encoder is unavailable or fails mid-run: `ask` (refuse silent fallback — the run fails with a clear error), `disabled` (fail immediately), `enabled` (silently retry with libx264, legacy behaviour) |
| `--x264-preset` | `medium` | libx264 preset: `ultrafast`/`superfast`/`veryfast`/`faster`/`fast`/`medium`/`slow`/`slower`. Faster presets reduce CPU load at the cost of file size / quality. Use `ultrafast` or `veryfast` on an unstable / overclocked CPU. |
| `--encoder-threads` | `auto` | Encoder thread count: `auto` (let ffmpeg pick, usually one per logical core) or a positive int to cap libx264's thread pool. Lowering this reduces peak CPU at the cost of slower encode. |
| `--output-fps` | `source` | Output FPS policy: `source` (preserve input cadence, no frame duplication) or `24`/`25`/`30`/`50`/`60` (force CFR conversion via `fps` filter; duplicated frames inflate size). |
| `--output-format` | `video` | Output container/codec: `video` (H.264 + AAC MP4, default) or an audio-only format — `mp3` (libmp3lame), `opus` (libopus), `aac` (m4a), `wav` (PCM 16-bit, lossless), `flac` (lossless). Audio-only outputs drop the video stream entirely; `audio_quality` controls bitrate on lossy formats, ignored on lossless. |
| `--gapless-concat` / `--no-gapless-concat` | off | Re-encode audio in the final concat pass so per-segment AAC priming (~21ms per segment) doesn't accumulate as A/V drift on multi-segment outputs. Video is re-encoded too (one generation loss); for lossless video + gapless audio use `cut_then_encode` instead. Default off (concat demuxer, faster). |
| `--low-process-priority` / `--no-low-process-priority` | off | Spawn ffmpeg at a lower scheduling priority so a long-running encode doesn't starve interactive applications. On Windows: `BELOW_NORMAL_PRIORITY_CLASS`; on Linux/macOS: nice +10. Useful for unattended batch processing on shared/desktop machines. Default off (normal priority, faster encoding). |
| `--preset` | `balanced` | Resource preset — a bundle of tunables (`x264_low_memory`, `memory_limit_mb`, `batch_chunk_size`, `low_process_priority`) applied as a baseline before any explicit `--flag` overrides. `low_memory` trades speed for stability on 4-8 GB machines (`x264_low_memory=True`, `batch_chunk_size=20`, `low_process_priority=True`); `balanced` reproduces the historical defaults; `maximum_performance` trades RAM for throughput (`x264_low_memory=False`, `memory_limit_mb=0`, `batch_chunk_size=80`). Explicit flags win on a per-key basis — e.g. `--preset low_memory --no-low-process-priority` keeps the other low-memory tunables but flips priority back off. |
| `--download-timeout` | `28800` | Absolute ceiling for the whole download in seconds (8h, sized for big VODs). Ignored for local files. |
| `--connect-timeout` | `300` | Seconds to wait for the first progress event (DNS+TLS+handshake+first byte) before killing yt-dlp with a clear timeout error. Increase on very slow / satellite links. |
| `--no-progress-timeout` | `1800` | Seconds of silence mid-download before killing yt-dlp (stalled connection watchdog). Increase for very slow / unstable links where mid-download pauses are normal. |
| `--segment-timeout` | `600` | Per-segment encode timeout in seconds (10 min). Raise for very long segments or slow hardware. |
| `--final-concat-timeout` | `86400` | Final concat-demuxer timeout in seconds (24h, absolute ceiling on the final pass). |
| `--silence-timeout` | `36000` | Silence detection ceiling in seconds (10h). |
| `--stall-timeout` | `300` | No-progress kill timeout in seconds (5 min). ffmpeg is killed if no progress line arrives within this window. |
| `--waveform-timeout` | `300` | Waveform preview decode timeout in seconds (5 min). |
| `--batch-chunk-size` | `40` | Number of keep-segments per batch filter invocation. Scaled down dynamically for large segment counts. |
| `--min-part-bytes` | `1024` | Minimum bytes for a resumed part to be considered valid. Smaller files are re-encoded. |
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

# Extract audio only (mp3; also: opus, aac, wav, flac)
stream2video video.mp4 --output-format mp3 --audio-quality high
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
| `audio_quality` | `high`/`medium`/`low` | `medium` | Audio (AAC) bitrate preset: `high` (256k), `medium` (192k), `low` (128k). Also settable via `--audio-quality`. |
| `download_quality` | `best`/`1080p`/`720p`/`480p`/`360p` | `best` | Max resolution to download from Twitch/YouTube (ignored for local files). Also settable via `--download-quality`. |
| `software_fallback` | `ask`/`disabled`/`enabled` | `ask` | What happens when the requested HW encoder is unavailable or fails mid-run. `ask` (default) refuses silent fallback to libx264; `disabled` raises immediately; `enabled` preserves the legacy silent-fallback behaviour. |
| `x264_preset` | `ultrafast`..`slower` | `medium` | libx264 preset. Faster presets reduce CPU load at the cost of file size / quality. |
| `encoder_threads` | `auto` or positive int | `auto` | Caps libx264's thread pool. `auto` lets ffmpeg pick (usually one per logical core). |
| `output_fps` | `source`/`24`/`25`/`30`/`50`/`60` | `source` | Output FPS policy. `source` preserves the input's cadence without duplication; numeric values force CFR conversion via the `fps` filter. |
| `output_format` | `video`/`mp3`/`opus`/`aac`/`wav`/`flac` | `video` | Output container/codec. `video` produces H.264 + AAC MP4; the audio-only values drop the video stream and produce a standalone audio file (mp3=libmp3lame, opus=libopus, aac=m4a, wav=PCM 16-bit lossless, flac=lossless). `audio_quality` controls bitrate on lossy formats, ignored on lossless. |
| `gapless_concat` | bool | `false` | When True, the segment path's final join uses the `concat` filter (re-encode) instead of the `concat` demuxer (stream copy) so per-segment AAC priming (~21ms per segment) doesn't accumulate as A/V drift on multi-segment outputs. Video is re-encoded (one generation loss); for lossless video + gapless audio use `cut_then_encode`. Default off (faster). |
| `low_process_priority` | bool | `false` | When True, spawned ffmpeg subprocesses use `BELOW_NORMAL_PRIORITY_CLASS` on Windows and nice +10 on POSIX so a long encode doesn't starve interactive applications. See `subprocess_kwargs` in utils.py. Default off (faster). |
| `preset` | `low_memory`/`balanced`/`maximum_performance` | `balanced` | Resource preset — a bundle of tunables (`x264_low_memory`, `memory_limit_mb`, `batch_chunk_size`, `low_process_priority`) applied as a baseline before any explicit override. `balanced` reproduces the historical defaults (empty override dict). See `PRESETS` / `apply_preset` in config.py. Explicit keys in the config file or `--flag` on the CLI win on a per-key basis. |
| `memory_limit_mb` | `auto` or non-negative int | `auto` | RAM budget for the encode (soft + hard thresholds). `auto` = 60% of total RAM at run start; `0` disables the budget check (OS reserve still enforced). Requires the `[monitor]` extra (psutil). |
| `memory_reserve_mb` | positive int | `2048` | Hard floor of available RAM the pipeline never violates — even when the budget hasn't been hit, going below this triggers a cancel so the OS doesn't swap. |
| `download_timeout` | positive int | `28800` | Absolute ceiling for the whole download in seconds (8h, sized for big VODs). |
| `connect_timeout` | positive int | `300` | Seconds to wait for the first progress event before killing yt-dlp. |
| `no_progress_timeout` | positive int | `1800` | Seconds of silence mid-download before killing yt-dlp (stalled connection watchdog). |
| `segment_encode_timeout` | positive int (1-604800) | `600` | Per-segment encode timeout in seconds (10 min). Raise for very long segments or slow hardware. |
| `final_concat_timeout` | positive int (1-604800) | `86400` | Final concat-demuxer timeout in seconds (24h ceiling). |
| `silence_timeout` | positive int (1-604800) | `36000` | Silence detection ceiling in seconds (10h). |
| `stall_kill_timeout` | positive int (10-3600) | `300` | No-progress kill timeout in seconds. ffmpeg is killed if no progress line arrives within this window. |
| `stall_warning_timeout` | positive int (5-1800) | `120` | No-progress warning threshold in seconds. A warning is logged but ffmpeg is not killed yet. |
| `waveform_timeout` | positive int (10-3600) | `300` | Waveform preview decode timeout in seconds (5 min). |
| `batch_chunk_size` | positive int (1-500) | `40` | Number of keep-segments per batch filter invocation. Scaled down dynamically for large segment counts. |
| `min_part_bytes` | positive int (1-10485760) | `1024` | Minimum bytes for a resumed part to be considered valid. Smaller files are re-encoded. |
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
| Hardware encoder unavailable | Driver missing / encoder not in this ffmpeg build | Install the encoder or pick another (default policy `ask` refuses silent fallback to libx264 — set `software_fallback=enabled` to allow the legacy silent retry) |
| Download stalled before first byte | DNS / TLS / handshake hang | `--connect-timeout` (default 300s) kills yt-dlp with a clear error instead of waiting the 8h ceiling |
| Download stalled mid-stream | Server stopped sending / route dropped | `--no-progress-timeout` (default 1800s) watchdog kills the process |
| Memory budget exceeded | RSS grew past 95% of `memory_limit_mb` or available RAM dropped below `memory_reserve_mb` | Lower `--video-quality`, switch to `segment` method, lower `--encoder-threads`, or raise `memory_limit_mb` (requires `[monitor]` extra / psutil) |
| Encoder failed mid-run | HW encoder crashed (driver, bad input) | Set `software_fallback=enabled` to allow libx264 retry; `disabled` for unattended CI runs that should fail fast |
| Resume cache mismatch | Source changed, encoder/quality changed, pipeline version bumped | Working dir is wiped automatically and the encode restarts from scratch — no action needed |

## Development

```bash
pytest -v
stream2video video.mp4 --log-level DEBUG
```

## License

MIT
