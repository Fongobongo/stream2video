# Changelog

## [Unreleased]

### Added
- **Waveform preview tab (GUI)** — a new "Waveform" tab in the right panel renders the audio waveform of the selected input file with detected silence regions overlaid as semi-transparent red bars and a time axis. Lets users tune `threshold` / `min_silence` / `margin` visually instead of running a full encode. The "Render preview" button extracts the WAV (if not cached) and runs silence detection on it with the current slider values; results are saved to the silence cache so the next real pipeline run is instant. Pillow is a new optional `[gui]` extra.
- **CI workflow** — GitHub Actions runs `ruff check`, `ruff format --check`, `mypy`, and `pytest` on push/PR to `main`. Uses `uv` for fast, reproducible installs (committed `uv.lock`).
- **24 unit tests for `stream2video.waveform`** — covers peak downsampling, silence pixel mapping, image rendering with overlays, theme colors, and edge cases (missing file, stereo mixing, empty WAV, subpixel silences).

## [0.2] - 2026-06-06

### Added
- **Audio-only silence detection with WAV cache** — first run extracts a 16kHz mono WAV and runs silence detection on it instead of decoding the full video. Subsequent runs skip the extract and re-run on the cached WAV. A 60s sample-verify on the source catches broken-timestamp videos and falls back to full re-detection. Significantly faster on long videos. The cached WAV is the same format the upcoming STT phase will need, so the extract step won't be repeated there.
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
