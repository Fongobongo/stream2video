# Changelog

## [Unreleased]

### Added

- **`--doctor` flag** — print an environment report (Python version, ffmpeg/ffprobe path and version, available encoders, RAM, config file location) and exit. Useful for filing bug reports or checking that everything is wired up before a long run. Works without an input file.
- **`--dry-run` / `-n` flag** — run only silence detection and print a "what would be cut" summary (number of segments, total length removed, expected output duration) without encoding. Lets you tune `threshold` / `min_silence` / `margin` against a real video in seconds instead of waiting for a full encode.
- **`--log-format json` flag** — emit every log line as a JSON object (one per line), for log aggregators like ELK / Splunk / Loki. The default `rich` format is unchanged.
- **`--proxy` CLI flag** — pass an HTTP or SOCKS5 proxy for downloads, matching the existing GUI proxy dialog. `http://127.0.0.1:8080` or `socks5://user:pass@host:1080`.
- **Shell completion** — `stream2video --install-completion` installs Bash / Zsh / Fish / PowerShell completion (powered by Typer). `--show-completion` prints the script for manual install.
- **Docker image** — `Dockerfile` plus a CLI-only entrypoint, so you can run the pipeline in a container without a local python/ffmpeg install.

### Fixed

- **`cut_then_encode` progress bar no longer jumps around** — the cut phase reports a monotonic percentage across segments (cumulative encoded duration instead of restarting from 0 per segment), and the final remux now reports real progress through ffmpeg's `-progress pipe:1` instead of a dead callback.
- **Output lock is never stolen from a live run** — a lock whose recorded pid is still alive is refused regardless of how old the lock file is (the mtime is never refreshed mid-run, so an old file just means a long run). Only a dead or pid-less lock is reclaimed.
- **Gapless tree join is no longer Windows-only** — the intermediate tree path triggers whenever the estimated per-call command line exceeds the budget, on any platform.
- **Log file no longer dies silently on a project-dir move failure** — the old file handler was closed but re-attached on rollback, which made `logging.handleError` swallow every subsequent record for the rest of the run. The handler is now reconstructed via `_make_file_handler` on the moved (or original) file.
- **`cut_then_encode` resume gate validates the audio stream** of the intermediate `raw_concat.mp4`, not just video — a crash mid-write could previously reuse a video-valid-but-audio-truncated file and produce a silent-video output.
- **Output lock now fires before any probe / encoder smoke-test**, so two concurrent runs (GUI + CLI) fail fast for the loser instead of each spawning ffprobe/ffmpeg first (`api.py`).
- **Downloaded file is per-run unique** (`%(id)s-%(epoch)s.%(ext)s`) — two pipelines pointed at the same URL no longer write into the same yt-dlp output file.
- **Bounded reaps after kills.** All `process.wait()` after `process.kill()` calls (concat runner, silencedetect, download timeout paths) now use a 30s ceiling; an unbounded `wait()` could hang the worker on a wedged Windows child.
- **Pipeline cancellations keep a completed source file.** A Ctrl+C after the download phase no longer unlinks a fully-fetched multi-GB VOD — only a genuinely-partial download is removed.
- **Stderr capture is ring-capped.** ffmpeg / yt-dlp spam from a corrupt source no longer grows into GBs between stall-checks (head+tail kept, middle dropped); the captured text is still enough for error classification.
- **Sample-verify passes the clip duration to the parser** — a trailing `silence_start` inside the 60s sample window is no longer dropped, eliminating a false-positive "mismatch → full re-detect" fallback.
- **GUI threads-safety fixes**:
  - Waveform live-overlay now uses the resolved path as the store key (previously a raw `./path` lookup always missed, so the overlay was dead).
  - Stale live-segments are dropped in a `finally:` on every pipeline exit path, not just success/cancel.
  - `messagebox` dialogs are given `parent=` so they can't hide behind the main window while blocking input.
  - Waveform-slider / pan epsilon-guard makes the CTkSlider readout ack no longer trigger redundant re-renders.
  - `_on_close`'s Quit dialog, file-info stat, rmtree of large project dirs, and the waveform preview's `cancel_process` no longer block the Tk main loop.
- **`--log-format json` no longer double-prints** — `install_json_handler` sets `logger.propagate = False` before the root-handler attach.
- **Docs:** `memory_reserve_mb` README corrected — the OS reserve is a warning floor mid-run (only the per-process RSS budget cancels); pre-flight still refuses a fresh heavy phase.
- **GUI: waveform rendering no longer freezes the main window** when the preview opens on a long video — decoding now runs fully off the Tk event loop.
- **Sample-verify on a re-extracted WAV** correctly clears the cached segments, so a re-detect after `--force` no longer reuses stale cut points.
- **`--doctor` honours `--config`** for its config-file path; previously the flag was silently ignored by the diagnostics entry point.
- **JSON mode keeps stdout line-per-JSON** — a stray banner or progress bar no longer breaks piping to `jq` / log aggregators.
- **Gapless tree join** no longer blows up disk on long multi-segment outputs — intermediates now use a near-lossless libx264 pass instead of uncompressed ffv1, and the run refuses to start when free disk space is below the projected intermediate size.
- **Subprocess hygiene**: registered processes are now reliably killed on GUI crash and on close-during-running; cancel no longer leaves a zombie ffmpeg holding the output file open (which used to trip `WinError 32` on the next cleanup).
- **Dockerfile**: the test stage now copies `tests/` (previously `pytest -q` ran zero tests), stages are named (`test` / `cli`) so `docker build .` builds the documented default, and the comment about the `[gui]` extra matches reality (`[dev]` does pull it; headless import still skips cleanly).
- **Doc strings / error messages aligned with behaviour**: gapless tree-intermediate docstring corrected (libx264 CRF 18 + PCM, not ffv1), `raw_concat.mkv` → `raw_concat.mp4` in the changelog, and the `_audio_opts` unknown-quality error lists the same `source/high/medium/low` set as `_audio_bitrate`.

## [0.3] - 2026-08-01

### Added

- **New cutting method `cut_then_encode`** — the best-quality mode: silenced parts are removed losslessly (stream copy, keyframe-aligned) and the remaining pieces are encoded in a single pass at the end. One encoding generation, so the picture stays closest to the source.
- **Quality presets** — pick how the output is encoded without editing bitrates by hand:

  - `--video-quality` and `--audio-quality`: `high` / `medium` / `low`, plus `source` to keep the encoder's own defaults (the new default).
  - `--download-quality`: `best` / `1080p` / `720p` / `480p` / `360p` to cap the resolution fetched from YouTube/Twitch.

  All of the above are also in the GUI as dropdowns.
- **Audio-only output** — `--output-format mp3|opus|aac|wav|flac` drops the video track and produces a standalone audio file (silence still removed). The output gets the matching file extension automatically.
- **Gapless audio by default** — re-encodes audio in the final join pass so the tiny per-segment gap no longer accumulates as A/V drift on multi-segment outputs. Turn off with `--no-gapless-concat` if you prefer the faster stream-copy join. For lossless video + gapless audio in one pass, use `cut_then_encode` instead.
- **Output FPS policy** — `--output-fps source` (default) preserves the input's frame rate without duplication; `24` / `25` / `30` / `50` / `60` force a constant frame rate when the source cadence causes playback issues.
- **Resource presets (`--preset`)** — one switch for low-end or busy machines: `low_memory` (4–8 GB RAM, keeps the system responsive), `low_cpu` (background encode, caps thread count), `balanced` (default), `maximum_performance` (fastest if you have RAM to spare). Any explicit flag still overrides the preset on that key.
- **Memory protection** — a background watchdog (requires the optional `psutil` dependency) cancels a runaway encode before the machine starts swapping, and the run refuses to start a heavy phase when free RAM is already below the safe reserve (`--memory-limit-mb`, `--memory-reserve-mb`). When ffmpeg is killed by the OS for running out of memory, the error now says so in plain words with tips on which settings to lower. On Linux/macOS, `--rlimit-as-mb` can additionally hard-cap ffmpeg's memory at the kernel level.
- **Download health monitoring** — three timeouts catch a stuck download early: no first byte within 5 minutes, no progress for 30 minutes mid-stream, or 8 hours total (`--connect-timeout`, `--no-progress-timeout`, `--download-timeout`, all configurable).
- **Download progress display** — both CLI and GUI now show live percent, size, speed, and ETA while downloading instead of a bare "Downloading video..." line.
- **Waveform preview overhaul** — the preview shows peaks immediately and overlays detected silence regions on top as they are found, supports cursor-anchored zoom + horizontal pan, a time/dB tooltip under the cursor, and a dry-run detection when no cache exists yet. Opening it after a run reuses the saved cache instead of re-detecting.
- **Resume after interruption** — silence detection checkpoints its progress roughly every 30 seconds, so a cancelled or crashed run continues from the last checkpoint instead of starting over. Partially encoded video segments are verified before reuse, and any artifacts left from a run with different settings are discarded automatically.
- **Completion sound (GUI)** — optional short chime when the pipeline finishes, with a different tone on cancel/failure ("Sound when done" checkbox, on by default). Works on Windows and macOS out of the box; on Linux uses whatever player is installed (`paplay`, `aplay`, or `ffplay`).
- **Finer encode control** — `--x264-preset` (speed/size trade-off for CPU encoding), `--encoder-threads` (cap CPU usage), `--x264-low-memory` (less RAM, slightly larger files), and `--low-process-priority` (ffmpeg stays in the background so the PC stays usable while you encode).
- **Encoder fallback policy** — when the selected GPU encoder is missing or crashes, the run now asks before switching to slow CPU encoding (`--software-fallback ask`, the new default); `enabled` restores the old automatic behaviour, `disabled` fails fast.
- **Smarter progress bar** — the GUI progress bar adapts its scale per run (a local file with cached silence detection jumps straight to encoding) and shows an explicit percentage next to the bar.
- **Benchmark script** — `scripts/benchmark_presets.py` compares x264 speed presets on a synthetic clip so you can pick the fastest acceptable one for your hardware.
- **Safer cancellation** — Cancel in the GUI (and Ctrl+C in the CLI) now reliably kills the running ffmpeg/yt-dlp process, including cases where it was stuck writing output.

### Changed

- **Frame-accurate cutting** — both `segment` and `batch` methods were reworked so cuts land exactly on the requested timestamps. Previously each non-initial segment could lose ~0.5 seconds, and the `batch` method inserted a 1-second freeze where silence was removed. The batch method also no longer re-decodes the file from the start for every chunk — a 6-hour stream with 100 chunks used to decode 600 hours of video, now it decodes 6.
- **Honest source bitrate on hardware encoders** — when `video_quality=source` is set, the actual source bitrate is probed via ffprobe and used as the encoder target; previously a hardcoded fallback was silently applied.
- **Better default downloads** — `best` download quality now prefers separate best video + best audio tracks (often 1080p where the old logic grabbed a pre-merged 720p).
- **No more audio downgrade** — audio is encoded at the quality you pick; a 256k+ source is no longer silently compressed to a lower bitrate.
- **No more accumulated drift** — the 100 ms per-segment audio padding that added up to seconds of drift on very long videos is gone.
- **GUI layout** — the main window defaults to 1280×720 and fits all controls without scrolling; input/output and quality pickers are tightened into two columns.
- **GUI presets behave as expected** — switching the resource preset back to `balanced` actually restores the default tunables, and the selected preset is remembered across restarts.
- **Python 3.13 is now the baseline.** Windows ARM64 is still supported; CI runs the full test suite on both Ubuntu and Windows.

### Fixed

- Downloaded-file detection no longer misfires when yt-dlp prints extra lines after the download — the run no longer reports "file not found" despite a successful download.
- A slow video-host page no longer trips the "stalled before first byte" watchdog — the timer only fires when yt-dlp is truly silent.
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
- The final concat step no longer produces broken timestamps on sources with shifted PTS; the timestamp-regeneration flag now sits in the correct position.

## [0.2] - 2026-06-06

### Added

- **Audio-only silence detection with WAV cache** — first run extracts a 16kHz mono WAV and runs silence detection on it instead of decoding the full video. Subsequent runs skip the extract and re-run on the cached WAV. A 60-second sample verify on the source catches broken-timestamp videos and falls back to full re-detection. Significantly faster on long videos.
- **Per-video project directory (`per_video_dir`)** — opt-in (on by default) mode that groups all artifacts (downloaded source, audio cache, silence cache, compressed output, log file, temp dirs) into a single `{output_dir}/{stem}/` subdirectory. Local source files are never moved or copied.
- **Recent Projects panel (GUI)** — left info panel shows the 5 most recent project directories. Click the label to open in the system file manager, click `X` to confirm-and-delete the whole project dir. List persists across restarts; rows truncate long filenames with `…`.
- **User defaults (`user_defaults.json`)** — new "Save current as defaults" button writes current tunable settings to a separate file; "Restore defaults" now uses these instead of always reverting to factory defaults. Per-session state (output dir, recent projects, input path) is intentionally not saved.
- **Final summary on completion (GUI)** — status line, log block, and completion popup now show source/output size and duration in a `X -> Y` format. `?` is used for any unmeasurable field.
- **Status line `elapsed / remaining` and overall wall-clock timer (GUI)** — per phase the status line shows `elapsed / ETA`; the bottom row has a live `Elapsed: X | Remaining: Y` label that freezes at the final total when done.
- **`--per-video-dir` / `--no-per-video-dir` CLI flag** — override the YAML config's `per_video_dir` setting per run.

### Changed

- **Default: `per_video_dir: True`** (was `False`) — most users want every video in its own folder.
- **Default: `threshold: -30.0`** (was `-60.0`) — more typical "speech" threshold, cuts clear pauses without eating quiet consonants or background hum.
- **Compact action row + slimmer window (GUI)** — Start/Cancel buttons narrowed, step label moved next to them, progress bar shrunk, default window 1250 → 1080.
- **Restore Defaults now respects saved user values** — previously, "Restore Defaults" would override saved `force`, `per_video_dir`, and `delete_after` with hardcoded deselects; now it correctly reads from the current settings.

### Fixed

- **GUI was always re-running full silence detection** — the GUI didn't pass `output_dir` to `detect_silence()`, so the WAV cache was never used. Now the GUI benefits from the cache like the CLI does.
- **WAV cache invalidated on long silences** — silence segments crossing the 60-second verify window no longer cause a false-positive cache invalidation.
- **"Step 2/3" dropped on fast transitions** — terminal status messages (`Complete!`, `Cancelled`, `Failed`, `Error`, and step transitions) now always display even when the pipeline finishes quickly.
- **Step label could be pushed off-screen** — the action row is now in a separate grid column with `weight=1` so the step label always gets the remaining width.
- **Log said "Phase X/3" while status said "Step X/3"** — both now use the same word.
- **Recent Projects: various fixes** — eager persist on add, file-existence pruning, click-to-open, confirmation before delete, filename truncation.
- **GUI status label hard cut-off** — long status strings (e.g. progress with elapsed/remaining) were silently truncated mid-word at 50 chars; now end with `…`.
- **Recent Projects only tracked with `per_video_dir=True`** — output directories now appear in the panel regardless of the checkbox state.
- **GUI crashes on a hand-edited `settings.json`** — wrong-typed values are now silently dropped instead of crashing the slider/entry later.

## [0.1.1] - 2026-06-04

### Added

- **`--delete-after` CLI flag** — parity with the GUI "delete source" checkbox; deletes the downloaded source after successful compression.
- **Resolved output path in GUI** — the info-panel label shows the absolute output directory after pipeline start, so you see where the file will be saved even when the field is empty and the `./processed_videos` default is used.

### Fixed

- **Margin semantics** — positive margin now correctly shrinks silence (keeps more audio around phrases), negative expands it. Default changed from `-0.5` to `+0.5` so phrases are no longer clipped.
- **AAC encoder lookahead loss** — the `segment` method now pads each segment's duration slightly so the AAC encoder can flush its lookahead buffer at boundaries; previously the last ~20–50 ms of each segment's audio was being lost.
- **GUI slider precision** — sliders now land on exact 0.1 increments instead of values like `-25.1` when you want `-25`.
- **Entry → config sync** — editing an entry field and clicking Start without pressing Enter now uses the typed value, not the old one.
- **Status throttling** — terminal status messages (`Complete!`, `Cancelled`, `Failed`, `Error`) now bypass 0.5-second throttling so the final "Complete!" is never dropped on a fast run.
- **Missing Step 2 in pipeline** — silence detection block was inadvertently removed during a refactoring; restored.

### Changed

- **Defaults** — `method=segment`, `encoder=h264_mf`, `threshold=-60.0`, `min_silence=2.0` (was `batch` / `libx264` / `-20` / `1.0`). These are the softest possible values — almost no cutting by default.

## [0.1.0] - 2026-06-02

Initial MVP release.

- Download via `yt-dlp` or local file input
- Silence detection via ffmpeg `silencedetect`
- Two cutting methods: `segment` (per-segment encode + concat demuxer) and `batch` (frame-exact select/aselect)
- Hardware encoder support: NVENC / AMF / Media Foundation, with auto-fallback to `libx264`
- CLI (Typer) and GUI (CustomTkinter) interfaces
- Silence cache (`{stem}_silence_cache.json`) keyed by `(threshold, min_silence, margin)`
- Progress bar with ETA, cancellation support
