import os
from dotenv import load_dotenv

load_dotenv()

def _int(key: str, default: int) -> int:
    return int(os.getenv(key, str(default)))

def _bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).lower() == "true"

class Config:
    TOP_N_WALLETS:         int   = _int ("TOP_N_WALLETS", 20)
    MIN_ACCOUNT_VALUE:     float = float(os.getenv("MIN_ACCOUNT_VALUE", "10000"))
    LEADERBOARD_WINDOW:    str   = os.getenv("LEADERBOARD_WINDOW", "allTime")
    MAX_CONCURRENT_FETCHES: int  = _int ("MAX_CONCURRENT_FETCHES", 8)
    SAVE_REPORTS:          bool  = _bool("SAVE_REPORTS", True)
    AUTO_RUN_ON_START:     bool  = _bool("AUTO_RUN_ON_START", False)

    # ── Edge Filter ───────────────────────────────────────────────────────────
    # Minimum gap % between smart notional and dumb notional to allow a trade
    # gap_pct = |winner_net - loser_net| / (|winner_net| + |loser_net|) × 100
    EDGE_THRESHOLD:        float = float(os.getenv("EDGE_THRESHOLD", "50.0"))

    # ── Liquidation monitor ───────────────────────────────────────
    # Hyperliquid exposes liquidations only per-wallet, so the monitor
    # watches the leaderboard's top winners + biggest losers and alerts
    # when one of those wallets is liquidated above the threshold.
    # Minimum (aggregated) notional USD to trigger a Telegram alert
    MIN_LIQUIDATION_USD:   float = float(os.getenv("MIN_LIQUIDATION_USD", "100000"))
    # Which leaderboard group(s) to watch: "both" | "winners" | "losers"
    LIQ_WATCH_GROUP:       str   = os.getenv("LIQ_WATCH_GROUP", "both").lower()
    # How often to refetch the leaderboard and refresh the watchlist (hours)
    LIQ_REFRESH_HOURS:     float = float(os.getenv("LIQ_REFRESH_HOURS", "6"))
    TELEGRAM_BOT_TOKEN:    str   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID:      str   = os.getenv("TELEGRAM_CHAT_ID", "")

    HL_INFO_URL:       str = "https://api.hyperliquid.xyz/info"
    HL_LEADERBOARD_URL: str = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"

config = Config()
