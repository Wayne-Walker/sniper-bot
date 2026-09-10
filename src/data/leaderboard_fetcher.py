import requests
from dataclasses import dataclass
from typing import List
from src.config import config
from src.utils.logger import get_logger

log = get_logger(__name__)

@dataclass
class WalletRank:
    address:       str
    pnl:           float   # all-time or windowed PnL in USD
    roi_pct:       float
    account_value: float
    volume:        float
    rank:          int
    group:         str     # "winner" or "loser"


class LeaderboardFetcher:
    """
    Fetches the Hyperliquid public leaderboard and returns
    top N winners and top N losers as WalletRank objects.
    """

    def fetch(self) -> tuple[List[WalletRank], List[WalletRank]]:
        log.info(f"Fetching leaderboard [{config.LEADERBOARD_WINDOW}]...")
        raw = self._get_leaderboard()

        # Filter by minimum account value
        filtered = [
            r for r in raw
            if float(r.get("accountValue", 0)) >= config.MIN_ACCOUNT_VALUE
        ]

        log.info(f"Leaderboard: {len(raw)} total wallets, {len(filtered)} above ${config.MIN_ACCOUNT_VALUE:,.0f}")

        # Sort by PnL for the chosen window
        window_key = self._window_key()
        sorted_by_pnl = sorted(
            filtered,
            key=lambda x: float(x.get(window_key, 0)),
            reverse=True
        )

        winners = self._to_wallet_ranks(sorted_by_pnl[:config.TOP_N_WALLETS],  "winner")
        losers  = self._to_wallet_ranks(sorted_by_pnl[-config.TOP_N_WALLETS:], "loser")
        losers  = list(reversed(losers))   # worst first

        log.info(
            f"Winners top PnL: ${winners[0].pnl:,.0f} | "
            f"Losers worst PnL: ${losers[0].pnl:,.0f}"
        )
        return winners, losers

    # ── Private ───────────────────────────────────────────────────────────────

    def _get_leaderboard(self) -> list:
        resp = requests.get(config.HL_LEADERBOARD_URL, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        # Response is {"leaderboardRows": [...]}
        return data.get("leaderboardRows", [])

    def _window_key(self) -> str:
        mapping = {
            "day":     "pnl",        # daily PnL field
            "week":    "pnl",
            "month":   "pnl",
            "allTime": "pnl",
        }
        # The leaderboard rows contain a "windowPerformances" list:
        # [["day", {...}], ["week", {...}], ["month", {...}], ["allTime", {...}]]
        # We handle this in _to_wallet_ranks
        return "pnl"

    def _to_wallet_ranks(self, rows: list, group: str) -> List[WalletRank]:
        result = []
        for i, row in enumerate(rows):
            # Extract PnL for chosen window from windowPerformances
            pnl   = self._extract_pnl(row)
            roi   = self._extract_roi(row)
            vlm   = self._extract_volume(row)

            result.append(WalletRank(
                address       = row.get("ethAddress", row.get("user", "")),
                pnl           = pnl,
                roi_pct       = roi,
                account_value = float(row.get("accountValue", 0)),
                volume        = vlm,
                rank          = i + 1,
                group         = group,
            ))
        return result

    def _extract_pnl(self, row: dict) -> float:
        window = config.LEADERBOARD_WINDOW
        for perf in row.get("windowPerformances", []):
            if perf[0] == window:
                return float(perf[1].get("pnl", 0))
        # Fallback to top-level
        return float(row.get("pnl", 0))

    def _extract_roi(self, row: dict) -> float:
        window = config.LEADERBOARD_WINDOW
        for perf in row.get("windowPerformances", []):
            if perf[0] == window:
                return float(perf[1].get("roi", 0)) * 100
        return 0.0

    def _extract_volume(self, row: dict) -> float:
        window = config.LEADERBOARD_WINDOW
        for perf in row.get("windowPerformances", []):
            if perf[0] == window:
                return float(perf[1].get("vlm", 0))
        return 0.0
