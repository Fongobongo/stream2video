# Changelog

## [0.3] - 2026-08-01

### Added

- **New cutting method `cut_then_encode`** — the best-quality mode: cuts silenced parts losslessly, then runs a single encode pass at the end. One generation of encoding, so the picture stays closest to the source; cutting granularity follows keyframes.
- **Quality presets** — pick how the output is encoded instead of editing bitrates by hand: `--video-quality` and `--audio-quality` (`high` / `medium` / `low`, plus `source` to keep the encoder's own defaults), and `--download-quality` (`best` / `1080p` / `720p` / `480p` / `360p`) to cap the resolution fetched from YouTube/Twitch. All are also in the GUI as dropdowns.
- **Audio-only output** — `--output-format mp3` / `opus` / `aac` / `wav` / `flac` extracts just the audio (silence still removed). The output gets the matching file extension automatically.
- **Audio/video sync options** — new `--gapless-concat` removes the tiny audio gaps that used to accumulate at every cut point on long videos; `--output-fps` forces a constant frame rate (24/25/30/50/60) when the source cadence causes playback issues.
- **Resource presets (`--preset`)** — one switch for low-end or busy machines: `low_memory` (4–8 GB RAM), `low_cpu` (keeps the system responsive while encoding), `balanced` (default), `maximum_performance` (fastest if you have RAM to spare). Any explicit flag still overrides its preset value.
- **Memory protection** — a background watchdog (optional psutil dependency) cancels a runaway encode before the machine starts swapping, and the pipeline refuses to start a heavy phase when free RAM is already below the safe reserve (`--memory-limit-mb`, `--memory-reserve-mb`). When ffmpeg is killed by the OS for running out of memory, the error now says so in plain words with tips on which settings to lower. On Linux/macOS, `--rlimit-as-mb` can additionally hard-cap ffmpeg's memory at the kernel level.
- **Download health monitoring** — three timeouts catch a stuck download early: no first byte in 5 minutes, no progress for 30 minutes, or 8 hours total (`--connect-timeout`, `--no-progress-timeout`, `--download-timeout`, all configurable).
- **Download progress display** — both CLI and GUI now show live percent, size, speed, and ETA while downloading instead of a bare "Downloading video..." line.
- **Waveform preview overhaul** — the preview now shows peaks immediately and overlays the detected silence regions on top as they are found (even mid-pipeline), supports zoom (cursor-anchored) + horizontal pan (buttons, slider, or drag), a time/dB tooltip under the cursor, and a dry-run detection when no cache exists yet. Opening it after a run reuses the saved cache instead of re-detecting.
- **Resume after interruption** — silence detection checkpoints its progress every 30 seconds, so a cancelled or crashed run continues from the last checkpoint instead of starting over. Partially encoded video segments are verified before reuse, and any artifacts left from a run with different settings are discarded automatically.
- **Completion sound (GUI)** — optional short chime when the pipeline finishes, with a different tone on cancel/failure ("Sound when done" checkbox). Works on Windows and macOS out of the box; on Linux uses whatever player is installed (`paplay`, `aplay`, or `ffplay`).
- **Finer encode control** — `--x264-preset` (speed/size trade-off for CPU encoding), `--encoder-threads` (cap CPU usage), `--x264-low-memory` (less RAM, slightly larger files), and `--low-process-priority` (ffmpeg stays in the background so the PC stays usable).
- **Encoder fallback policy** (`software_fallback`) — when the selected GPU encoder is missing or crashes, the run now asks instead of silently switching to slow CPU encoding; `enabled` restores the old automatic behaviour, `disabled` fails fast.
- **Smarter progress bar** — the GUI progress bar adapts its scale per run (a local file with cached silence detection jumps straight to the encode part) and now shows an explicit percentage next to the bar.
- **Benchmark script** — `scripts/benchmark_presets.py` compares x264 speed presets on a synthetic clip so you can pick the fastest acceptable one for your hardware.
- **Safer cancellation** — Cancel in the GUI (and Ctrl+C in the CLI) now reliably kills the running ffmpeg/yt-dlp process, including cases where it was stuck writing output.

### Changed

- **Frame-accurate cutting** — both `segment` and `batch` methods were reworked: cuts now land exactly on the requested timestamps (before, each segment could lose ~0.5 seconds, and the batch mode inserted a 1-second freeze where silence was removed). Batch mode also no longer re-decodes the file from the start for every chunk — a 6-hour stream with 100 chunks used to decode 600 hours of video, now it decodes 6.
- **Better default downloads** — `best` download quality now prefers separate best video + best audio tracks (often 1080p where the old logic grabbed a pre-merged 720p).
- **No more audio downgrade** — audio is encoded at the quality you pick (192k by default); a 256k+ source is no longer silently compressed to 128k.
- **No more accumulated drift** — the 100 ms per-segment audio padding that added up to seconds of drift on very long videos is gone.
- **GUI layout** — the main window defaults to 1280×720 and fits all controls without scrolling; input/output and quality pickers are tightened into two columns.
- **GUI presets behave as expected** — switching the resource preset back to `balanced` actually restores the default tunables, and the selected preset is remembered across restarts.
- **Python 3.13 is now the baseline.** Windows ARM64 is still supported; CI runs the full test suite on both Ubuntu and Windows.

### Fixed

- Downloaded-file detection no longer misfires when yt-dlp prints extra lines after the download (the run used to report "file not found" despite a successful download).
- Slow video-host page loading no longer trips the "stalled before first byte" watchdog — the timer now only fires when yt-dlp is truly silent.
- Download timeout errors now include yt-dlp's own error message (e.g. "HTTP 403") instead of a bare "timed out".
- Videos without an audio track, with odd channel layouts (mono/5.1), or with shifted timestamps (a known replay-capture quirk) now produce a valid, in-sync output instead of failing or freezing.
- Videos where silence runs to the very end no longer have that last pause kept in the output.
- Sources from systems with a comma as the decimal separator no longer break silence detection.
- The GUI's "Silence" info line now actually shows the detected segment counts.
- The waveform threshold line now sits exactly at the height of a peak of the same loudness, and zoom-in no longer under-reports loudness by one sample.
- Pasting a `~/...` path now correctly enables the Waveform button.
- The portable launcher (`run_gui.cmd`) now also installs Pillow and psutil on first run — previously a fresh install crashed the GUI silently on startup.
- Cancelling no longer occasionally leaves a zombie ffmpeg process running in the background.
- A stalled-ffmpeg kill is no longer misreported as an out-of-memory error.
- The final concat step produced broken timestamps on sources with shifted PTS; fixed by moving the timestamp-regeneration flag to the correct position.

## [0.2] - 2026-06-06

### Added

- **Audio-only silence detection with WAV cache** — first run extracts a 16kHz mono WAV and runs silence detection on it instead of decoding the full video. Subsequent runs skip the extract and re-run on the cached WAV. A 60s sample-verify on the source catches broken-timestamp videos and falls back to full re-detection. Significantly faster on long videos.
- **Per-video project directory (`per_video_dir`)** — opt-in (on by default) mode that groups all artifacts (downloaded source, audio cache, silence cache, compressed output, log file, temp dirs) into a single `{output_dir}/{stem}/` subdirectory. Local source files are never moved or copied.
- **Recent Projects panel (GUI)** — left info panel shows the 5 most recent project directories. Click the label to open in the system file manager, click `X` to confirm-and-delete the whole project dir. List persists across restarts; rows truncate long filenames with `…`.
- **User defaults (`user_defaults.json`)** — new "Save current as defaults" button writes current tunable settings to a separate file; "Restore defaults" now uses these instead of always reverting to factory defaults. Per-session state (output dir, recent projects, input path) is intentionally not saved.
- **Final summary on completion (GUI)** — status line, log block, and completion popup now show source/output size and duration in a `X -> Y` format. `?` is used for any unmeasurable field.
- **Status line `elapsed / remaining` and overall wall-clock timer (GUI)** — per phase the status line shows `elapsed / ETA`; the bottom row has a live `Elapsed: X | Remaining: Y` label that freezes at the final total when done.
- **`--per-video-dir` / `--no-per-video-dir` CLI flag** — override the YAML config's `per_video_dir` setting per-run.

### Changed

- **Default: `per_video_dir: True`** (was `False`) — most users want every video in its own folder.
- **Default: `threshold: -30.0`** (was `-60.0`) — more typical "speech" threshold, cuts clear pauses without eating quiet consonants or background hum.
- **Compact action row + slimmer window (GUI)** — Start/Cancel buttons narrowed, step label moved next to them, progress bar shrunk to ~17% of the row width, default window 1250 → 1080.
- **Restore Defaults now respects saved user values** — previously, "Restore Defaults" would override saved `force`, `per_video_dir`, and `delete_after` with hardcoded deselects; now it correctly reads from the current settings.

### Fixed

- **GUI was always re-running full silence detection** — the GUI didn't pass `output_dir` to `detect_silence()`, so the WAV cache was never used. Now the GUI benefits from the cache like the CLI does.
- **WAV cache invalidated on long silences** — silence segments crossing the 60s verify window no longer cause a false-positive cache invalidation.
- **"Step 2/3" dropped on fast transitions** — terminal status messages (`Complete!`, `Cancelled`, `Failed`, `Error`, and step transitions) now always display even when the pipeline finishes quickly.
- **Step label could be pushed off-screen** — the action row is now in a separate grid column with `weight=1` so the step label always gets the remaining width.
- **Log said "Phase X/3" while status said "Step X/3"** — both now use the same word.
- **Recent Projects: various fixes** — eager persist on add, file-existence pruning, click-to-open, confirmation before delete, filename truncation.
- **GUI status label hard cut-off** — long status strings (e.g. progress with elapsed/remaining) were silently truncated mid-word at 50 chars; now end with `…`.
- **Recent Projects only tracked with `per_video_dir=True`** — output directories now appear in the panel regardless of the checkbox state.
- **GUI crashes on a hand-edited `settings.json`** — wrong-typed values are now silently dropped instead of crashing the slider/entry later.

## [0.1.1] - 2026-06-04

### Fixed

- **Margin semantics** — `_apply_margin` docstring was backwards; positive margin now correctly shrinks silence (keeps more audio around phrases), negative expands it. Default changed from `-0.5` to `+0.5` so phrases are no longer clipped.
- **AAC encoder lookahead loss** — `segment` method added `-af apad` + extended `-t` by `_AUDIO_PAD = 0.1s` so AAC encoder can flush its lookahead buffer at segment boundaries; previously the last ~20-50ms of each segment's audio was being lost.
- **GUI slider precision** — `number_of_steps` now derived from range (`round((max - min) * 10)`), so sliders land on exact 0.1 increments instead of values like `-25.1` when user wants `-25`.
- **`on_entry_confirm` rounding** — entry field value is now rounded before being applied to slider and config, so entry/slider/config stay in sync.
- **Entry → config sync** — added `_sync_slider_entries()` called before pipeline start; previously, editing an entry field and clicking Start without pressing Enter would use the old value.
- **Status throttling** — terminal status messages (`Complete!`, `Cancelled`, `Failed`, `Error`) now use `force=True` to bypass 0.5s throttling; previously the final "Complete!" was dropped when the pipeline finished quickly.
- **Missing Step 2 in pipeline** — silence detection block was inadvertently removed during refactoring; restored.

### Added

- **`--delete-after` CLI flag** — parity with GUI `chk_delete` checkbox; deletes downloaded source after successful compression.
- **Resolved output path in GUI** — `lbl_output` label in info panel shows the absolute output directory after pipeline start, so user sees where the file will be saved even when the field is empty and `./compressed_videos` default is used.

### Changed

- **Defaults** — `method=segment`, `encoder=h264_mf`, `threshold=-60.0`, `min_silence=2.0` (was `batch` / `libx264` / `-20` / `1.0`). These are the softest possible values — almost no cutting by default.

## [0.1.0] - 2026-06-02

Initial MVP release.

- Download (yt-dlp) or local file input
- Silence detection via ffmpeg `silencedetect`
- Two cutting methods: `segment` (per-segment encode + concat demuxer) and `batch` (frame-exact select/aselect)
- Hardware encoder support: NVENC / AMF / MediaFoundation with auto-fallback to `libx264`
- CLI (Typer) and GUI (CustomTkinter) interfaces
- Silence cache (`{stem}_silence_cache.json`) with `(threshold, min_silence, margin)` keying
- Progress bar with ETA, cancellation support
