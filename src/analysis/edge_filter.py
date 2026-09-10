"""
EdgeFilter
──────────
Bridges the Smart Money analysis and any trading bot.

Logic:
  1. Run a full smart money scan (leaderboard → positions → divergence)
  2. For each coin with a DIVERGENCE signal, compute:

       gap_pct = |winner_net_notional - loser_net_notional|
                 ─────────────────────────────────────────── × 100
                 |winner_net_notional| + |loser_net_notional|

  3. Only emit a TradeSignal if gap_pct >= EDGE_THRESHOLD (default 50%)

The signal can be consumed by any downstream bot (momentum, copy-trade etc.)
or used as a standalone CLI to list current high-conviction opportunities.
"""

import time
import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import List, Optional

from src.config import config
from src.data.leaderboard_fetcher import LeaderboardFetcher
from src.data.position_fetcher import PositionFetcher
from src.analysis.divergence_engine import DivergenceEngine, CoinSentiment, MarketAnalysis
from src.utils.logger import get_logger
from src.utils.telegram import send_alert

log = get_logger(__name__)

# ── Signal output ─────────────────────────────────────────────────────────────

@dataclass
class TradeSignal:
    coin:           str
    direction:      str    # "long" or "short"
    gap_pct:        float  # divergence gap — the edge strength (0–100)
    mark_price:     float

    # Supporting data
    winner_net_notional: float
    loser_net_notional:  float
    winner_long_pct:     float   # % of winner wallets that are long
    loser_long_pct:      float
    winner_avg_leverage: float
    loser_avg_leverage:  float
    net_sentiment:       str
    signal_type:         str
    generated_at:        str     # ISO timestamp

    # Human-readable summary
    @property
    def summary(self) -> str:
        return (
            f"${self.coin}  {self.direction.upper()}  "
            f"gap={self.gap_pct:.1f}%  "
            f"mark=${self.mark_price:,.4f}  "
            f"[{self.net_sentiment}]"
        )

    def to_dict(self) -> dict:
        return asdict(self)


# ── Edge Filter ───────────────────────────────────────────────────────────────

class EdgeFilter:
    """
    Runs a full smart money scan and returns only the coins where
    the smart/dumb money gap exceeds the configured threshold.

    Usage:
        ef      = EdgeFilter(threshold=50.0)
        signals = ef.scan()
        for s in signals:
            print(s.summary)
    """

    def __init__(self, threshold: float = None) -> None:
        self.threshold   = threshold or config.EDGE_THRESHOLD
        self.leaderboard = LeaderboardFetcher()
        self.positions   = PositionFetcher()
        self.engine      = DivergenceEngine()
        # Resolve at init time so os.getcwd() reflects where main.py was launched from
        self.signals_dir = os.path.join(os.getcwd(), "signals")
        os.makedirs(self.signals_dir, exist_ok=True)
        log.info(f"Signals directory: {self.signals_dir}")

        # Cached analysis — refreshed on each scan() call
        self._last_analysis:  Optional[MarketAnalysis] = None
        self._last_scan_time: Optional[float]          = None

    def scan(self, coin: str = None) -> List[TradeSignal]:
        """
        Run a full scan and return signals above threshold.
        If coin is provided, return signal for that coin only (or empty list).
        """
        log.info(
            f"EdgeFilter scan — threshold: {self.threshold}%  "
            f"{'coin: ' + coin if coin else 'all coins'}"
        )

        # ── Fetch data ────────────────────────────────────────────────────────
        winners, losers = self.leaderboard.fetch()
        all_wallets     = winners + losers
        all_positions   = self.positions.fetch_all(all_wallets)

        # ── Analyse ───────────────────────────────────────────────────────────
        if coin:
            sentiment = self.engine.analyse_coin(coin, all_positions)
            analysis  = None
            sentiments = [sentiment] if sentiment else []
        else:
            analysis   = self.engine.analyse(all_positions)
            self._last_analysis  = analysis
            self._last_scan_time = time.time()
            sentiments = list(analysis.coins.values())

        # ── Filter by threshold ───────────────────────────────────────────────
        signals = []
        for s in sentiments:
            signal = self._evaluate(s)
            if signal:
                signals.append(signal)
                log.info(
                    f"  ✅ SIGNAL: {signal.summary}"
                )
            else:
                reason = (
                    f"gap={s.gap_pct:.1f}% < {self.threshold}%"
                    if s.signal_type == "DIVERGENCE"
                    else f"signal_type={s.signal_type}"
                )
                log.info(f"  ✗  ${s.coin:<8}  FILTERED  ({reason})")

        log.info(
            f"Scan complete — {len(sentiments)} coins analysed, "
            f"{len(signals)} signals above {self.threshold}% threshold"
        )

        if signals:
            self._save_signals(signals)
            self._alert(signals)

        return signals

    def get_signal(self, coin: str) -> Optional[TradeSignal]:
        """Get signal for a single coin. Returns None if below threshold."""
        results = self.scan(coin=coin)
        return results[0] if results else None

    def is_tradeable(self, coin: str, direction: str) -> tuple[bool, str]:
        """
        Gate check for a trading bot.
        Returns (True, reason) if trade is allowed, (False, reason) if blocked.

        Usage in momentum bot:
            ok, reason = edge_filter.is_tradeable("SOL", "long")
            if not ok:
                log.info(f"Trade blocked: {reason}")
                return
        """
        signal = self.get_signal(coin)

        if signal is None:
            return False, f"No signal for ${coin} above {self.threshold}% threshold"

        if signal.direction != direction:
            return False, (
                f"Direction mismatch — smart money says {signal.direction.upper()} "
                f"but bot wants to go {direction.upper()}"
            )

        return True, (
            f"✅ Edge confirmed — gap={signal.gap_pct:.1f}%  "
            f"direction={signal.direction.upper()}  "
            f"sentiment={signal.net_sentiment}"
        )

    # ── Private ───────────────────────────────────────────────────────────────

    def _evaluate(self, s: CoinSentiment) -> Optional[TradeSignal]:
        """Convert a CoinSentiment into a TradeSignal if it passes the threshold."""
        if s.signal_type != "DIVERGENCE":
            return None

        if s.gap_pct < self.threshold:
            return None

        direction = "long" if s.divergence_score > 0 else "short"

        return TradeSignal(
            coin                = s.coin,
            direction           = direction,
            gap_pct             = s.gap_pct,
            mark_price          = s.mark_price,
            winner_net_notional = s.winner_long_notional - s.winner_short_notional,
            loser_net_notional  = s.loser_long_notional  - s.loser_short_notional,
            winner_long_pct     = s.winner_long_pct,
            loser_long_pct      = s.loser_long_pct,
            winner_avg_leverage = s.winner_avg_leverage,
            loser_avg_leverage  = s.loser_avg_leverage,
            net_sentiment       = s.net_sentiment,
            signal_type         = s.signal_type,
            generated_at        = datetime.now(timezone.utc).isoformat(),
        )

    def _save_signals(self, signals: List[TradeSignal]) -> None:
        """Save signals to JSON + TXT files."""
        os.makedirs(self.signals_dir, exist_ok=True)
        ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
        now_str   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        # ── JSON ──────────────────────────────────────────────────────────────
        json_fn     = os.path.join(self.signals_dir, f"signals_{ts}.json")
        latest_json = os.path.join(self.signals_dir, "latest.json")
        payload = {
            "generated_at":  datetime.now(timezone.utc).isoformat(),
            "threshold_pct": self.threshold,
            "signal_count":  len(signals),
            "signals":       [s.to_dict() for s in signals],
        }
        for path in (json_fn, latest_json):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)

        # ── TXT ───────────────────────────────────────────────────────────────
        txt_fn     = os.path.join(self.signals_dir, f"signals_{ts}.txt")
        latest_txt = os.path.join(self.signals_dir, "latest.txt")
        txt        = self._format_txt(signals, now_str)
        for path in (txt_fn, latest_txt):
            with open(path, "w", encoding="utf-8") as f:
                f.write(txt)

        log.info(f"Signals saved → {json_fn}")
        log.info(f"Signals saved → {txt_fn}")
        log.info(f"Latest JSON   → {latest_json}")
        log.info(f"Latest TXT    → {latest_txt}")

    def _format_txt(self, signals: List[TradeSignal], now_str: str) -> str:
        W    = 65
        SEP  = "═" * W
        SEP2 = "─" * W
        SEP3 = "·" * W

        lines = [
            "", SEP,
            "  HYPERLIQUID EDGE FILTER — TRADE SIGNALS".center(W),
            f"  Generated : {now_str}".center(W),
            f"  Threshold : {self.threshold}% gap minimum".center(W),
            f"  Signals   : {len(signals)} qualifying coin(s)".center(W),
            SEP, "",
        ]

        for i, s in enumerate(signals, 1):
            arrow = "▲ LONG" if s.direction == "long" else "▼ SHORT"
            lines += [
                f"  [{i}]  ${s.coin}  —  {arrow}  —  {s.net_sentiment}",
                SEP2,
                f"  Gap Score    : {s.gap_pct:>6.1f}%   (threshold: {self.threshold}%)",
                f"  Mark Price   : ${s.mark_price:>12,.4f}",
                "",
                f"  SMART MONEY  : {s.winner_long_pct:>5.1f}% wallets long",
                f"  Net Notional : ${s.winner_net_notional:>+12,.0f}",
                f"  Avg Leverage : {s.winner_avg_leverage:>5.1f}x",
                "",
                f"  DUMB MONEY   : {s.loser_long_pct:>5.1f}% wallets long",
                f"  Net Notional : ${s.loser_net_notional:>+12,.0f}",
                f"  Avg Leverage : {s.loser_avg_leverage:>5.1f}x",
                "",
                SEP3,
                f"  TRADE PLAN   : {s.direction.upper()} ${s.coin} @ ${s.mark_price:,.4f}",
                f"  EDGE         : Smart money {('LONG' if s.winner_net_notional > 0 else 'SHORT')} "
                f"vs Dumb money {('LONG' if s.loser_net_notional > 0 else 'SHORT')}",
                f"  GENERATED    : {s.generated_at}",
                "",
            ]

        lines += [SEP, "  END OF SIGNALS REPORT".center(W), SEP, ""]
        return "\n".join(lines)

    def _alert(self, signals: List[TradeSignal]) -> None:
        """Send Telegram alert for new signals."""
        if not signals:
            return
        lines = [f"⚡ *EdgeFilter — {len(signals)} signal(s) above {self.threshold}% threshold*\n"]
        for s in signals:
            arrow = "🟢 LONG" if s.direction == "long" else "🔴 SHORT"
            lines.append(
                f"{arrow} `${s.coin}`\n"
                f"  Gap: `{s.gap_pct:.1f}%`  |  Mark: `${s.mark_price:,.4f}`\n"
                f"  Smart: `{s.winner_long_pct:.0f}%` long  |  "
                f"Dumb: `{s.loser_long_pct:.0f}%` long\n"
                f"  {s.net_sentiment}\n"
            )
        send_alert("\n".join(lines))
