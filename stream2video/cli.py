"""CLI entry point using Typer."""

from __future__ import annotations

import shutil
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer
from rich.progress import (
    BarColumn,
    Progress,
    TaskID,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

from stream2video.cli_config import load_config as _load_config_impl
from stream2video.cli_helpers import (
    LoggingSessionBusyError,
    _check_ffmpeg,
    _console_handler,
    _make_file_handler,
    _make_sigint_cancel,
    app,
    console,
    logger,
    logging_session,
)
from stream2video.cli_resolver import is_from_cli, make_resolver
from stream2video.completion_sound import play_completion_sound
from stream2video.config import (
    CONFIG_DEFAULTS,
    DEFAULT_PRESET,
    apply_preset,
)
from stream2video.download import (
    DiskSpaceError,
    DownloadProgress,
    DownloadTimeoutError,
    FileBusyError,
    PermissionDeniedError,
    URLValidationError,
    VideoNotAvailableError,
)
from stream2video.formatters import fmt_completion_summary, fmt_dry_run_summary
from stream2video.gui_helpers import build_download_status
from stream2video.pipeline_controller import (
    PipelineCallbacks,
    PipelineCancelled,
    PipelineConcatError,
    PipelineController,
    PipelineDownloadError,
    PipelineSilenceError,
    PipelineUnexpectedError,
    build_pipeline_config,
)
from stream2video.tools import run_with_retry

# Module-level flag toggled by --log-format json. When True the human-
# readable banner and Rich progress bars are suppressed so the stdout
# stream stays line-per-JSON-record (piping to ``jq`` or an aggregator
# like ELK is unaffected by decorative output).
_JSON_LOG_MODE: bool = False

# The two console log formats --log-format accepts. The ONLY spelling
# rule for them lives in :func:`normalize_log_format` (case-insensitive)
# so the main() validator and the eager --doctor argv scan can never
# disagree about what "json" is spelled like (audit round 12, P2: the
# doctor scan used a case-sensitive ``== "json"`` while main()
# lowercased, so ``--log-format JSON`` behaved differently on the two
# paths).
_VALID_LOG_FORMATS = ("rich", "json")


def normalize_log_format(value: str) -> str | None:
    """Return the canonical lowercase log format for ``value`` (case-
    insensitive, whitespace-tolerant), or ``None`` when unrecognized.

    Shared by the ``--log-format`` validator in :func:`main` and the
    eager ``--doctor`` argv scan in :func:`_doctor_callback` — one value
    has one spelling rule on every surface.
    """
    v = value.strip().lower()
    return v if v in _VALID_LOG_FORMATS else None


def load_config(config_file: Path | None) -> dict:
    """Thin wrapper: ``cli_config.load_config`` + the module-level ``console``.

    Kept as a thin adapter so ``from stream2video.cli import load_config``
    continues to work, and so a test patching ``stream2video.cli.console``
    would see the output policy the test configured (the helper has no
    console of its own).
    """
    return _load_config_impl(config_file, console)


def _doctor_callback(ctx: typer.Context, param: Any, value: bool) -> bool:
    """Eager callback for ``--doctor``: run diagnostics + exit before the
    positional-arg validator fires, so ``stream2video --doctor`` works
    without an input path.
    """
    if value:
        global _JSON_LOG_MODE
        # ``--doctor`` is eager: it may fire before the *non-eager*
        # ``--config`` option has been parsed, so ``ctx.params`` does not
        # reliably contain ``config_file``. Scan argv directly — cheap,
        # order-agnostic, and covers both ``--config X`` / ``-c X`` and
        # the ``--config=X`` / ``-c=X`` spellings.
        cfg: Path | None = None
        argv = sys.argv[1:]
        # ``--`` ends option parsing (everything after is positional):
        # never treat a post-``--`` token as a flag value.
        argv = argv[: argv.index("--")] if "--" in argv else argv
        for i, arg in enumerate(argv):
            if arg in ("--config", "-c"):
                # The value must actually be the next token AND not look
                # like another flag (``-c --doctor`` would otherwise
                # become Path("--doctor")).
                if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                    cfg = Path(argv[i + 1])
                break
            if arg.startswith("--config="):
                cfg = Path(arg.split("=", 1)[1])
                break
            if arg.startswith("-c="):
                cfg = Path(arg.split("=", 1)[1])
                break
        # ``--log-format json`` is also non-eager, so it hasn't populated
        # _JSON_LOG_MODE yet when --doctor fires. Scan argv again (same
        # rationale as --config above) so the doctor's "line-per-object"
        # contract actually fires. Spelling check goes through the shared
        # :func:`normalize_log_format` — the same case-insensitive rule
        # main()'s validator applies, so ``JSON`` behaves identically on
        # both paths (audit P2).
        for i, arg in enumerate(argv):
            if (
                arg == "--log-format"
                and i + 1 < len(argv)
                and normalize_log_format(argv[i + 1]) == "json"
            ):
                _JSON_LOG_MODE = True
                break
            if (
                arg.startswith("--log-format=")
                and normalize_log_format(arg.split("=", 1)[1]) == "json"
            ):
                _JSON_LOG_MODE = True
                break
        # The eager callback exits BEFORE main() — its ``finally``
        # logging restore never runs, so any state mutated above (the
        # ``--log-format json`` scan sets _JSON_LOG_MODE) must be reset
        # before returning or it leaks into the next main() call in the
        # same process (audit P1/P2). try/finally wraps _run_doctor so
        # an exception inside the diagnostics still resets the flag
        # before propagating.
        try:
            ok = _run_doctor(cfg)
        finally:
            _JSON_LOG_MODE = False
        raise typer.Exit(0 if ok else 1)
    return value


def _run_doctor(config_file: Path | None = None) -> bool:
    """Print environment diagnostics; return True iff all critical checks pass.

    Reports system information (Python, ffmpeg, encoders, RAM, config)
    in a compact OK/fail list. Callers exit 0 when all critical checks
    pass, 1 otherwise. Non-critical items (psutil, YAML config file) only
    print a warning — the CLI still works without them.
    """
    from rich.table import Table

    from stream2video.config import _base_dir, settings_path, user_defaults_path

    all_critical_ok = True
    tbl = Table(show_header=False, box=None, padding=(0, 1))
    tbl.add_column("status", width=4)
    tbl.add_column("check")
    # Structured mirror of every check row so --log-format=json can emit
    # line-per-object records without scraping Rich cells (fragile across
    # Rich versions). Each entry: (status, plain-label).
    checks: list[tuple[str, str]] = []

    def _row(status_glyph: str, status: str, rich_label: str, plain_label: str) -> None:
        tbl.add_row(status_glyph, rich_label)
        checks.append((status, plain_label))

    # Redirected stdout on Windows decodes via the OEM/ANSI codepage
    # (cp1251/cp866), which can't encode the ✓/✗/— glyphs below. Rich
    # only enables its legacy Windows console API when the stream is a
    # real console; for pipes/files, reconfigure stdout/stderr to UTF-8
    # so ``--doctor > report.txt`` doesn't die with UnicodeEncodeError.
    for _stream in (sys.stdout, sys.stderr):
        _reconfigure = getattr(_stream, "reconfigure", None)
        if _reconfigure is None:
            continue  # e.g. pytest's captured stream
        try:
            _reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

    # Python version
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    ok = sys.version_info >= (3, 13)
    _row(
        "[green]✓[/green]" if ok else "[red]✗[/red]",
        "ok" if ok else "fail",
        f"Python {py_ver}" + ("" if ok else "  [red](3.13+ required)[/red]"),
        f"Python {py_ver}" + ("" if ok else " (3.13+ required)"),
    )
    if not ok:
        all_critical_ok = False

    # ffmpeg + ffprobe presence
    for tool in ("ffmpeg", "ffprobe"):
        exe = shutil.which(tool)
        if exe:
            try:
                # run_with_retry (audit round 15 P2): a transient
                # FileNotFoundError from a WinGet shim replacement / AV
                # filter must not turn the doctor's version line into
                # "(version unknown)" — retry with re-resolution first.
                out = (
                    run_with_retry(
                        [exe, "-version"], capture_output=True, text=True, timeout=5, check=False
                    )
                    .stdout.splitlines()[0]
                    .strip()
                )
                _row("[green]✓[/green]", "ok", f"{tool}: {out} ({exe})", f"{tool}: {out} ({exe})")
            except Exception as e:
                _row(
                    "[green]✓[/green]",
                    "ok",
                    f"{tool}: found at {exe} [dim](version unknown: {e})[/dim]",
                    f"{tool}: found at {exe} (version unknown: {e})",
                )
        else:
            _row("[red]✗[/red]", "fail", f"{tool}: not found in PATH", f"{tool}: not found in PATH")
            all_critical_ok = False

    # GPU encoder availability (smoke test via the existing check_encoder).
    # Each check spawns a 1-frame lavfi encode — fast (< 2s each) and
    # authoritative.
    from stream2video.concat.encoders import check_encoder

    enc_rows: list[str] = []
    for enc_name in ("h264_nvenc", "h264_amf", "h264_mf"):
        enc_rows.append(f"{enc_name}={'ok' if check_encoder(enc_name) else 'no'}")
    _row(
        "[green]✓[/green]",
        "ok",
        f"GPU encoders: {', '.join(enc_rows)}",
        f"GPU encoders: {', '.join(enc_rows)}",
    )

    # RAM
    try:
        from stream2video.memory import _HAS_PSUTIL, _available_ram_mb

        if _HAS_PSUTIL:
            import psutil

            vm = psutil.virtual_memory()
            total_mb = vm.total / (1024 * 1024)
            avail_mb = _available_ram_mb()
            budget_mb = int(total_mb * 0.60)  # 60% default auto policy
            avail_s = f"{avail_mb:.0f} MB" if avail_mb is not None else "?"
            _row(
                "[green]✓[/green]",
                "ok",
                f"RAM: {total_mb / 1024:.0f} GB (60% budget = {budget_mb / 1024:.1f} GB, "
                f"available now: {avail_s})",
                f"RAM: {total_mb / 1024:.0f} GB (60% budget = {budget_mb / 1024:.1f} GB, "
                f"available now: {avail_s})",
            )
        else:
            _row(
                "[yellow]![/yellow]",
                "warn",
                "RAM: psutil not installed (no memory guardrail; install with `pip install stream2video[monitor]`)",
                "RAM: psutil not installed (no memory guardrail; install with `pip install stream2video[monitor]`)",
            )
    except Exception as e:
        _row(
            "[yellow]![/yellow]",
            "warn",
            f"RAM: could not query ({e})",
            f"RAM: could not query ({e})",
        )

    # Config file (YAML or user_defaults.json)
    if config_file is not None:
        # User explicitly passed --config: report whether it loaded.
        if config_file.exists():
            _row(
                "[green]✓[/green]",
                "ok",
                f"Config file: {config_file}",
                f"Config file: {config_file}",
            )
        else:
            _row(
                "[yellow]![/yellow]",
                "warn",
                f"Config file: {config_file} [yellow](not found — using defaults)[/yellow]",
                f"Config file: {config_file} (not found — using defaults)",
            )
    user_cfg = user_defaults_path()
    if user_cfg.exists():
        _row("[green]✓[/green]", "ok", f"User defaults: {user_cfg}", f"User defaults: {user_cfg}")
    else:
        _row(
            "[dim]i[/dim]",
            "info",
            f"User defaults: {user_cfg} [dim](none — defaults used)[/dim]",
            f"User defaults: {user_cfg} (none — defaults used)",
        )

    # Output dir (settings file location — informational)
    _row("[dim]i[/dim]", "info", f"Settings: {settings_path()}", f"Settings: {settings_path()}")
    _row("[dim]i[/dim]", "info", f"Base dir: {_base_dir()}", f"Base dir: {_base_dir()}")

    if _JSON_LOG_MODE:
        # JSON mode: line-per-object output so a downstream consumer
        # doesn't have to parse Rich markup/ANSI. Each check is one
        # record; the final record carries the overall verdict.
        import json as _json

        print(_json.dumps({"doctor": "begin"}, ensure_ascii=False))
        for status, label in checks:
            print(
                _json.dumps(
                    {"doctor": "check", "status": status, "check": label}, ensure_ascii=False
                )
            )
        print(_json.dumps({"doctor": "end", "ok": all_critical_ok}, ensure_ascii=False))
        return all_critical_ok

    console.print("\n[bold cyan]stream2video doctor[/bold cyan]\n")
    console.print(tbl)
    if not all_critical_ok:
        console.print("\n[red]Some critical checks failed — see above[/red]")
    return all_critical_ok


@app.command()
def main(
    ctx: typer.Context,
    input_video: str = typer.Argument(
        ...,  # required positionally; --doctor / --completion bypass via is_eager
        help="URL or path to input video",
    ),
    output_dir: Path = typer.Option(
        Path("./processed_videos"),
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
        "--force/--no-force",
        "-f",
        help="Re-detect silence, ignore cache. If not passed, falls back to "
        "the config file's `force` key (default False); --no-force pins it "
        "off so a config file with `force: true` can be overridden.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        "-n",
        help="Show what would be cut (silence detection + statistics) without "
        "encoding. Exits after phase 2. Useful for tuning threshold/min_silence "
        "before a long encode.",
    ),
    method: str = typer.Option(
        CONFIG_DEFAULTS["method"],
        "--method",
        "-m",
        help="Concat method: 'segment' (fast, ~1.5h), 'batch' (select/aselect filter, ~6-7h), "
        "or 'cut_then_encode' (best quality, one encode pass after lossless cut). "
        "If not passed, the config file's `method` key is used.",
    ),
    encoder: str = typer.Option(
        CONFIG_DEFAULTS["encoder"],
        "--encoder",
        "-e",
        help="Video encoder: 'h264_nvenc' (NVIDIA), 'h264_amf' (AMD), 'h264_mf' "
        "(Media Foundation, default), or 'libx264' (CPU fallback). If not passed, "
        "the config file's `encoder` key is used.",
    ),
    video_quality: str = typer.Option(
        CONFIG_DEFAULTS["video_quality"],
        "--video-quality",
        "-vq",
        help="Encode quality preset: 'source' (encoder defaults, default), 'high' (10000k / CRF 18), "
        "'medium' (7000k / CRF 23), or 'low' (3500k / CRF 28). If not passed, "
        "the config file's `video_quality` key is used.",
    ),
    audio_quality: str = typer.Option(
        CONFIG_DEFAULTS["audio_quality"],
        "--audio-quality",
        "-aq",
        help="Audio (AAC) quality preset: 'source' (codec defaults + native rate/channels, default), "
        "'high' (256k), 'medium' (192k), or 'low' (128k). If not passed, "
        "the config file's `audio_quality` key is used.",
    ),
    download_quality: str = typer.Option(
        CONFIG_DEFAULTS["download_quality"],
        "--download-quality",
        "-dq",
        help="Download quality preset (Twitch/YouTube, ignored for local files): "
        "'best' (default), '1080p', '720p', '480p', '360p'. If not passed, the "
        "config file's `download_quality` key is used.",
    ),
    software_fallback: str = typer.Option(
        CONFIG_DEFAULTS["software_fallback"],
        "--software-fallback",
        help="What to do when the requested hardware encoder is unavailable "
        "or fails mid-run: 'ask' (default — refuse silent fallback to libx264; "
        "the run fails with a clear error), 'disabled' (fail immediately), or "
        "'enabled' (silently retry with libx264, legacy behaviour). If not "
        "passed, the config file's `software_fallback` key is used.",
    ),
    x264_preset: str = typer.Option(
        CONFIG_DEFAULTS["x264_preset"],
        "--x264-preset",
        help="libx264 preset (ultrafast..slow, default 'medium'). Faster presets "
        "reduce CPU load at the cost of file size / quality. Use 'ultrafast' or "
        "'veryfast' on an unstable / overclocked CPU. If not passed, the config "
        "file's `x264_preset` key is used.",
    ),
    encoder_threads: str = typer.Option(
        CONFIG_DEFAULTS["encoder_threads"],
        "--encoder-threads",
        help="Encoder thread count: 'auto' (default — let ffmpeg pick, usually "
        "one per logical core) or a positive int to cap libx264's thread pool. "
        "Lowering this reduces peak CPU at the cost of slower encode. If not "
        "passed, the config file's `encoder_threads` key is used.",
    ),
    x264_low_memory: bool | None = typer.Option(
        None,
        "--x264-low-memory/--no-x264-low-memory",
        help="Reduce x264's frame-buffer footprint via rc-lookahead=10, ref=1, "
        "bframes=0. Produces slightly larger files but uses significantly less "
        "RAM during encode. Useful on memory-constrained machines (4-8 GB). If "
        "not passed, falls back to the config file's `x264_low_memory` key "
        "(default False); --no-x264-low-memory pins it off so a config file "
        "with `x264_low_memory: true` can be overridden.",
    ),
    use_crf: bool | None = typer.Option(
        None,
        "--use-crf/--no-use-crf",
        help="Use quality-fixed encoding instead of bitrate-fixed "
        "(-b:v source/10000k/7000k/3500k). libx264 uses CRF, NVENC/AMF use "
        "CQ/QP-style modes, and MF uses quality mode. File size varies by "
        "content and encoder. Default off (bitrate parity between encoders). "
        "If not passed, falls back to the config file's `use_crf` key "
        "(default False); --no-use-crf pins it off so a config file with "
        "`use_crf: true` can be overridden.",
    ),
    gapless_concat: bool | None = typer.Option(
        None,
        "--gapless-concat/--no-gapless-concat",
        help="Re-encode audio in the final concat pass so per-segment AAC "
        "priming (~21ms per segment) doesn't accumulate as A/V drift on "
        "multi-segment outputs. Default on (follows gapless_concat in the "
        "config file, which defaults to true). Video is stream-copied; only "
        "audio is re-encoded. Use --no-gapless-concat for the faster "
        "concat-demuxer (stream copy) join at the cost of per-segment drift "
        "on very long multi-segment outputs. For lossless video + gapless "
        "audio in a single pass use --method cut_then_encode instead.",
    ),
    low_process_priority: bool | None = typer.Option(
        None,
        "--low-process-priority/--no-low-process-priority",
        help="Spawn ffmpeg at a lower scheduling priority so a long-running "
        "encode doesn't starve interactive applications. On Windows: "
        "BELOW_NORMAL_PRIORITY_CLASS; on Linux/macOS: nice +10. Useful for "
        "unattended batch processing on shared/desktop machines. Default "
        "off (normal priority, faster encoding). If not passed, falls back "
        "to the config file's `low_process_priority` key (default False); "
        "--no-low-process-priority pins it off so a config file with "
        "`low_process_priority: true` can be overridden.",
    ),
    rlimit_as_mb: int = typer.Option(
        0,
        "--rlimit-as-mb",
        help=(
            "POSIX-only. When >0, cap each spawned ffmpeg subprocess's "
            "virtual address space to this many MiB via RLIMIT_AS in "
            "preexec_fn. malloc/mmap return ENOMEM (and ffmpeg bails) "
            "before the OS swaps or the Linux OOM killer kicks in -- a "
            "hard, kernel-enforced complement to the in-process "
            "--memory-limit-mb pre-flight check (which only samples "
            "RSS between wall-clock polls and can miss a fast spike). "
            "On Windows this flag is ignored (no portable equivalent "
            "of RLIMIT_AS). Default 0 disables the cap."
        ),
    ),
    preset: str = typer.Option(
        DEFAULT_PRESET,
        "--preset",
        help=(
            "Resource preset — bundle of tunables (x264_low_memory, "
            "memory_limit_mb, batch_chunk_size, low_process_priority, "
            "x264_preset, encoder_threads). 'low_memory' trades speed for "
            "stability on 4-8 GB machines (x264_low_memory=True, "
            "batch_chunk_size=20, low_process_priority=True). 'low_cpu' "
            "minimizes CPU usage for background encoding "
            "(x264_preset=ultrafast, encoder_threads=2, x264_low_memory=True, "
            "low_process_priority=True). 'balanced' (default) reproduces "
            "the historical defaults. 'maximum_performance' trades RAM "
            "for maximum throughput (x264_low_memory=False, memory_limit_mb=0, "
            "batch_chunk_size=80). The preset is applied first, then any "
            "explicit --flag overrides win — so `--preset low_memory "
            "--no-low-process-priority` keeps low_memory's other "
            "tunables but flips low_process_priority back off."
        ),
    ),
    delete_after: bool | None = typer.Option(
        None,
        "--delete-after/--no-delete-after",
        help="Delete downloaded source file after successful compression. If not "
        "passed, falls back to the config file's `delete_after` key (default "
        "False); --no-delete-after pins it off so a config file with "
        "`delete_after: true` can be overridden.",
    ),
    per_video_dir: bool | None = typer.Option(
        None,
        "--per-video-dir/--no-per-video-dir",
        help="Group all artifacts (source, WAV cache, JSON cache, output, log) "
        "into a per-video subdirectory. Default follows config/per_video_dir.",
    ),
    completion_sound: bool | None = typer.Option(
        None,
        "--completion-sound/--no-completion-sound",
        help="Play the completion chime after a successful run (matches the "
        "GUI's 'Sound when done' checkbox). If not passed, falls back to the "
        "config file's `completion_sound` key (default True).",
    ),
    doctor: bool = typer.Option(
        False,
        "--doctor",
        # Eager callback runs the diagnostics and exits before Typer
        # validates the positional INPUT_VIDEO argument, so
        # ``stream2video --doctor`` works without a URL/path.
        is_eager=True,
        callback=_doctor_callback,
        help="Print environment diagnostics (Python version, ffmpeg/encoders, "
        "RAM, config) and exit.",
    ),
    log_level: str = typer.Option(
        "INFO",
        "--log-level",
        "-l",
        help="Logging level (DEBUG, INFO, WARNING, ERROR)",
    ),
    log_format: str = typer.Option(
        "rich",
        "--log-format",
        help="Console log format: 'rich' (default — human-readable markup), "
        "'json' (one JSON object per line, for log aggregation: ELK/Splunk/Loki).",
    ),
    download_timeout: int = typer.Option(
        CONFIG_DEFAULTS["download_timeout"],
        "--download-timeout",
        help="Absolute ceiling for the whole download in seconds (default 28800 = 8h, "
        "sized for big VODs). Lower for quick test runs; raise for very large "
        "streams. Ignored for local files.",
    ),
    connect_timeout: int = typer.Option(
        CONFIG_DEFAULTS["connect_timeout"],
        "--connect-timeout",
        help="Seconds to wait for the first progress event (DNS+TLS+handshake+first "
        "byte) before killing yt-dlp with a clear timeout error. Default 300s "
        "(5 min). Increase on very slow / satellite links.",
    ),
    no_progress_timeout: int = typer.Option(
        CONFIG_DEFAULTS["no_progress_timeout"],
        "--no-progress-timeout",
        help="Seconds of silence mid-download before killing yt-dlp (stalled "
        "connection watchdog). Default 1800s (30 min). Increase for very "
        "slow / unstable links where mid-download pauses are normal.",
    ),
    proxy: str = typer.Option(
        CONFIG_DEFAULTS["proxy"],
        "--proxy",
        help="Proxy server to use for downloads, e.g. http://127.0.0.1:8080 "
        "or socks5://user:pass@host:1080. Empty (default) = direct "
        "connection. Passed to yt-dlp.",
    ),
    proxy_active: bool | None = typer.Option(
        None,
        "--proxy-active/--no-proxy-active",
        help="Gate for the proxy address stored in user_defaults.json / config "
        "YAML. Passing it explicitly pins the proxy on (with the --proxy URL) or "
        "off, overriding any proxy_active: true in the config files. If not "
        "passed, the config file's `proxy_active` key (default False) decides. "
        "Copied GUI commands use --no-proxy-active when the GUI has the proxy "
        "switched off, so a paste can't silently re-enable a stored proxy.",
    ),
    memory_limit_mb: str = typer.Option(
        str(CONFIG_DEFAULTS["memory_limit_mb"]),
        "--memory-limit-mb",
        help="RAM budget for the encode pipeline: 'auto' (60%% of total RAM, "
        "default) or a positive MB value. Exceeding 95%% of the budget cancels "
        "the running ffmpeg (only ffmpeg's own RSS, not other apps' pressure). "
        "0 disables the budget check. If not passed, the config file's "
        "`memory_limit_mb` key is used.",
    ),
    memory_reserve_mb: int = typer.Option(
        CONFIG_DEFAULTS["memory_reserve_mb"],
        "--memory-reserve-mb",
        help="Available-RAM warning floor in MB. Below this the pipeline logs a "
        "warning but keeps running (cancelling on transient system-wide dips "
        "would lose encode progress). Pre-flight still refuses to start a new "
        "phase below this floor. Default 2048 (2 GB). If not passed, the "
        "config file's `memory_reserve_mb` key is used.",
    ),
    segment_encode_timeout: int = typer.Option(
        CONFIG_DEFAULTS["segment_encode_timeout"],
        "--segment-timeout",
        help="Per-segment encode timeout in seconds (default 600 = 10 min). "
        "Raise for very long segments or slow hardware. If not passed, "
        "the config file's `segment_encode_timeout` key is used.",
    ),
    final_concat_timeout: int = typer.Option(
        CONFIG_DEFAULTS["final_concat_timeout"],
        "--final-concat-timeout",
        help="Final concat-demuxer timeout in seconds (default 86400 = 24 h). "
        "Absolute ceiling on the final concat pass. If not passed, the "
        "config file's `final_concat_timeout` key is used.",
    ),
    silence_timeout: int = typer.Option(
        CONFIG_DEFAULTS["silence_timeout"],
        "--silence-timeout",
        help="Silence detection ceiling in seconds (default 36000 = 10 h). "
        "If not passed, the config file's `silence_timeout` key is used.",
    ),
    stall_kill_timeout: int = typer.Option(
        CONFIG_DEFAULTS["stall_kill_timeout"],
        "--stall-timeout",
        help="No-progress kill timeout in seconds (default 300 = 5 min). "
        "ffmpeg is killed if no progress line arrives within this window. "
        "If not passed, the config file's `stall_kill_timeout` key is used.",
    ),
    stall_warning_timeout: int = typer.Option(
        CONFIG_DEFAULTS["stall_warning_timeout"],
        "--stall-warning-timeout",
        help="No-progress WARNING timeout in seconds (default 120 = 2 min). "
        "A stall warning is logged (and surfaced in the UI) when no progress "
        "line arrives within this window; the kill timer is `--stall-timeout`. "
        "If not passed, the config file's `stall_warning_timeout` key is used.",
    ),
    waveform_timeout: int = typer.Option(
        CONFIG_DEFAULTS["waveform_timeout"],
        "--waveform-timeout",
        help="Waveform preview decode timeout in seconds (default 300 = 5 min). "
        "Accepted and range-validated for parity with the config key (the GUI "
        "emits it in copied commands); the CLI itself renders no waveform "
        "preview, so it has no further effect on this run.",
    ),
    batch_chunk_size: int = typer.Option(
        CONFIG_DEFAULTS["batch_chunk_size"],
        "--batch-chunk-size",
        help="Number of keep-segments per batch filter invocation (default 40). "
        "Scaled down dynamically for large segment counts. If not passed, "
        "the config file's `batch_chunk_size` key is used.",
    ),
    min_part_bytes: int = typer.Option(
        CONFIG_DEFAULTS["min_part_bytes"],
        "--min-part-bytes",
        help="Minimum bytes for a resumed part to be considered valid "
        "(default 1024). Smaller files are re-encoded. If not passed, the "
        "config file's `min_part_bytes` key is used.",
    ),
    output_fps: str = typer.Option(
        CONFIG_DEFAULTS["output_fps"],
        "--output-fps",
        help="Output FPS policy: 'source' preserves the source cadence; "
        "'24', '25', '30', '50', or '60' force CFR conversion via ffmpeg's "
        "fps filter. If not passed, the config file's `output_fps` key is used.",
    ),
    output_format: str = typer.Option(
        CONFIG_DEFAULTS["output_format"],
        "--output-format",
        help="Output container/codec: 'video' (default — H.264 + AAC MP4), "
        "or an audio-only format: 'mp3' (libmp3lame), 'opus' (libopus), "
        "'aac' (m4a), 'wav' (PCM 16-bit, lossless), 'flac' (lossless). "
        "Audio-only outputs drop the video stream entirely. If not passed, "
        "the config file's `output_format` key is used.",
    ),
    threshold: float | None = typer.Option(
        None,
        "--threshold",
        help="Silence detection threshold in dB (default -30, range -60..-5). "
        "Lower (more negative) values only treat quieter audio as silence. "
        "If not passed, the config file's `threshold` key is used.",
    ),
    min_silence: float | None = typer.Option(
        None,
        "--min-silence",
        help="Minimum silence length in seconds (default 2, range 0.1..60). "
        "Shorter gaps than this are kept. If not passed, the config file's "
        "`min_silence` key is used.",
    ),
    margin: float | None = typer.Option(
        None,
        "--margin",
        help="Seconds of audio kept around each silence segment (default 0.5, "
        "range -3..5). Negative values cut into the neighbouring speech. If "
        "not passed, the config file's `margin` key is used.",
    ),
) -> None:
    """
    Compress stream recording by removing silence segments.

    Processes:
    1. Download video (if URL provided)
    2. Detect silence segments
    3. Cut and concatenate video
    """
    # Validate log_format BEFORE any logging happens so an unknown format
    # exits cleanly instead of producing half-Rich/half-JSON output.
    # Spelling rule: the shared normalize_log_format (case-insensitive) —
    # the eager --doctor path uses the exact same function, so one value
    # has one rule on every surface.
    log_format_lower = normalize_log_format(log_format)
    if log_format_lower is None:
        console.print(
            f"[red]Invalid log format:[/red] {log_format!r} "
            f"(use {' or '.join(repr(f) for f in _VALID_LOG_FORMATS)})"
        )
        raise typer.Exit(1)

    # Validate log_level BEFORE the logging session is entered. The JSON
    # branch of the session hands the level straight to
    # ``install_json_handler`` → logging's level-name lookup, so a bad
    # level would raise a ``ValueError`` there before the user-facing
    # check ever runs — and the rich and json paths would surface two
    # different failures for the same flag (audit round 13 P2). Validate
    # once, on one path, and hand the session the canonical UPPER spelling.
    valid_levels = ("DEBUG", "INFO", "WARNING", "ERROR")
    level = log_level.upper()
    if level not in valid_levels:
        console.print(
            f"[red]Invalid log level:[/red] {log_level!r} (use DEBUG, INFO, WARNING, or ERROR)"
        )
        raise typer.Exit(1)

    # CLI logging is owned by a per-run session context manager
    # (``cli_helpers.logging_session``): ``__enter__`` takes the snapshot
    # and installs the JSON stdout handler or the Rich console handler;
    # ``__exit__`` closes the run's file handler (whatever object
    # ``_log_state.file_handler`` points at) and restores the snapshot on
    # EVERY exit — success, ``typer.Exit``, or exception. The previous
    # hand-written snapshot + ``try/finally`` lived in this function and
    # leaked logging state whenever the try boundary drifted past a
    # mutating statement (missing ffmpeg, bad ``--log-level`` — the
    # audit's P1). The session makes that mistake structurally impossible:
    # setup and restore are one construct, not two code sites to keep in
    # sync (audit round 13 follow-up).

    def _set_json_mode(value: bool) -> None:
        global _JSON_LOG_MODE
        _JSON_LOG_MODE = value

    prev_handler: Any = None
    # False until the current SIGINT handler has been captured. A
    # failure BEFORE that point must not "restore" SIG_DFL — that would
    # clobber a host's signal handler the CLI never touched.
    _sigint_captured = False
    # The session's __enter__ rejects a second concurrent session with
    # LoggingSessionBusyError BEFORE touching any CLI state. That
    # rejection is an EXPECTED outcome (a host already running one
    # embedded CLI), not an internal failure — print a short message and
    # exit 1 instead of leaking an unhandled traceback through Typer
    # (audit round 20 P2). Enter/exit are driven manually so the guard
    # can sit between them without re-indenting the whole body below:
    # the body keeps its original indentation under the inner ``try``,
    # and the outer ``finally`` below drives the session's ``__exit__``.
    logging_session_cm = logging_session(log_format_lower, level, _set_json_mode)
    try:
        _log_state = logging_session_cm.__enter__()
    except LoggingSessionBusyError:
        console.print(
            "[red]Error:[/red] another embedded CLI session is active; "
            "logging sessions cannot overlap"
        )
        raise typer.Exit(1) from None
    try:
        try:
            # Verify ffmpeg is available
            _check_ffmpeg()

            # Apply the (already-validated) log level to the console handler,
            # not the logger itself, so the file handler still receives DEBUG
            # regardless of the user's choice.
            _console_handler.setLevel(level)

            # ``signal.getsignal`` / ``signal.signal`` can only be called from the
            # interpreter's main thread; a host embedding ``cli.main`` in a worker
            # thread would otherwise crash here with ``ValueError``. When that
            # happens, fall back to ``None`` so the ``finally``-block restore path
            # uses ``SIG_DFL`` (and even that restore will be guarded below).
            try:
                prev_handler = signal.getsignal(signal.SIGINT)
            except (ValueError, OSError) as e:
                logger.warning(f"Could not read current SIGINT handler: {e}")
                prev_handler = None
            _sigint_captured = True

            # SIGINT drives the pipeline controller's cancel_event (the event half
            # of the pair); the callback half exists for embedding hosts that
            # want to poll it.
            cancel_event, _cancel_cb = _make_sigint_cancel()
            # When a host calls ``main()`` twice in the same process, the second
            # invocation would otherwise read *our own* handler back via
            # ``getsignal`` and then restore a stale cancel-event closure on exit.
            # Marking the installed handler's owner lets the ``finally`` block
            # detect that case and restore ``SIG_DFL`` instead, which is the only
            # well-defined "previous" state a bare script had before the CLI ran.
            # Identity check against the module-level reference in
            # cli_helpers, not a name+module heuristic — a refactor that renamed
            # the closure would break the old check silently.
            import stream2video.cli_helpers as _ch

            _ours = prev_handler is not None and prev_handler is getattr(
                _ch, "_installed_sigint_handler", None
            )
            if _ours:
                # A previous in-process main() call never restored. Treat the
                # pre-CLI state as SIG_DFL rather than restoring our own stale
                # closure (which would keep the old cancel event alive forever).
                prev_handler = signal.SIG_DFL

            # Load configuration. ``load_config`` strictly validates BOTH numeric
            # ranges (CONFIG_RANGES) AND enum keys (method/encoder/...), so a
            # bad YAML value for any of these cannot sneak through here.
            config = load_config(config_file)

            # Resolve the effective output directory: an explicit -o/--output
            # wins; otherwise honour the config file's ``output_dir`` key
            # (parity with the GUI worker, which reads the same key from
            # settings.json), else the historical ``./processed_videos``
            # default. The typer option's default stays as the fallback for
            # hosts that call main() with an explicit value.
            if not is_from_cli(ctx, "output_dir"):
                output_dir = Path(config.get("output_dir") or "./processed_videos")

            # Ensure the output directory exists — deliberately AFTER config
            # load and resolution, so an invalid config aborts before any
            # directory is created and the config's ``output_dir`` is
            # honoured (the mkdir used to run on the typer default before
            # the YAML was even read, both ignoring the key and littering a
            # stray ./processed_videos on a config error). A failure here
            # (permission, read-only drive, path too long) is a clear
            # startup error, not a mid-pipeline crash.
            try:
                output_dir.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                console.print(f"[red]Cannot create output directory:[/red] {output_dir} ({e})")
                raise typer.Exit(1) from None
            log_file = output_dir / "stream2video.log"

            if not _JSON_LOG_MODE:
                console.print(
                    "\n[bold cyan]stream2video[/bold cyan] - Compress by removing silence"
                )
                console.print(f"Logs saved to: {log_file}\n")

            _log_state.file_handler = _make_file_handler(log_file)
            logger.addHandler(_log_state.file_handler)

            # Apply the resource preset. The preset bundles tunables
            # (x264_low_memory, memory_limit_mb, batch_chunk_size,
            # low_process_priority) into a named profile; each subsequent
            # resolver call below reads the merged config AND checks
            # ``ParameterSource.COMMANDLINE``, so an explicit --flag wins
            # over the preset's override. The preset itself is resolved via
            # a throwaway resolver — CLI --preset wins, otherwise the YAML
            # key ``preset`` (if present) is used, else DEFAULT_PRESET.
            # The MAIN resolver is created AFTER ``apply_preset()``: a
            # resolver bound to the pre-preset config would keep resolving
            # every flag below from the unmerged defaults, so e.g.
            # ``--preset low_memory`` would silently fail to enable
            # x264_low_memory / batch_chunk_size=20 / low_process_priority.
            #
            # Precedence contract (README): explicit keys WIN per-key.
            # ``load_config`` tags the keys it explicitly read from the
            # YAML file; the preset is applied as a baseline, then each
            # explicitly-written preset-managed key is layered back on top
            # — so ``preset: low_memory`` + ``batch_chunk_size: 50`` in one
            # file runs 50, while keys the user left unset pick up the
            # preset's value (audit round 13 P1). An explicit --flag still
            # beats both, enforced by the resolver below.
            resolved_preset = make_resolver(ctx, config, console).resolve("preset", preset)
            # ``apply_preset`` raises ValueError for an unknown preset, and
            # the resolver already rejected any value outside PRESET_NAMES —
            # no separate validity check needed here.
            _explicit = getattr(config, "explicit_keys", None) or frozenset()
            config = apply_preset(config, resolved_preset, explicit_keys=_explicit)
            resolver = make_resolver(ctx, config, console)

            # Resolve each CLI flag against the config via the generic resolver.
            # A flag that the user passed explicitly (ParameterSource.COMMANDLINE)
            # wins; one that the user left at its default falls back to the
            # config value (which came from YAML if provided, else CONFIG_DEFAULTS,
            # with preset overrides already applied above).
            method = resolver.resolve("method", method)
            encoder = resolver.resolve("encoder", encoder)
            video_quality = resolver.resolve("video_quality", video_quality)
            download_quality = resolver.resolve("download_quality", download_quality)
            audio_quality = resolver.resolve("audio_quality", audio_quality)
            software_fallback = resolver.resolve("software_fallback", software_fallback)
            x264_preset = resolver.resolve("x264_preset", x264_preset)
            output_fps = resolver.resolve("output_fps", output_fps)
            output_format = resolver.resolve("output_format", output_format)

            # The output filename extension is derived by the controller from
            # ``output_format`` (``OUTPUT_FORMAT_SPECS``), so the CLI no
            # longer computes its own suffix here (audit #11) — the old
            # ``artifact_stem(video_path) + "_" + output_suffix`` naming now
            # lives in exactly one place.

            # Bool and int parameters with a single call site each. The
            # resolver reads the CLI flag value, the YAML config value, and
            # the preset-merged CONFIG_DEFAULTS, applying CLI > config >
            # default precedence.
            resolved_x264_low_memory: bool = resolver.resolve("x264_low_memory", x264_low_memory)
            resolved_use_crf: bool = resolver.resolve("use_crf", use_crf)
            resolved_gapless_concat: bool = resolver.resolve("gapless_concat", gapless_concat)
            resolved_low_process_priority: bool = resolver.resolve(
                "low_process_priority", low_process_priority
            )
            resolved_encoder_threads: str | int = resolver.resolve(
                "encoder_threads", encoder_threads
            )
            resolved_memory_limit_mb: str | int = resolver.resolve(
                "memory_limit_mb", memory_limit_mb
            )
            resolved_memory_reserve_mb: int = resolver.resolve(
                "memory_reserve_mb", memory_reserve_mb
            )
            resolved_rlimit_as_mb: int = resolver.resolve("rlimit_as_mb", rlimit_as_mb)

            force = resolver.resolve("force", force)
            delete_after = resolver.resolve("delete_after", delete_after)
            per_video_dir_resolved = resolver.resolve("per_video_dir", per_video_dir)
            # Push the resolved value back into config so downstream code that
            # reads config["per_video_dir"] (e.g. paths.apply_per_video_dir)
            # sees the same value as the CLI path.
            config["per_video_dir"] = per_video_dir_resolved

            # batch_chunk_size is a preset-tunable, so honour the preset
            # override unless the user passed --batch-chunk-size explicitly.
            batch_chunk_size = resolver.resolve("batch_chunk_size", batch_chunk_size)

            # P1: pipeline timeouts + network tunables. These were the
            # 9 yaml keys silently ignored before — the CLI used to rely on
            # typer defaults populated from CONFIG_DEFAULTS at import time,
            # so a user ``silence_timeout: 60`` in config.yaml had no effect.
            resolved_download_timeout: int = resolver.resolve("download_timeout", download_timeout)
            resolved_connect_timeout: int = resolver.resolve("connect_timeout", connect_timeout)
            resolved_no_progress_timeout: int = resolver.resolve(
                "no_progress_timeout", no_progress_timeout
            )
            resolved_silence_timeout: int = resolver.resolve("silence_timeout", silence_timeout)
            resolved_segment_encode_timeout: int = resolver.resolve(
                "segment_encode_timeout", segment_encode_timeout
            )
            resolved_final_concat_timeout: int = resolver.resolve(
                "final_concat_timeout", final_concat_timeout
            )
            resolved_stall_kill_timeout: int = resolver.resolve(
                "stall_kill_timeout", stall_kill_timeout
            )
            resolved_stall_warning_timeout: int = resolver.resolve(
                "stall_warning_timeout", stall_warning_timeout
            )
            resolved_waveform_timeout: int = resolver.resolve("waveform_timeout", waveform_timeout)
            resolved_completion_sound: bool = resolver.resolve("completion_sound", completion_sound)
            resolved_min_part_bytes: int = resolver.resolve("min_part_bytes", min_part_bytes)

            # P1: proxy — honour YAML + CLI. The resolver's ``proxy`` kind
            # implements the "CLI --proxy implies proxy_active=True" contract:
            # a user explicitly passing --proxy on the command line means the
            # run goes through the proxy. YAML's ``proxy: url`` is inert unless
            # paired with ``proxy_active: true`` so a config file doesn't
            # silently change networking (matches the GUI's checkbox contract).
            # An explicit --proxy-active / --no-proxy-active flag pins the gate
            # in either direction BEFORE the proxy resolves — the copied
            # command of a GUI with the proxy OFF pastes --no-proxy-active so
            # a ``proxy_active: true`` in user_defaults.json cannot
            # re-enable the stored address (audit P1).
            if proxy_active is not None:
                resolver.pin_proxy_active(proxy_active)
            resolved_proxy: str = resolver.resolve("proxy", proxy)

            # P1: silence-tuning floats. Explicit CLI flags (--threshold /
            # --min-silence / --margin) win; otherwise the config value
            # (YAML or defaults) is used — the copied GUI command no longer
            # needs a side-car YAML to carry the slider values.
            resolved_threshold: float = resolver.resolve("threshold", threshold)
            resolved_min_silence: float = resolver.resolve("min_silence", min_silence)
            resolved_margin: float = resolver.resolve("margin", margin)

            # Assemble the immutable config snapshot the controller runs on.
            # Every value above was already validated: the resolver rejects
            # bad CLI flags (enum whitelists + CONFIG_RANGES bounds via
            # PARAM_SPECS) and ``load_config`` rejected bad YAML, so no
            # second validation pass is needed here (the GUI worker runs the
            # same validator because its config arrives from hand-editable
            # JSON, not from these two chokepoints).
            _pcfg = build_pipeline_config(
                input_raw=input_video,
                output_dir=output_dir,
                method=method,
                encoder=encoder,
                video_quality=video_quality,
                audio_quality=audio_quality,
                download_quality=download_quality,
                software_fallback=software_fallback,
                x264_preset=x264_preset,
                encoder_threads=resolved_encoder_threads,
                output_fps=output_fps,
                output_format=output_format,
                force=force,
                delete_after=delete_after,
                per_video_dir=per_video_dir_resolved,
                threshold=resolved_threshold,
                min_silence=resolved_min_silence,
                margin=resolved_margin,
                memory_limit_mb=resolved_memory_limit_mb,
                memory_reserve_mb=resolved_memory_reserve_mb,
                x264_low_memory=resolved_x264_low_memory,
                use_crf=resolved_use_crf,
                gapless_concat=resolved_gapless_concat,
                low_process_priority=resolved_low_process_priority,
                rlimit_as_mb=resolved_rlimit_as_mb,
                download_timeout=resolved_download_timeout,
                connect_timeout=resolved_connect_timeout,
                no_progress_timeout=resolved_no_progress_timeout,
                proxy=resolved_proxy,
                segment_encode_timeout=resolved_segment_encode_timeout,
                final_concat_timeout=resolved_final_concat_timeout,
                silence_timeout=resolved_silence_timeout,
                stall_kill_timeout=resolved_stall_kill_timeout,
                min_part_bytes=resolved_min_part_bytes,
                stall_warning_timeout=resolved_stall_warning_timeout,
                waveform_timeout=resolved_waveform_timeout,
                batch_chunk_size=batch_chunk_size,
                dry_run=dry_run,
            )

            # P1: reify ``software_fallback="ask"``. The callback typer
            # confirms with (``--force``-style defaults can't short-circuit
            # because the sigint-cancel has already fired for Ctrl+C-era
            # reproducibility) closes the gap between the CLI and the
            # GUI's consent dialog.
            def _make_fallback_consent() -> Callable[[], bool] | None:
                if software_fallback != "ask":
                    return None

                def _consent() -> bool:
                    try:
                        return typer.confirm(
                            f"Selected encoder {encoder!r} is unavailable or failed. "
                            "Fall back to libx264 (CPU-heavy, ~3-5x slower)?",
                            default=False,
                        )
                    except Exception:
                        # Non-interactive tty / EOF / headless: refuse the
                        # fallback (``ask`` must not silently switch).
                        return False

                return _consent

            progress_columns = [
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                TimeElapsedColumn(),
                TimeRemainingColumn(),
            ]

            with Progress(*progress_columns, console=console, disable=_JSON_LOG_MODE) as progress:
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
                            # clamp completed to total so the bar caps at 100%
                            # even when the server under-reports Content-Length.
                            completed=min(p.downloaded_bytes or 0.0, p.total_bytes),
                        )
                    else:
                        # Unknown total — show indeterminate bar (no total) so
                        # it reads as "spinning" rather than as "0%". We still
                        # surface the bytes received below so the description
                        # advances meaningfully.
                        progress.update(task1, total=None, completed=0.0)

                    # Delegate the description formatting to the shared
                    # helper so the CLI and GUI stay in sync. Previously the
                    # CLI rolled its own percent/speed/ETA string here; when
                    # the GUI's format diverged (different separator, different
                    # rounding) users saw inconsistent output between the two
                    # entry points. ``build_download_status`` is pure and
                    # unit-tested in tests/test_gui_helpers.py.
                    description = "[cyan]" + build_download_status(
                        downloaded_bytes=p.downloaded_bytes,
                        total_bytes=p.total_bytes,
                        speed=p.speed,
                        eta=p.eta,
                        # clamp: a server under-reporting Content-Length can
                        # yield downloaded>total, which otherwise renders a
                        # >100% ("250.0%") progress label.
                        pct=min(100.0, 100.0 * (p.downloaded_bytes or 0.0) / p.total_bytes)
                        if p.total_bytes
                        else None,
                    )
                    progress.update(task1, description=description)

                # Step 1.5: the controller resolves the per-video project dir
                # internally (apply_per_video_dir); the ``on_output_resolved``
                # hook fires right after so the CLI can mark the download task
                # done, move the log file into the project dir, and announce it.
                def _on_output_resolved(out_dir: Path, vpath: Path, is_dl: bool) -> None:
                    nonlocal output_dir
                    if is_dl:
                        progress.update(
                            task1,
                            total=1,
                            completed=1,
                            description="[green]+[/green] Video downloaded",
                        )
                    else:
                        progress.update(
                            task1,
                            total=1,
                            completed=1,
                            description="[green]+[/green] Local file (download skipped)",
                        )
                    if out_dir == output_dir:
                        return
                    if is_dl:
                        logger.info(f"Moved source into project dir: {vpath}")
                    if _log_state.file_handler is not None:
                        # Safe swap: the old handler must stay
                        # attached until the new one is proven constructible —
                        # but on Windows the open FileHandler holds a lock that
                        # blocks shutil.move (WinError 32), so the move happens
                        # between close() and addHandler(). Order:
                        #   1. Close+detach old handler (releases the lock).
                        #   2. Move the log file to the project dir.
                        #   3. Construct+attach the new handler. If ANY of
                        #      these fails after step 1, the logging
                        #      session's teardown closes whichever handler
                        #      ``_log_state.file_handler`` points at
                        #      (removeHandler+close on a closed handler is
                        #      harmless, so the path is idempotent); the log
                        #      lands wherever step 2 left it and the run
                        #      continues with the new handler only if step
                        #      3 succeeded.
                        old_fh = _log_state.file_handler
                        new_log = out_dir / "stream2video.log"
                        try:
                            logger.removeHandler(old_fh)
                            old_fh.close()
                            if new_log.exists():
                                new_log.unlink()
                            if log_file.exists():
                                shutil.move(str(log_file), str(new_log))
                            new_fh = _make_file_handler(new_log)
                        except OSError as e:
                            # The old handler is closed by now; re-attaching
                            # IT would leave the logger feeding a dead stream
                            # (every record dies in logging.handleError for the
                            # rest of the run). Re-open whichever file exists:
                            # the moved one wins when step 2 succeeded and the
                            # failure happened at step 3, otherwise the original.
                            fallback_log = new_log if new_log.exists() else log_file
                            try:
                                _log_state.file_handler = _make_file_handler(fallback_log)
                            except OSError:
                                _log_state.file_handler = None
                            if _log_state.file_handler is not None:
                                logger.addHandler(_log_state.file_handler)
                            logger.warning(
                                f"Could not move log file to project dir ({e}); "
                                f"log continues on the original path where possible"
                            )
                        else:
                            logger.addHandler(new_fh)
                            _log_state.file_handler = new_fh
                    output_dir = out_dir
                    console.print(f"Project directory: [cyan]{output_dir}[/cyan]")

                # Step 2: Detect silence (with cache support)
                task2 = progress.add_task("[cyan]Detecting silence segments...", total=100)
                task_cut: TaskID | None = None
                task_concat: TaskID | None = None

                def _console_log_line(msg: str) -> None:
                    # The controller's on_log stream (step banners, cache
                    # hits, disk/memory warnings, delete-after notices).
                    # Rendered raw (markup=False) so [WARN]/[ERROR] tags show
                    # literally, matching the GUI's log panel.
                    console.print(msg, markup=False)

                def _on_phase_cli(name: str, f: float) -> None:
                    # Atomic named-phase dispatch from the controller: drive
                    # the per-phase Rich bars. Phase tasks are created lazily
                    # so the bar list grows in the same order as before
                    # (silence → cutting → concatenating).
                    nonlocal task_cut, task_concat
                    if name == "silence":
                        progress.update(task2, completed=min(f * 100, 100))
                        if f >= 1.0:
                            progress.update(
                                task2, description="[green]+[/green] Silence detection done"
                            )
                    elif name == "cutting":
                        if task_cut is None:
                            task_cut = progress.add_task("[cyan]Cutting segments...", total=100)
                        progress.update(task_cut, completed=min(f * 100, 100))
                        if f >= 1.0:
                            progress.update(task_cut, description="[green]+[/green] Cutting done")
                    else:  # concatenating
                        if task_cut is None:
                            task_cut = progress.add_task("[cyan]Cutting segments...", total=100)
                        if task_concat is None:
                            progress.update(
                                task_cut, completed=100, description="[green]+[/green] Cutting done"
                            )
                            task_concat = progress.add_task("[cyan]Concatenating...", total=100)
                        progress.update(task_concat, completed=min(f * 100, 100))
                        if f >= 1.0:
                            progress.update(
                                task_concat, description="[green]+[/green] Concatenating done"
                            )

                # The CLI and GUI now orchestrate through the SAME
                # PipelineController (audit #11); this block only renders
                # (Rich bars, console lines) and provides the interactive
                # libx264 consent. The controller owns download / project-dir
                # resolution / silence cache+resume / concat / output
                # validation / delete-after — the old hand-rolled phases
                # here drifted from the GUI's (e.g. missing-output success),
                # which is exactly what the audit flagged.
                controller = PipelineController(
                    cfg=_pcfg,
                    cb=PipelineCallbacks(
                        on_progress=lambda f: None,
                        on_status=lambda text, *, force=False: None,
                        on_log=_console_log_line,
                        on_info=_console_log_line,
                        on_overall=lambda elapsed, remaining, silent: None,
                        on_total=lambda total: None,
                        on_phase=_on_phase_cli,
                        on_download_progress=_download_progress_cb,
                        on_pipeline_complete=lambda summary: None,
                    ),
                    cancel_event=cancel_event,
                    on_output_resolved=_on_output_resolved,
                    on_fallback_consent=_make_fallback_consent(),
                )

                # CLI ↔ GUI parity: the GUI's worker plays an "attention"
                # chime on cancel/failure and "success" on completion. Best-
                # effort — playback failure returns a warning string instead
                # of raising.
                def _play_attention_sound() -> None:
                    if not resolved_completion_sound:
                        return
                    try:
                        _sound_warning = play_completion_sound(enabled=True, kind="attention")
                        if _sound_warning:
                            console.print(f"[yellow]{_sound_warning}[/yellow]")
                    except Exception:
                        logger.debug("play_completion_sound raised", exc_info=True)

                try:
                    result = controller.run()
                    if _pcfg.dry_run:
                        # --dry-run: the controller stopped after silence
                        # detection. Show the "what would be cut" summary and
                        # exit before the encode phase starts. This is the
                        # tuning loop: a user adjusts threshold / min_silence /
                        # margin in the config, runs --dry-run, reads the
                        # stats, and iterates without spending CPU on a
                        # throwaway encode. See tests/test_cli_dry_run.py.
                        if result.silence_segments is None or result.keep_segments is None:
                            # Defensive: both segment lists are part of the
                            # dry-run contract. Checked explicitly rather than
                            # via ``assert`` — asserts vanish under
                            # ``python -O`` and the summary would then be fed
                            # None (mirrors the output_path guard below).
                            console.print(
                                "[red]Dry-run summary unavailable:[/red] "
                                "the controller returned no segment lists"
                            )
                            raise typer.Exit(1)
                        console.print()
                        console.print(
                            fmt_dry_run_summary(
                                src_duration=result.src_duration,
                                src_size_bytes=result.src_size_bytes,
                                silence_segments=result.silence_segments,
                                keep_segments=result.keep_segments,
                            )
                        )
                        raise typer.Exit(0)

                    output_video = result.output_path
                    if output_video is None:
                        # Defensive: success without a resolved output path is
                        # a controller bug; fail loudly instead of printing a
                        # summary that dereferences None.
                        raise typer.Exit(1)

                    # Mark whichever task is live as done
                    if task_concat is not None:
                        progress.update(
                            task_concat,
                            completed=100,
                            description="[green]+[/green] Concatenating done",
                        )
                    else:
                        if task_cut is None:
                            # on_phase never fired (e.g. single-segment or a
                            # stub cut_and_concat in tests) — materialize the
                            # bars so the summary still shows the full flow.
                            task_cut = progress.add_task("[cyan]Cutting segments...", total=100)
                        progress.update(
                            task_cut, completed=100, description="[green]+[/green] Cutting done"
                        )
                        # No concat phase (e.g. single segment) — still show it
                        tc = progress.add_task("[cyan]Concatenating...", total=100)
                        progress.update(
                            tc, completed=100, description="[green]+[/green] Concatenating done"
                        )

                except PipelineCancelled:
                    console.print("[yellow]Pipeline cancelled.[/yellow]")
                    _play_attention_sound()
                    raise typer.Exit(130) from None
                except PipelineDownloadError as e:
                    cause = e.__cause__
                    if isinstance(cause, URLValidationError):
                        # Caught before the generic download handler so the
                        # user gets a clear "this isn't a URL or local file"
                        # message.
                        console.print(f"[red]Invalid input:[/red] {e}")
                        console.print(
                            "  Expected an http(s):// URL or an existing local file path."
                        )
                        raise typer.Exit(2) from None
                    if isinstance(cause, VideoNotAvailableError):
                        console.print(f"[red]Video unavailable:[/red] {e}")
                        console.print("  The video may be private, deleted, or region-restricted.")
                        raise typer.Exit(1) from None
                    if isinstance(cause, DownloadTimeoutError):
                        console.print(f"[red]Download timed out:[/red] {e}")
                        console.print("  Try again later or check your connection.")
                        raise typer.Exit(1) from None
                    if isinstance(cause, DiskSpaceError):
                        console.print(f"[red]Disk space error:[/red] {e}")
                        console.print("  Free up disk space and try again.")
                        raise typer.Exit(1) from None
                    if isinstance(cause, PermissionDeniedError):
                        console.print(f"[red]Permission denied:[/red] {e}")
                        console.print("  Check file permissions and try again.")
                        raise typer.Exit(1) from None
                    if isinstance(cause, FileBusyError):
                        console.print(f"[red]File in use:[/red] {e}")
                        console.print("  Close the program using the file and try again.")
                        raise typer.Exit(1) from None
                    console.print(f"[red]Download failed:[/red] {e}")
                    logger.exception("Download error")
                    _play_attention_sound()
                    raise typer.Exit(1) from None
                except PipelineSilenceError as e:
                    console.print(f"[red]Silence detection failed:[/red] {e}")
                    logger.exception("Silence detection error")
                    _play_attention_sound()
                    raise typer.Exit(1) from None
                except PipelineConcatError as e:
                    console.print(f"[red]Concatenation failed:[/red] {e}")
                    logger.exception("Concatenation error")
                    _play_attention_sound()
                    raise typer.Exit(1) from None
                except PipelineUnexpectedError as e:
                    # The controller already logged the full traceback;
                    # surface the user-facing message and preserve the cause.
                    console.print(f"[red]Unexpected error:[/red] {e}")
                    _play_attention_sound()
                    raise typer.Exit(1) from e

            # Summary — show rich stats: input/output size + duration, percent
            # saved, wall-clock time, and a ``X.Yx realtime`` throughput hint.
            # The numbers help the user sanity-check a long encode at a glance
            # (a 6h source -> 1h output at 8x realtime is ~7.5 min wall time,
            # anything slower suggests something went wrong).
            try:
                from stream2video.concat import get_video_duration as _get_duration

                src_size = result.src_size_bytes
                dst_size = result.dst_size_bytes
                src_dur_secs = result.src_duration  # None on ffprobe failure
                # Wall-clock from the start of the controller run to the
                # completion banner — covers download + silence + encode and
                # matches what the user watched on the clock.
                elapsed = result.pipeline_seconds
                # Use output_video's duration as "keep_dur" — that's the
                # actual encoded length, which beats an estimate computed
                # from the input. ``None`` on an empty file is tolerated by
                # fmt_completion_summary.
                keep_dur_secs = _get_duration(output_video) if output_video.exists() else None
                console.print(
                    "\n"
                    + fmt_completion_summary(
                        src_duration=src_dur_secs,
                        src_size_bytes=src_size,
                        output_path=str(output_video),
                        dst_size_bytes=dst_size,
                        keep_duration=keep_dur_secs if keep_dur_secs is not None else 0.0,
                        pipeline_seconds=elapsed,
                    )
                )
            except Exception as e:
                # Summary formatting should never crash the pipeline — fall
                # back to the legacy one-line output (the "Output:" line below).
                logger.debug(f"Could not build completion summary: {e}", exc_info=True)
                console.print("\n[bold green]+ Compression complete![/bold green]")
                console.print(f"Output: [cyan]{output_video}[/cyan]")

            # Delete-after is handled by the controller inside its ``_finish``
            # step (audit #11) — it owns the download + delete lifecycle, so
            # this block must not exist here anymore.
            logger.info(f"Successfully compressed video to {output_video}")

            # Completion chime (CLI ↔ GUI parity): the GUI's worker plays
            # ``completion_sound`` from the same config key. Best-effort —
            # playback failure returns a warning string instead of raising.
            if resolved_completion_sound:
                try:
                    _sound_warning = play_completion_sound(enabled=True)
                    if _sound_warning:
                        console.print(f"[yellow]{_sound_warning}[/yellow]")
                except Exception:
                    logger.debug("play_completion_sound raised", exc_info=True)

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
            # ``signal.getsignal`` can return ``None`` on some interpreters
            # when no handler has been installed; restoring ``None`` would
            # raise TypeError, so treat ``None`` as "no explicit handler" and
            # restore SIG_DFL for a clean state. SIG_IGN / SIG_DFL must be
            # restored just like any other handler: skipping them would leave
            # our temporary ``_handler`` installed, so a host that had SIGINT
            # ignored would see it point at a stale cancel event afterwards.
            # ``prev_handler`` was already de-own'd above when it pointed at
            # our own handler from a previous in-process run — restoring that
            # closure would keep the old cancel event alive forever, and the
            # handler would trip over a disposed context on the next SIGINT.
            # Guard: when the failure happened BEFORE the SIGINT capture (no
            # handler installed by this run), don't touch the host's handler.
            if _sigint_captured:
                restore_to: Any = signal.SIG_DFL if prev_handler is None else prev_handler
                if restore_to is not None:
                    try:
                        signal.signal(signal.SIGINT, restore_to)
                    except (OSError, ValueError, TypeError) as e:
                        # signal.signal raises ValueError when called from a
                        # non-main thread; some platforms raise OSError. Log
                        # rather than silently swallow so a host that runs the
                        # CLI in a worker thread can diagnose why SIGINT wasn't
                        # restored.
                        logger.warning(f"Could not restore SIGINT handler: {e}")
            # The per-run file handler and every piece of logging state the
            # run rewrote (root handlers, propagate, console.stderr, the
            # JSON-mode flag, the console level) are restored by the logging
            # session's ``__exit__`` — on this path AND on every early one.
    finally:
        # Mirror of ``with logging_session(...)``: exit is driven
        # manually (see the enter above) so the LoggingSessionBusyError
        # guard could sit between the two. ``sys.exc_info()`` carries
        # the body's current exception (if any) into __exit__, which
        # restores the snapshot on EVERY path — success, typer.Exit, or
        # exception — and returns None, so the exception propagates.
        logging_session_cm.__exit__(*sys.exc_info())


if __name__ == "__main__":
    app()
