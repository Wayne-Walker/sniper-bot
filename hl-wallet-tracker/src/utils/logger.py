import logging, os
from datetime import datetime

LOG_DIR  = os.path.join(os.path.dirname(__file__), "..", "logs")
LOG_FILE = os.path.join(LOG_DIR, "tracker.log")
os.makedirs(LOG_DIR, exist_ok=True)

RESET="\x1b[0m"; GREEN="\x1b[32m"; YELLOW="\x1b[33m"; RED="\x1b[31m"; CYAN="\x1b[36m"; GREY="\x1b[90m"
COLOURS={"DEBUG":GREY,"INFO":CYAN,"WARNING":YELLOW,"ERROR":RED,"CRITICAL":RED}

class CF(logging.Formatter):
    def format(self,r):
        c=COLOURS.get(r.levelname,RESET); ts=datetime.utcnow().strftime("%H:%M:%S")
        return f"{GREY}[{ts}]{RESET} {c}{r.levelname:<7}{RESET} {r.getMessage()}"

class PF(logging.Formatter):
    def format(self,r):
        ts=datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        return f"[{ts}] {r.levelname:<7} {r.getMessage()}"

def get_logger(name):
    lg=logging.getLogger(name)
    if lg.handlers: return lg
    lg.setLevel(logging.DEBUG)
    ch=logging.StreamHandler(); ch.setFormatter(CF()); lg.addHandler(ch)
    fh=logging.FileHandler(LOG_FILE,encoding="utf-8"); fh.setFormatter(PF()); lg.addHandler(fh)
    return lg
