"""Pure-function helpers extracted from ``gui.py``.

These functions have no Tk / no side effects so they can be unit-tested
without instantiating the GUI. The GUI class delegates formatting and
string construction to them; this both shrinks ``gui.py`` and lets the
test suite cover the actual logic (CLI command shape, status-line
truncation, ETA breakdown) instead of driving the widgets through the
Tk event loop.

Anything that touches ``self``, ``ctk``, ``messagebox``, ``Tk``, or the
clipboard stays in ``gui.py`` — only pure transformations live here.
"""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

from stream2video.config import CONFIG_DEFAULTS, PRESETS, apply_preset, effective_defaults
from stream2video.formatters import fmt_clock_time, fmt_size, fmt_speed, fmt_time
from stream2video.param_specs import (
    CLI_BOOL_FLAG_ORDER,
    CLI_VALUE_FLAG_ORDER,
    PARAM_SPECS,
)

# Maximum length of the GUI's status-line text (per line when wrapped).
# Kept as a module-level constant so tests can pin it and the GUI can
# import it without a circular dependency on the class. The two-line
# status label (row 0 + row 1 in prog_frame) wraps via _wrap_status_lines,
# so per-line budget stays 50 but total visible chars double.
STATUS_MAX = 50
# Two-line status uses STATUS_MAX per line, so wrap budgets are separate
# but truncation keeps total within 2*STATUS_MAX.
STATUS_MAX_LINES = 2

# Throttle for ``_ui_status``: subsequent updates closer than this many
# seconds apart are dropped (unless ``force=True``). Keeps the status
# line readable during fast yt-dlp progress bursts.
STATUS_UPDATE_INTERVAL = 0.5

# Overall-ETA is hidden until the whole pipeline's progress fraction
# reaches this value. Below it the estimate (elapsed / progress) is so
# noisy it would show absurd values like "5h" after 30 seconds.
TOTAL_ETA_MIN_PROGRESS = 0.02

# EMA factor applied to each raw ETA sample. 0.25 gives a responsive
# but visibly stable readout when the callbacks fire once per second.
_ETA_SMOOTHING = 0.25


class EtaSmoother:
    """Exponential moving average for a stream of ETA samples (seconds).

    The pipeline's ETA callbacks (``elapsed / fraction - elapsed``)
    jitter hectically second-to-second early in a phase. ``update()``
    returns a smoothed value that converges to the raw estimate with a
    ~4-sample time constant. ``update(None)`` pauses sampling and just
    replays the last smoothed value (used when a callback has no ETA
    yet); ``reset()`` clears the filter at phase boundaries so the
    previous phase's estimate doesn't bleed into the next one.
    """

    def __init__(self, alpha: float = _ETA_SMOOTHING) -> None:
        self._alpha = alpha
        self._smoothed: float | None = None

    def reset(self) -> None:
        self._smoothed = None

    def update(self, raw: float | None) -> float | None:
        if raw is None:
            return self._smoothed
        raw = max(0.0, raw)
        if self._smoothed is None:
            self._smoothed = raw
        else:
            self._smoothed = self._smoothed + self._alpha * (raw - self._smoothed)
        return self._smoothed


# Shell targets supported by ``build_cli_command``. Each has its own
# quoting rules — there is NO universal quoting that is safe in both
# cmd.exe and PowerShell (audit #2: MSVCRT double quotes still let
# PowerShell interpolate ``$(...)``/``$var`` and cmd.exe expand
# ``%VAR%``/``!VAR!``).
POWERSHELL_SHELL = "powershell"
CMD_SHELL = "cmd"
POSIX_SHELL = "posix"
# Characters that force quoting for the target shell. The sets differ:
# cmd.exe expands %VAR% and (with delayed expansion) !VAR! even inside
# double quotes; PowerShell interpolates $var/$(...) inside double
# quotes and treats ` as an escape.
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_./:+@=\\-]+$")


def _default_target_shell() -> str:
    """The shell a pasted command will land in on this platform."""
    return POWERSHELL_SHELL if sys.platform == "win32" else POSIX_SHELL


def _quote_powershell_arg(arg: str) -> str:
    """Quote a single argument for PowerShell (audit #2).

    PowerShell single-quoted strings are fully literal: ``$var``,
    ``$(...)``, backticks and ``%VAR%`` are all passed through verbatim.
    The only escape needed is doubling an embedded single quote
    (``'`` → ``''``). Tokens with no metacharacters are passed through
    unquoted.
    """
    if _SAFE_TOKEN_RE.match(arg):
        return arg
    return "'" + arg.replace("'", "''") + "'"


# cmd.exe metacharacters that force quoting. `%` is included because
# cmd.exe expands %VAR% even inside most contexts; `!` enables delayed
# expansion in cmd.exe. Bare tokens without any of these round-trip
# unquoted in cmd.
_WIN_CMD_METACHARS = re.compile(r'[\s&|<>^"%!]')


def _quote_cmd_arg(arg: str) -> str:
    """Quote a single argument for cmd.exe (audit #2).

    cmd.exe has no fully safe quoting: ``%VAR%`` expands even inside
    double quotes (cannot be escaped interactively) and ``!VAR!``
    expands when delayed expansion is enabled. Tokens containing ``%``
    or ``!`` are therefore REFUSED (ValueError) — better a clear error
    than a command that injects or corrupts. Everything else uses the
    MSVCRT double-quote rules (the same algorithm as CPython's
    ``subprocess.list2cmdline``):

      * a backslash run immediately before a quote: ``2k+1``
        backslashes + the quote (the odd one escapes the quote);
      * a trailing backslash run (before the closing quote we add):
        doubled to ``2k`` so it can't escape the quote.

    Tokens with no whitespace or cmd metacharacters are passed through
    unquoted.
    """
    if not _WIN_CMD_METACHARS.search(arg):
        return arg
    if "%" in arg or "!" in arg:
        raise ValueError(
            "cmd.exe cannot quote an argument containing '%' or '!' safely "
            "(%VAR% expands inside double quotes, !VAR! under delayed "
            "expansion); paste into PowerShell instead"
        )
    out: list[str] = ['"']
    backslashes = 0
    for ch in arg:
        if ch == "\\":
            backslashes += 1
        elif ch == '"':
            out.append("\\" * (2 * backslashes + 1))
            out.append('"')
            backslashes = 0
        else:
            out.append("\\" * backslashes)
            out.append(ch)
            backslashes = 0
    out.append("\\" * (2 * backslashes))
    out.append('"')
    return "".join(out)


def _quote_arg(arg: str, target_shell: str | None = None) -> str:
    """Quote a single CLI argument for the shell the command is pasted into.

    * ``powershell`` (Windows default): single quotes with ``''``
      doubling — the only quoting PowerShell cannot interpolate through.
    * ``cmd``: MSVCRT double quotes, ``!`` escaped; ``%`` is refused
      (unsafe, see :func:`_quote_cmd_arg`).
    * ``posix``: delegates to :func:`shlex.quote`.
    """
    shell = _default_target_shell() if target_shell is None else target_shell
    if not arg:
        # Empty argument — must stay quoted or the token vanishes and
        # swallows the NEXT flag (``--proxy --method``); see the
        # proxy-active-with-empty-address pin in build_cli_command.
        return '""' if shell == CMD_SHELL else "''"
    if shell == POWERSHELL_SHELL:
        return _quote_powershell_arg(arg)
    if shell == CMD_SHELL:
        return _quote_cmd_arg(arg)
    return shlex.quote(arg)


def build_cli_command(
    input_raw: str,
    output_dir: Path,
    *,
    method: str,
    encoder: str,
    video_quality: str,
    download_quality: str,
    audio_quality: str = CONFIG_DEFAULTS["audio_quality"],
    software_fallback: str = CONFIG_DEFAULTS["software_fallback"],
    x264_preset: str = CONFIG_DEFAULTS["x264_preset"],
    encoder_threads: str | int = CONFIG_DEFAULTS["encoder_threads"],
    output_fps: str = CONFIG_DEFAULTS["output_fps"],
    output_format: str = CONFIG_DEFAULTS["output_format"],
    threshold: float = CONFIG_DEFAULTS["threshold"],
    min_silence: float = CONFIG_DEFAULTS["min_silence"],
    margin: float = CONFIG_DEFAULTS["margin"],
    x264_low_memory: bool = CONFIG_DEFAULTS["x264_low_memory"],
    use_crf: bool = CONFIG_DEFAULTS["use_crf"],
    gapless_concat: bool = CONFIG_DEFAULTS["gapless_concat"],
    low_process_priority: bool = CONFIG_DEFAULTS["low_process_priority"],
    preset: str = CONFIG_DEFAULTS["preset"],
    memory_limit_mb: str | int = CONFIG_DEFAULTS["memory_limit_mb"],
    memory_reserve_mb: int = CONFIG_DEFAULTS["memory_reserve_mb"],
    download_timeout: int = CONFIG_DEFAULTS["download_timeout"],
    connect_timeout: int = CONFIG_DEFAULTS["connect_timeout"],
    no_progress_timeout: int = CONFIG_DEFAULTS["no_progress_timeout"],
    segment_encode_timeout: int = CONFIG_DEFAULTS["segment_encode_timeout"],
    final_concat_timeout: int = CONFIG_DEFAULTS["final_concat_timeout"],
    silence_timeout: int = CONFIG_DEFAULTS["silence_timeout"],
    stall_kill_timeout: int = CONFIG_DEFAULTS["stall_kill_timeout"],
    stall_warning_timeout: int = CONFIG_DEFAULTS["stall_warning_timeout"],
    waveform_timeout: int = CONFIG_DEFAULTS["waveform_timeout"],
    batch_chunk_size: int = CONFIG_DEFAULTS["batch_chunk_size"],
    min_part_bytes: int = CONFIG_DEFAULTS["min_part_bytes"],
    rlimit_as_mb: int = CONFIG_DEFAULTS["rlimit_as_mb"],
    force: bool = CONFIG_DEFAULTS["force"],
    delete_after: bool = CONFIG_DEFAULTS["delete_after"],
    completion_sound: bool = CONFIG_DEFAULTS["completion_sound"],
    proxy: str = CONFIG_DEFAULTS["proxy"],
    proxy_active: bool = CONFIG_DEFAULTS["proxy_active"],
    per_video_dir: bool = CONFIG_DEFAULTS["per_video_dir"],
    target_shell: str | None = None,
) -> str:
    """Build the equivalent CLI invocation for the current GUI settings.

    Used by the GUI's "Copy CLI command" button so a user who's tuned
    the GUI can paste the same operation into a shell, a script, or
    documentation. Pure: returns the string, doesn't touch the
    clipboard.

    ``target_shell`` selects the quoting rules (audit #2: there is no
    quoting that is safe in both cmd.exe and PowerShell). Defaults to
    PowerShell on Windows — the only Windows shell whose args can be
    quoted losslessly — and POSIX sh elsewhere. ``cmd`` is best-effort
    and raises :class:`ValueError` when an argument cannot be quoted
    safely for it (contains ``%``).

    The shape mirrors what the CLI actually accepts (see
    ``stream2video.cli.main``) — the conditional flags are derived from
    the shared ``param_specs`` table (``PARAM_SPECS`` +
    ``CLI_VALUE_FLAG_ORDER`` / ``CLI_BOOL_FLAG_ORDER``), the same table
    the CLI's resolver validates against, so the copied command cannot
    drift from the CLI's flag names. Defaults are taken from
    ``effective_defaults()`` — CONFIG_DEFAULTS plus any user
    ``user_defaults.json`` overrides, i.e. the exact starting point
    ``cli_config.load_config`` uses — flags are appended only when their
    value diverges from that default, keeping the copied command
    readable when the user hasn't customised everything. Bool flags use
    the same divergence rule (positive flag when the divergent value is
    True, ``--no-*`` when False), so a GUI that switched a toggle off
    that a user_defaults.json keeps on still pins the off-state in the
    pasted command (audit P1: the ``--no-proxy-active`` case).

    ``proxy_active`` mirrors the GUI's "Use proxy" checkbox: when True
    AND ``proxy`` carries an address, the address travels via
    ``--proxy``; the gate flag (``--proxy-active`` /
    ``--no-proxy-active``) is emitted whenever the GUI's checkbox state
    diverges from the effective default, so the paste can't silently
    re-enable a proxy from user_defaults.json.

    ``config_path`` support was removed: a pasted command carries every
    tunable as explicit flags (including the slider values via
    ``--threshold`` / ``--min-silence`` / ``--margin``), so no side-car
    YAML is needed and no caller passed it anymore.
    """
    parts = ["stream2video"]
    if input_raw:
        parts.append(_quote_arg(input_raw, target_shell))
    parts.extend(["-o", _quote_arg(str(output_dir), target_shell)])
    parts.extend(["--method", method, "--encoder", encoder])
    parts.extend(["--video-quality", video_quality])
    parts.extend(["--download-quality", download_quality])
    # Proxy address: pinned whenever the GUI's checkbox is ON — even
    # when the address is empty (``--proxy ''``) so a stale address in
    # user_defaults.json cannot sneak into the paste (the CLI resolver
    # treats an explicit --proxy as the gate; an explicit empty value
    # pins "direct connection" regardless of a stored address).
    if proxy or proxy_active:
        parts.extend(["--proxy", _quote_arg(proxy, target_shell)])
    # Conditional value flags — appended only when they're not the
    # effective default so the copied command stays compact. The names,
    # flag spellings, order and defaults all come from the shared
    # param_specs table (the same table the CLI's resolver uses), so
    # the GUI and the CLI cannot drift apart on flag names.
    #
    # The divergence baseline for PRESET-MANAGED keys is the value the
    # preset would apply on the CLI side (``apply_preset`` runs before
    # the CLI's resolver), not the bare effective default: with
    # ``--preset low_memory`` and the user having switched
    # low_process_priority back off, the GUI value equals the effective
    # default (False) but the CLI's preset overlay would force True —
    # the flag must be spelled out or the paste silently runs the
    # preset's value (audit P1 follow-up).
    _defaults = effective_defaults()
    if preset in PRESETS:
        _preset_applied = apply_preset(_defaults, preset)
    else:
        _preset_applied = _defaults
    _values = {
        "audio_quality": audio_quality,
        "software_fallback": software_fallback,
        "x264_preset": x264_preset,
        "encoder_threads": encoder_threads,
        "output_fps": output_fps,
        "output_format": output_format,
        "threshold": threshold,
        "min_silence": min_silence,
        "margin": margin,
        "memory_limit_mb": memory_limit_mb,
        "memory_reserve_mb": memory_reserve_mb,
        "rlimit_as_mb": rlimit_as_mb,
        "download_timeout": download_timeout,
        "connect_timeout": connect_timeout,
        "no_progress_timeout": no_progress_timeout,
        "segment_encode_timeout": segment_encode_timeout,
        "final_concat_timeout": final_concat_timeout,
        "silence_timeout": silence_timeout,
        "stall_kill_timeout": stall_kill_timeout,
        "stall_warning_timeout": stall_warning_timeout,
        "waveform_timeout": waveform_timeout,
        "batch_chunk_size": batch_chunk_size,
        "min_part_bytes": min_part_bytes,
        "preset": preset,
    }
    _preset_managed = {k for p in PRESETS.values() for k in p}
    for _name in CLI_VALUE_FLAG_ORDER:
        _value = _values[_name]
        _baseline = _preset_applied.get(_name) if _name in _preset_managed else _defaults[_name]
        if _value != _baseline:
            parts.extend([PARAM_SPECS[_name]["flag"], str(_value)])
    # Bool flags. A value that diverges from the effective default is
    # ALWAYS spelled out — the positive flag for True, the spec table's
    # ``--no-*`` form for False — so the copied command is
    # self-contained even when a user_defaults.json override holds the
    # opposite value. The deciding case: the GUI's proxy checkbox OFF
    # against an inherited ``proxy_active: true`` must paste as
    # ``--no-proxy-active`` or the CLI re-enables the stored proxy
    # (audit P1).
    _bools = {
        "x264_low_memory": x264_low_memory,
        "use_crf": use_crf,
        "gapless_concat": gapless_concat,
        "low_process_priority": low_process_priority,
        "per_video_dir": per_video_dir,
        "completion_sound": completion_sound,
        "force": force,
        "delete_after": delete_after,
        "proxy_active": proxy_active,
    }
    for _name in CLI_BOOL_FLAG_ORDER:
        _spec = PARAM_SPECS[_name]
        _value = _bools[_name]
        _baseline = (
            bool(_preset_applied.get(_name)) if _name in _preset_managed else bool(_defaults[_name])
        )
        if _value != _baseline:
            parts.append(_spec["flag"] if _value else _spec["flag_off"])
    return " ".join(parts)


def mask_proxy(proxy: str) -> str:
    """Redact the credentials embedded in a proxy URL for display/logging.

    ``socks5://user:pass@host:1080`` → ``socks5://***:***@host:1080``.
    Proxies without a ``user:pass@`` part are returned unchanged (there
    is nothing secret to hide). The credential cut is made at the LAST
    ``@`` after the scheme, so a password containing ``@``
    (``socks5://user:pa@ss@host:1080``) is still fully redacted.
    """
    if not proxy:
        return proxy
    if "://" in proxy:
        scheme, _, rest = proxy.partition("://")
    else:
        scheme, rest = None, proxy
    if "@" not in rest:
        return proxy
    _, _, host = rest.rpartition("@")
    return f"{scheme}://***:***@{host}" if scheme else f"***:***@{host}"


def proxy_has_credentials(proxy: str) -> bool:
    """True when ``proxy`` embeds ``user:pass@`` credentials.

    Mirrors :func:`mask_proxy`'s notion of "secret present": a proxy
    URL with an ``@`` after the scheme carries credentials (the cut is
    made at the LAST ``@`` so a password containing ``@`` still counts).
    """
    if not proxy:
        return False
    if "://" in proxy:
        _, _, rest = proxy.partition("://")
    else:
        rest = proxy
    return "@" in rest


def strip_proxy_credentials(proxy: str) -> str:
    """Return *proxy* without its ``user:pass@`` credential part.

    ``socks5://user:pass@host:1080`` → ``socks5://host:1080``. A proxy
    without credentials is returned unchanged. Used to build a copyable
    command that never carries the secret.
    """
    if not proxy_has_credentials(proxy):
        return proxy
    if "://" in proxy:
        scheme, _, rest = proxy.partition("://")
        _, _, host = rest.rpartition("@")
        return f"{scheme}://{host}"
    _, _, host = proxy.rpartition("@")
    return host


def redact_proxy_in_cli_command(cmd: str, proxy: str, target_shell: str | None = None) -> str:
    """Return *cmd* with the ``--proxy`` value replaced by its masked form.

    The GUI log line must never print the credentials: the proxy token
    is swapped for the masked form instead. The replacement is exact
    (the quoted token is unique within the command), so only the proxy
    token is affected. Quoting must match
    :func:`build_cli_command` exactly — i.e. the same ``target_shell``
    must be passed here.
    """
    if not cmd or not proxy:
        return cmd
    quoted = _quote_arg(proxy, target_shell)
    if quoted not in cmd:
        return cmd
    return cmd.replace(quoted, _quote_arg(mask_proxy(proxy), target_shell))


def _wrap_status_lines(
    text: str, max_len: int = STATUS_MAX, max_lines: int = STATUS_MAX_LINES
) -> list[str]:
    """Wrap a status string into up to *max_lines* lines of *max_len* each.

    Word-aware where possible (breaks on space); hard-breaks when a
    single token exceeds max_len. Truncates with an ellipsis on the last
    line if the budget is exhausted. Pure helper for the two-line status
    label — avoids widening the window to show full progress digits.
    """
    if not text:
        return [""] * max_lines
    # Truncate total budget first to avoid runaway wrapping on very long errors
    total_max = max_len * max_lines
    # Use truncation with ellipsis if over budget, but wrap then truncate last line
    words = text.split(" ")
    lines: list[str] = []
    cur = ""
    for w in words:
        if not w:
            continue
        # Handle tokens longer than max_len by hard-splitting
        while len(w) > max_len:
            if cur:
                lines.append(cur)
                cur = ""
                if len(lines) >= max_lines:
                    break
            lines.append(w[:max_len])
            w = w[max_len:]
            if len(lines) >= max_lines:
                break
        if len(lines) >= max_lines:
            break
        sep = " " if cur else ""
        if len(cur) + len(sep) + len(w) <= max_len:
            cur = cur + sep + w
        else:
            lines.append(cur)
            cur = w
            if len(lines) >= max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    # Truncate overflow with ellipsis on last line if needed
    if len(" ".join(text.split())) > total_max and lines and len(lines[-1]) == max_len:
        lines[-1] = lines[-1][: max_len - 1] + "…"
    # Pad to max_lines with empty strings for callers that expect two entries
    while len(lines) < max_lines:
        lines.append("")
    return lines[:max_lines]


def build_download_status(
    *,
    downloaded_bytes: float | None,
    total_bytes: float | None,
    speed: float | None,
    eta: float | None,
    pct: float | None = None,
) -> str:
    """Format a yt-dlp progress update as a status-line string.

    Two shapes:
      * ``total_bytes`` known: ``"Downloading 42.3% (123 MB / 291 MB) at 5.2 MiB/s ETA 1m 12s"``
      * ``total_bytes`` unknown: ``"Downloading 89 MB at 5.2 MiB/s"``

    Falls back to ``"?"`` for unknown fields so the line is always
    readable. Pure: callers (GUI worker thread, CLI progress callback)
    format the bytes/sizes via :mod:`stream2video.formatters` and pass
    them here.
    """
    downloaded_s = fmt_size(int(downloaded_bytes)) if downloaded_bytes is not None else "?"
    speed_s = fmt_speed(speed)
    eta_s = fmt_time(eta) if eta is not None else "?"
    if total_bytes is not None and total_bytes > 0:
        total_s = fmt_size(int(total_bytes))
        if pct is None:
            # Caller didn't compute percent — derive it here so the
            # status line stays self-contained.
            pct = 100.0 * (downloaded_bytes or 0.0) / total_bytes
        # clamp: yt-dlp can report downloaded>total when the server
        # under-reports Content-Length (chunked / unknown size), which
        # otherwise renders "Downloading 250.0% (...)".
        pct = max(0.0, min(100.0, pct))
        return f"Downloading {pct:.1f}% ({downloaded_s} / {total_s}) at {speed_s} ETA {eta_s}"
    return f"Downloading {downloaded_s} at {speed_s}"


def build_eta_tail(
    phase_remaining: float | None,
    more_phases: bool,
) -> str:
    """Render the ``Remaining:`` tail of the Elapsed/Remaining line.

    * ``phase_remaining=None`` or ``<=0``: ``"?"`` when more phases
      follow (we don't know how long they'll take), ``"—"`` when this
      is the last phase (the line is about to disappear).
    * Otherwise: ``"~1m 12s"`` for the last phase, ``"~1m 12s + ?"``
      when more phases follow (the ``+ ?`` flags that the total ETA
      is a lower bound, not a precise estimate).

    Pure: takes the ETA float and the more-phases flag, returns the
    display string. The GUI uses it inside its ``_ui_overall`` method.
    """
    if phase_remaining is None or phase_remaining <= 0:
        return "?" if more_phases else "—"
    formatted = f"~{fmt_time(phase_remaining)}"
    return f"{formatted} + ?" if more_phases else formatted


def build_overall_line(total_elapsed: float, eta_tail: str) -> str:
    """Format the full ``Elapsed: X | Remaining: Y`` line."""
    return f"Elapsed: {fmt_time(total_elapsed)} | Remaining: {eta_tail}"


def build_total_line(total_elapsed: float, overall_est: float | None) -> str:
    """Format the ``Total:`` wall-clock label with an optional ETA.

    ``overall_est`` is the estimated total wall-clock of the whole
    pipeline (``elapsed / overall_progress``); None when the estimate
    is still too noisy — the label then shows elapsed only.
    """
    if overall_est is not None and overall_est > total_elapsed:
        return f"Total: {fmt_time(total_elapsed)} / ~{fmt_time(overall_est)}"
    return f"Total: {fmt_time(total_elapsed)}"


def build_progress_meta_line(
    total_elapsed: float,
    eta_tail: str,
    overall_est: float | None,
) -> str:
    """One-line Elapsed/Remaining/Total readout for the bar row.

    Collapses the former two-row readout (Elapsed/Remaining on one line,
    Total on another) into a single string so the progress frame stays
    two rows tall: the status line + the bar row. ``eta_tail`` comes
    from :func:`build_eta_tail`; ``overall_est`` is the estimated
    whole-run wall-clock (None while still too noisy — omitted).
    """
    parts = [build_overall_line(total_elapsed, eta_tail)]
    if overall_est is not None and overall_est > total_elapsed:
        parts.append(build_total_line(total_elapsed, overall_est))
    return " | ".join(parts)


def build_compact_done_line(
    src_duration: float | None,
    keep_duration: float,
    pipeline_seconds: float,
) -> str:
    """One-line post-run summary shown under the log: durations +
    compression percent + wall-clock, e.g.
    ``Done: 42:10 → 12:30 (-70%) in 8:15``.

    The compression percent is omitted when the source duration is
    unknown (failed probe) or non-positive.
    """
    src_s = fmt_clock_time(src_duration)
    keep_s = fmt_clock_time(keep_duration)
    if src_duration is not None and src_duration > 0:
        pct = max(0, min(100, round(100 * (1 - keep_duration / src_duration))))
        return f"Done: {src_s} → {keep_s} (-{pct}%) in {fmt_time(pipeline_seconds)}"
    return f"Done: {src_s} → {keep_s} in {fmt_time(pipeline_seconds)}"


def build_silence_info_line(
    *,
    num_silence: int,
    num_keep: int,
    keep_duration: float | None,
) -> str:
    """Format the ``Silence: N segments / Keep: M segments (Xm Ys)`` line.

    ``keep_duration=None`` skips the duration parenthetical so the line
    works for the pre-cut preview where we don't yet know how long the
    output will be.
    """
    if keep_duration is not None and keep_duration > 0:
        return (
            f"Silence: {num_silence} segments\n"
            f"Keep: {num_keep} segments ({fmt_time(keep_duration)})"
        )
    return f"Silence: {num_silence} segments\nKeep: {num_keep} segments"


def should_update_status(
    last_update_monotonic: float,
    now_monotonic: float,
    *,
    force: bool = False,
    interval: float = STATUS_UPDATE_INTERVAL,
) -> bool:
    """Decide whether a status-line update should be applied or throttled.

    The GUI's ``_ui_status`` receives many rapid updates from yt-dlp
    progress callbacks; without throttling the status bar redraws
    faster than the eye can read. ``force=True`` bypasses the throttle
    for explicit user-initiated updates (step transitions, errors).
    """
    if force:
        return True
    return now_monotonic - last_update_monotonic >= interval


def build_completion_summary(
    src_size_bytes: int,
    src_duration: float | None,
    dst_size_bytes: int,
    dst_duration: float,
    pipeline_seconds: float,
    output_path: str,
) -> dict:
    """Build the user-facing strings emitted on pipeline completion.

    Pure function — no Tk / no side effects — so it can be unit-tested
    without instantiating the GUI. Previously lived inline in
    ``gui._build_completion_summary``.

    Returns a dict with keys:
      - status:    one-line headline for the status bar: 'Complete!' plus
                   the total wall-clock in parentheses, e.g. 'Complete! (23m 5s)'.
                   Size and duration go in the log block and the popup only.
      - log_lines: list of log lines (with '=' separators) for grep-ability.
                   Contains the full src->dst size/duration breakdown.
      - popup:     multi-line message for the 'Complete' messagebox.
                   Full breakdown with Source/Output labels.
    """
    src_size_s = fmt_size(src_size_bytes)
    dst_size_s = fmt_size(dst_size_bytes)
    src_dur_s = fmt_clock_time(src_duration)
    dst_dur_s = fmt_clock_time(dst_duration)
    pipe_s = fmt_time(pipeline_seconds)

    sep = "=" * 60
    log_lines = [
        sep,
        f"[SUCCESS] Output: {output_path}",
        f"  Size:     {src_size_s} -> {dst_size_s}",
        f"  Duration: {src_dur_s} -> {dst_dur_s}",
        f"  Pipeline: {pipe_s}",
        sep,
    ]

    popup = (
        f"Video saved to:\n{output_path}\n\n"
        f"Source:  {src_size_s}, {src_dur_s}\n"
        f"Output:  {dst_size_s}, {dst_dur_s}\n\n"
        f"Pipeline: {pipe_s}"
    )

    return {
        "status": f"Complete! ({pipe_s})",
        "log_lines": log_lines,
        "popup": popup,
    }


# Back-compat alias so existing imports of ``_build_completion_summary``
# (inside gui.py and any downstream code) keep working during the
# incremental refactor.
_build_completion_summary = build_completion_summary


# Short phase names keyed by the "Step X/4" token the pipeline emits in
# its status lines (see PipelineController._run_*_phase). Used by the
# GUI's status line to render "Step 2/4 · Silence (35%)" instead of a
# bare number.
PHASE_LABELS: dict[str, str] = {
    "1": "Download",
    "2": "Silence",
    "3": "Cutting",
    "4": "Concat",
}


def build_phase_line(step: str | None, pct: int | None = None) -> str:
    """Render the status line's step indicator.

    ``None`` step (no phase running yet) yields an empty string. A known
    ``pct`` appends the LIVE in-phase progress (0..100), e.g.
    ``"Step 2/4 · Silence (35%)"``. Pure so the GUI's worker-thread
    dispatchers can unit-test the formatting.
    """
    if step is None or step not in PHASE_LABELS:
        return ""
    name = PHASE_LABELS[step]
    if pct is not None:
        return f"Step {step}/4 · {name} ({pct}%)"
    return f"Step {step}/4 · {name}"
