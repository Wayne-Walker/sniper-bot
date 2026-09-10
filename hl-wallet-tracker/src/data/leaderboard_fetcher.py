import requests
from dataclasses import dataclass
from typing import List
from src.config import config
from src.utils.logger import get_logger

log = get_logger(__name__)

@dataclass
class WalletInfo:
    address:       str
    pnl:           float
    roi_pct:       float
    account_value: float
    volume:        float
    rank:          int


class LeaderboardFetcher:

    def fetch_top_wallets(self, n: int = None, window: str = None) -> List[WalletInfo]:
        n      = n      or config.LEADERBOARD_TOP_N
        window = window or config.LEADERBOARD_WINDOW

        log.info(f"Fetching top {n} wallets from leaderboard [{window}]...")
        resp = requests.get(config.HL_LEADERBOARD_URL, timeout=15)
        resp.raise_for_status()
        rows = resp.json().get("leaderboardRows", [])

        # Sort by PnL for chosen window
        def get_pnl(row):
            for perf in row.get("windowPerformances", []):
                if perf[0] == window:
                    return float(perf[1].get("pnl", 0))
            return float(row.get("pnl", 0))

        def get_roi(row):
            for perf in row.get("windowPerformances", []):
                if perf[0] == window:
                    return float(perf[1].get("roi", 0)) * 100
            return 0.0

        def get_vlm(row):
            for perf in row.get("windowPerformances", []):
                if perf[0] == window:
                    return float(perf[1].get("vlm", 0))
            return 0.0

        sorted_rows = sorted(rows, key=get_pnl, reverse=True)
        result = []
        for i, row in enumerate(sorted_rows[:n]):
            result.append(WalletInfo(
                address       = row.get("ethAddress", row.get("user", "")),
                pnl           = get_pnl(row),
                roi_pct       = get_roi(row),
                account_value = float(row.get("accountValue", 0)),
                volume        = get_vlm(row),
                rank          = i + 1,
            ))

        log.info(f"Top wallet PnL: ${result[0].pnl:,.0f} | #{n} wallet PnL: ${result[-1].pnl:,.0f}")
        return result
