"""JSON structured logging for the CLI.

Drop-in replacement for the Rich console handler when the user runs
with ``--log-format json``. Each log record is emitted as a single
JSON object per line to **stdout** (not stderr), suitable for piping
into ELK / Splunk / Loki / journald:

    $ stream2video --log-format json video.mp4 | jq .
    {"ts": "2026-08-09T14:32:07.123Z", "level": "INFO", "logger": "stream2video", "msg": "..."}

Why a custom formatter instead of ``python-json-logger``: the
dependency would be heavy for a CLI tool that already ships Rich, and
the format we emit is intentionally minimal (no thread name, code path
in every record, etc.) to keep line size small for log aggregators.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

# Static schema version. Bump when fields change so parsers can detect.
_LOG_SCHEMA_VERSION = 1


class _JsonFormatter(logging.Formatter):
    """Format log records as JSON, one per line."""

    def format(self, record: logging.LogRecord) -> str:
        obj: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "v": _LOG_SCHEMA_VERSION,
        }
        # Exception info as a single string (newlines escaped inside the
        # JSON string so the line stays one-line-per-record).
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)
        # Standard `extra` fields passed via logger.info("...", extra={})
        # land in ``record.__dict__`` alongside the reserved attributes.
        # Common keys are hoisted into the top level so queries like
        # ``step="download" AND status="ok"`` work without knowing the
        # record's class-path. The top-level keys (``ts``/``level``/
        # ``logger``/``msg``/``v``/``exc``) are owned by this formatter,
        # and the hoisted names are a fixed list that never collides
        # with them — so no ``extra_``-prefix guard is needed (the
        # historical version carried a dead ``_RESERVED`` collision
        # branch whose premise — that these keys could overwrite the
        # reserved ones — was false; audit R4.2).
        for key in ("step", "phase", "duration_s", "bytes", "src", "dst"):
            val = record.__dict__.get(key)
            if val is None:
                continue
            obj[key] = val
        return json.dumps(obj, default=str, ensure_ascii=False)


def install_json_handler(logger: logging.Logger, level: str = "INFO") -> logging.Handler:
    """Build and attach a single JSON-emitting stdout handler to ``logger``.

    The handler is both attached to ``logger`` and returned so callers
    can detach it in a ``finally:`` block (mirroring how the CLI handles
    its Rich handler). Previously-installed JSON handlers from earlier
    ``main()`` calls are removed first (repeated CLI invocations in the
    same process would otherwise double-fire every record); other handler
    kinds are left alone — replacing them is the caller's choice (the
    CLI replaces its Rich console handler explicitly before calling this).

    The stream is captured as ``sys.stdout`` *at call time* (not stderr)
    so a piped command ``stream2video --log-format json video.mp4 | jq .``
    shows the JSON and the human-readable banner is unaffected.

    The handler's level is set from ``level``; the logger's own level is
    left untouched so importers of this module don't get re-configured.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(getattr(logging, level.upper(), logging.INFO))
    handler.setFormatter(_JsonFormatter())
    # Idempotency for repeated ``main()`` calls: a previous invocation
    # attached ITS JSON handler to the app ``logger``; that instance is
    # still there (``basicConfig(force=True)`` only re-roots the root
    # logger, it never detaches handlers from the app logger), so a new
    # handler added below would make every record fire twice — stdout
    # JSON duplicated line-by-line, breaking ``| jq .`` pipes. Drop the
    # old JSON handlers before attaching the fresh one.
    for old in list(logger.handlers):
        if isinstance(old, logging.StreamHandler) and isinstance(old.formatter, _JsonFormatter):
            logger.removeHandler(old)
    # Attach here so the function's name honours its contract. Callers
    # that previously did ``logger.addHandler(install_json_handler(...))``
    # still work — ``addHandler`` is idempotent for the same instance.
    if handler not in logger.handlers:
        logger.addHandler(handler)
    return handler
