import os
from dotenv import load_dotenv

load_dotenv()

def _int(k, d): return int(os.getenv(k, str(d)))
def _float(k, d): return float(os.getenv(k, str(d)))
def _bool(k, d): return os.getenv(k, str(d)).lower() == "true"

class Config:
    # Wallets
    WATCH_WALLETS: list = [
        w.strip() for w in os.getenv("WATCH_WALLETS", "").split(",") if w.strip()
    ]
    LEADERBOARD_TOP_N:  int = _int  ("LEADERBOARD_TOP_N",  10)
    LEADERBOARD_WINDOW: str = os.getenv("LEADERBOARD_WINDOW", "allTime")

    # History
    LOOKBACK_DAYS:          int   = _int  ("LOOKBACK_DAYS",          30)
    MIN_TRADE_PNL:          float = _float("MIN_TRADE_PNL",          -999999)
    MAX_CONCURRENT_FETCHES: int   = _int  ("MAX_CONCURRENT_FETCHES", 5)

    # Report
    SAVE_REPORTS: bool = _bool("SAVE_REPORTS", True)

    # Telegram
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID:   str = os.getenv("TELEGRAM_CHAT_ID",   "")

    # API
    HL_INFO_URL:        str = "https://api.hyperliquid.xyz/info"
    HL_LEADERBOARD_URL: str = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"

config = Config()
