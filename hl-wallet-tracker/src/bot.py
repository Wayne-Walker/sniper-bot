import os
from typing import List, Optional
from src.config import config
from src.data.leaderboard_fetcher import LeaderboardFetcher, WalletInfo
from src.data.fills_fetcher import FillsFetcher
from src.analysis.trade_analyser import TradeAnalyser, WalletStats
from src.report.report_generator import ReportGenerator
from src.utils.logger import get_logger

log = get_logger(__name__)


class WalletTrackerBot:

    def __init__(self) -> None:
        self.leaderboard = LeaderboardFetcher()
        self.fills       = FillsFetcher()
        self.analyser    = TradeAnalyser()
        self.reporter    = ReportGenerator()
        self._wallets:   Optional[List[WalletInfo]] = None
        self._stats:     Optional[List[WalletStats]] = None

    def run_all(self) -> str:
        """Full report — all tracked wallets."""
        self._load()
        report   = self.reporter.summary_report(self._stats)
        csv_path = self.reporter.save_csv(self._stats)
        print(report)
        print(f"\n  ✅ Files saved to reports/")
        print(f"     TXT → wallet_tracker_*.txt")
        print(f"     CSV → trades_*.csv          (closed trades)")
        print(f"     CSV → open_positions_*.csv  (currently open)\n")
        return report

    def run_wallet(self, address: str) -> str:
        """Deep dive for a specific wallet address."""
        self._load()
        match = next((s for s in self._stats if s.address.lower() == address.lower()), None)
        if not match:
            match = next((s for s in self._stats if address.lower() in s.address.lower()), None)
        if not match:
            print(f"  Wallet {address} not found. Run 'wallets' to see tracked addresses.")
            return ""
        report = self.reporter.wallet_report(match)
        print(report)
        print(f"\n  ✅ Report saved to reports/\n")
        return report

    def run_coin(self, coin: str) -> str:
        """Show all trades for a specific coin across all tracked wallets."""
        self._load()
        coin = coin.upper()
        lines = [
            f"\n  ALL {coin} TRADES ACROSS TRACKED WALLETS",
            "─" * 72,
            f"  {'WALLET':<13} {'DIR':<8} {'OPEN':<17} {'CLOSE':<17} {'DUR':>6} "
            f"{'NOTIONAL':>12} {'ENTRY':>11} {'EXIT':>11} {'PNL':>12} {'ROI':>7}",
            "·" * 72,
        ]
        found = 0
        for s in self._stats:
            coin_trades = [t for t in s.all_trades if t.coin.upper() == coin]
            for t in sorted(coin_trades, key=lambda x: x.close_time, reverse=True):
                from src.report.report_generator import _dur, _dir
                lines.append(
                    f"  {s.address[:11]}..  "
                    f"{_dir(t.direction):<8} "
                    f"{t.open_date:<17} {t.close_date:<17} "
                    f"{_dur(t.duration_hrs):>6}  "
                    f"${t.notional_usd:>10,.0f}  "
                    f"${t.open_price:>9,.4f}  "
                    f"${t.close_price:>9,.4f}  "
                    f"${t.net_pnl:>+10,.2f}  "
                    f"{t.roi_pct:>+6.2f}%"
                )
                found += 1
        if found == 0:
            lines.append(f"  No completed {coin} trades found.")
        output = "\n".join(lines)
        print(output)
        # Save to file
        saved = self.reporter._save_txt(output, f"coin_{coin}")
        print(f"\n  ✅ Report saved → {saved}\n")
        return output

    def list_wallets(self) -> None:
        """Print the tracked wallet list."""
        self._load()
        print(f"\n  TRACKED WALLETS  ({len(self._wallets)} total)")
        print("─" * 60)
        for w in self._wallets:
            print(f"  #{w.rank:<3} {w.address}  PnL: ${w.pnl:>+12,.0f}  ROI: {w.roi_pct:>+7.1f}%")

    def refresh(self) -> None:
        self._wallets = None
        self._stats   = None
        log.info("Cache cleared — will re-fetch on next command.")

    # ── Private ───────────────────────────────────────────────────────────────

    def _load(self) -> None:
        if self._stats is not None:
            return

        # Resolve wallet list
        if config.WATCH_WALLETS:
            log.info(f"Using {len(config.WATCH_WALLETS)} manually configured wallets")
            self._wallets = [
                WalletInfo(address=a, pnl=0, roi_pct=0, account_value=0, volume=0, rank=i+1)
                for i, a in enumerate(config.WATCH_WALLETS)
            ]
        else:
            self._wallets = self.leaderboard.fetch_top_wallets()

        # Fetch completed trades and current open positions concurrently
        fills_by_wallet = self.fills.fetch_all(self._wallets)
        open_by_wallet  = self.fills.fetch_open_positions(self._wallets)

        self._stats = []
        for w in self._wallets:
            trades = fills_by_wallet.get(w.address, [])

            # Auto-extend lookback if no closed trades found but wallet has open positions
            extended = False
            if not trades and open_by_wallet.get(w.address):
                log.info(
                    f"{w.address[:10]}... has open positions but no closed trades "
                    f"in {config.LOOKBACK_DAYS}d — extending lookback 3x"
                )
                trades   = self.fills.fetch_with_extended_lookback(w, multiplier=3)
                extended = True

            stats = self.analyser.analyse(w, trades)
            stats.open_positions          = open_by_wallet.get(w.address, [])
            stats.extended_lookback_used  = extended
            self._stats.append(stats)

        self._stats.sort(key=lambda s: s.net_pnl, reverse=True)
        total_closed = sum(s.total_trades for s in self._stats)
        total_open   = sum(len(s.open_positions) for s in self._stats)
        log.info(f"Analysis complete — {total_closed} closed trades | {total_open} open positions")
