import os
from dotenv import load_dotenv

load_dotenv()

def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise EnvironmentError(f"Missing required env var: {key}")
    return val

def _float(key: str, default: float) -> float:
    return float(os.getenv(key, str(default)))

def _int(key: str, default: int) -> int:
    return int(os.getenv(key, str(default)))

def _bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).lower() == "true"


class Config:
    # ── Wallet ────────────────────────────────────────────────────
    PRIVATE_KEY:     str = _require("PRIVATE_KEY")
    WALLET_ADDRESS:  str = os.getenv("WALLET_ADDRESS", "")

    # ── Trade ─────────────────────────────────────────────────────
    TRADE_SIZE_USD:        float = _float("TRADE_SIZE_USD", 20.0)
    LEVERAGE:              int   = _int  ("LEVERAGE", 1)
    MAX_CONCURRENT_TRADES: int   = _int  ("MAX_CONCURRENT_TRADES", 2)

    # ── Momentum detection ────────────────────────────────────────
    OBSERVATION_SECONDS: int   = _int  ("OBSERVATION_SECONDS", 10)
    MIN_MOMENTUM_PCT:    float = _float("MIN_MOMENTUM_PCT", 0.5)
    MIN_VOLUME_USD:      float = _float("MIN_VOLUME_USD", 5000.0)

    # ── Exit ──────────────────────────────────────────────────────
    TAKE_PROFIT_PCT:    float = _float("TAKE_PROFIT_PCT", 3.0)
    STOP_LOSS_PCT:      float = _float("STOP_LOSS_PCT", 1.5)
    TIME_STOP_MINUTES:  int   = _int  ("TIME_STOP_MINUTES", 10)
    TRAILING_STOP_PCT:  float = _float("TRAILING_STOP_PCT", 1.0)

    # ── Mode ──────────────────────────────────────────────────────
    PAPER_TRADE: bool = _bool("PAPER_TRADE", True)

    # ── Telegram ──────────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID:   str = os.getenv("TELEGRAM_CHAT_ID", "")


config = Config()
