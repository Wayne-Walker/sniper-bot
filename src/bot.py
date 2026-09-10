from typing import List, Optional
from src.data.leaderboard_fetcher import LeaderboardFetcher, WalletRank
from src.data.position_fetcher import PositionFetcher, OpenPosition
from src.analysis.divergence_engine import DivergenceEngine
from src.report.report_generator import ReportGenerator
from src.utils.logger import get_logger
from src.utils.telegram import send_alert

log = get_logger(__name__)


class SmartMoneyBot:
    """
    Orchestrates the full pipeline:
    1. Fetch leaderboard (winners + losers)
    2. Fetch open positions for all wallets
    3. Run divergence analysis
    4. Generate and print report
    """

    def __init__(self) -> None:
        self.leaderboard = LeaderboardFetcher()
        self.positions   = PositionFetcher()
        self.engine      = DivergenceEngine()
        self.reporter    = ReportGenerator()
        # Cached data — reused across coin-specific requests
        self._winners:          Optional[List[WalletRank]]  = None
        self._losers:           Optional[List[WalletRank]]  = None
        self._all_positions:    Optional[List[OpenPosition]] = None

    def run_global(self) -> str:
        """Full market scan — winners vs losers across all coins."""
        log.info("Running full global market analysis...")
        self._load_data()
        analysis = self.engine.analyse(self._all_positions)
        report   = self.reporter.global_report(analysis, self._winners, self._losers)
        print(report)
        send_alert(f"📊 *Global Sentiment Report Generated*\n{len(analysis.top_divergences)} divergence signals found.")
        return report

    def run_coin(self, coin: str) -> str:
        """Targeted analysis for a specific coin."""
        coin = coin.upper().replace("$", "").strip()
        log.info(f"Running analysis for ${coin}...")
        self._load_data()
        sentiment = self.engine.analyse_coin(coin, self._all_positions)
        report    = self.reporter.coin_report(coin, sentiment, self._winners, self._losers)
        print(report)
        return report

    def refresh(self) -> None:
        """Force re-fetch of leaderboard and positions."""
        log.info("Refreshing all data...")
        self._winners       = None
        self._losers        = None
        self._all_positions = None
        self._load_data()
        log.info("Data refreshed.")

    # ── Private ───────────────────────────────────────────────────────────────

    def _load_data(self) -> None:
        """Lazy-load and cache leaderboard + positions."""
        if self._all_positions is not None:
            return  # Already loaded

        self._winners, self._losers = self.leaderboard.fetch()
        all_wallets = self._winners + self._losers
        self._all_positions = self.positions.fetch_all(all_wallets)
