import logging
import os
from datetime import datetime

LOG_DIR  = os.path.join(os.path.dirname(__file__), "..", "logs")
LOG_FILE = os.path.join(LOG_DIR, "bot.log")
os.makedirs(LOG_DIR, exist_ok=True)

RESET  = "\x1b[0m"; GREEN = "\x1b[32m"; YELLOW = "\x1b[33m"
RED    = "\x1b[31m"; CYAN  = "\x1b[36m"; GREY   = "\x1b[90m"
COLOURS = {"DEBUG": GREY, "INFO": CYAN, "WARNING": YELLOW, "ERROR": RED, "CRITICAL": RED}

class ColouredFormatter(logging.Formatter):
    def format(self, record):
        c  = COLOURS.get(record.levelname, RESET)
        ts = datetime.utcnow().strftime("%H:%M:%S")
        return f"{GREY}[{ts}]{RESET} {c}{record.levelname:<7}{RESET} {record.getMessage()}"

class PlainFormatter(logging.Formatter):
    def format(self, record):
        ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        return f"[{ts}] {record.levelname:<7} {record.getMessage()}"

def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    ch = logging.StreamHandler(); ch.setFormatter(ColouredFormatter()); logger.addHandler(ch)
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8"); fh.setFormatter(PlainFormatter()); logger.addHandler(fh)
    return logger
