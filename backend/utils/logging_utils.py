"""
Logging Utilities
Provides a consistent, coloured, timestamped logger for every module.
"""

from __future__ import annotations

import logging
import sys

# ── Format ────────────────────────────────────────────────────────────────────

LOG_FORMAT  = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
LOG_LEVEL   = logging.DEBUG   # Change to INFO for production quietness

# ── Colour map (ANSI escape codes) ───────────────────────────────────────────

COLOURS = {
    "DEBUG":    "\033[36m",   # cyan
    "INFO":     "\033[32m",   # green
    "WARNING":  "\033[33m",   # yellow
    "ERROR":    "\033[31m",   # red
    "CRITICAL": "\033[41m",   # red background
}
RESET = "\033[0m"


class _ColouredFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        colour = COLOURS.get(record.levelname, "")
        record.levelname = f"{colour}{record.levelname}{RESET}"
        return super().format(record)


# ── Root handler (set up once) ────────────────────────────────────────────────

def _setup_root_logger() -> None:
    root = logging.getLogger()
    if root.handlers:
        return  # already configured

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_ColouredFormatter(fmt=LOG_FORMAT, datefmt=DATE_FORMAT))
    root.addHandler(handler)
    root.setLevel(LOG_LEVEL)

    # Quieten noisy third-party libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("multipart").setLevel(logging.WARNING)
    logging.getLogger("PIL").setLevel(logging.WARNING)


_setup_root_logger()


# ── Public interface ──────────────────────────────────────────────────────────

def get_logger(name: str) -> logging.Logger:
    """
    Return a named logger.  Usage:

        from utils.logging_utils import get_logger
        logger = get_logger(__name__)
        logger.info("Hello from %s", __name__)
    """
    return logging.getLogger(name)
