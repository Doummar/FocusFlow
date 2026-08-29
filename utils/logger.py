"""Centralized logging for FocusFlow.

All modules import `log` from here instead of using print().  Errors are
written to `focusflow.log` in the addon directory so users can attach the
file when reporting bugs, rather than having to reproduce the issue with
Anki's console open.

Usage
-----
    from ..utils.logger import log

    log.debug("timer started: elapsed=%d", elapsed)
    log.warning("revlog query returned unexpected type: %r", row_type)
    log.error("stats computation failed: %s", exc)
    log.exception("heatmap render failed")   # includes full traceback
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ── constants ─────────────────────────────────────────────────────────────────

_LOG_NAME    = "focusflow"
# The addon package lives at focusflow/utils/logger.py  →  ../../ = addon root
_LOG_FILE    = Path(__file__).parent.parent / "focusflow.log"
_MAX_BYTES   = 512 * 1024   # 512 KB per file
_BACKUP_COUNT = 2            # keep focusflow.log + focusflow.log.1 + .2


# ── setup ─────────────────────────────────────────────────────────────────────

def _build_logger() -> logging.Logger:
    logger = logging.getLogger(_LOG_NAME)
    if logger.handlers:
        # Already configured (e.g. Anki reloaded the addon without restarting)
        return logger

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-8s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Rotating file handler — silently falls back to stderr if the log file
    # cannot be created (e.g. read-only filesystem in some sandboxed setups).
    class _CloseAfterEmit(RotatingFileHandler):
        """Close the log file after every write to avoid WinError 32 on Windows."""
        def emit(self, record: logging.LogRecord) -> None:
            super().emit(record)
            try: self.close()
            except Exception: pass

    try:
        fh = _CloseAfterEmit(
            _LOG_FILE,
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
            delay=True,
        )
        fh.setLevel(logging.WARNING)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        logger.addHandler(sh)

    # PERFORMANCE FIX: this defaulted to DEBUG, and _CloseAfterEmit does a
    # full file open + write + close on every single emit() (needed to
    # avoid WinError 32 on Windows) — so every debug/trace log call paid
    # that cost, for messages that matter only when actively troubleshooting.
    # WARNING+ (the messages that matter for bug reports) still always log;
    # call set_verbose(True) to get DEBUG-level tracing back when needed.
    logger.setLevel(logging.WARNING)
    logger.propagate = False  # don't double-print via Anki's root logger
    return logger


def set_verbose(enabled: bool) -> None:
    """Toggle DEBUG-level tracing on/off at runtime (e.g. from a hidden
    Settings option), without needing a restart."""
    level = logging.DEBUG if enabled else logging.WARNING
    log.setLevel(level)
    for h in log.handlers:
        h.setLevel(level)


# ── public API ────────────────────────────────────────────────────────────────

#: Drop-in replacement for the scattered `print(f"[FocusFlow] ...")` calls.
#: Import this object; do not call _build_logger() directly in other modules.
log: logging.Logger = _build_logger()
