import logging
import os
from datetime import datetime

LOG_DIR  = os.path.join(os.path.dirname(__file__), "..", "..", "logs")
LOG_FILE = os.path.join(LOG_DIR, "momentum.log")

os.makedirs(LOG_DIR, exist_ok=True)

# ── Colour codes ──────────────────────────────────────────────────────────────
RESET  = "\x1b[0m"
GREEN  = "\x1b[32m"
YELLOW = "\x1b[33m"
RED    = "\x1b[31m"
CYAN   = "\x1b[36m"
GREY   = "\x1b[90m"

COLOURS = {
    "DEBUG":    GREY,
    "INFO":     CYAN,
    "WARNING":  YELLOW,
    "ERROR":    RED,
    "CRITICAL": RED,
}

class ColouredFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        colour = COLOURS.get(record.levelname, RESET)
        ts     = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        return f"{GREY}[{ts}]{RESET} {colour}{record.levelname:<7}{RESET} {record.getMessage()}"

class PlainFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        return f"[{ts}] {record.levelname:<7} {record.getMessage()}"

def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # already configured

    logger.setLevel(logging.DEBUG)

    # Console handler — coloured
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    ch.setFormatter(ColouredFormatter())
    logger.addHandler(ch)

    # File handler — plain text
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(PlainFormatter())
    logger.addHandler(fh)

    return logger
