from dataclasses import dataclass, field
from typing import List, Dict, Optional
from src.data.fills_fetcher import CompletedTrade, CurrentPosition
from src.utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class CoinStats:
    coin:           str
    trade_count:    int   = 0
    win_count:      int   = 0
    loss_count:     int   = 0
    win_rate:       float = 0.0
    total_pnl:      float = 0.0
    total_fees:     float = 0.0
    net_pnl:        float = 0.0
    avg_net_pnl:    float = 0.0
    best_trade:     float = 0.0
    worst_trade:    float = 0.0
    avg_roi_pct:    float = 0.0
    avg_duration_hr: float = 0.0
    avg_notional:   float = 0.0
    long_count:     int   = 0
    short_count:    int   = 0
    long_pnl:       float = 0.0
    short_pnl:      float = 0.0


@dataclass
class WalletStats:
    address:         str
    rank:            int
    leaderboard_pnl: float
    leaderboard_roi: float
    account_value:   float

    # Trade summary
    total_trades:    int   = 0
    win_count:       int   = 0
    loss_count:      int   = 0
    win_rate:        float = 0.0
    total_pnl:       float = 0.0
    total_fees:      float = 0.0
    net_pnl:         float = 0.0
    avg_net_pnl:     float = 0.0
    best_trade_pnl:  float = 0.0
    worst_trade_pnl: float = 0.0
    avg_roi_pct:     float = 0.0
    avg_notional:    float = 0.0

    # Duration patterns
    avg_duration_hr:  float = 0.0
    min_duration_hr:  float = 0.0
    max_duration_hr:  float = 0.0
    median_duration_hr: float = 0.0

    # Direction bias
    long_count:   int   = 0
    short_count:  int   = 0
    long_pnl:     float = 0.0
    short_pnl:    float = 0.0
    long_win_rate: float = 0.0
    short_win_rate: float = 0.0

    # Per-coin breakdown
    coin_stats:  Dict[str, CoinStats] = field(default_factory=dict)

    # Best and worst individual trades
    best_trades:  List[CompletedTrade] = field(default_factory=list)
    worst_trades: List[CompletedTrade] = field(default_factory=list)
    all_trades:   List[CompletedTrade] = field(default_factory=list)

    # Currently open positions (not yet closed)
    open_positions: List[CurrentPosition] = field(default_factory=list)
    extended_lookback_used: bool = False   # True if we had to extend the window

    # Patterns
    preferred_coins:    List[str] = field(default_factory=list)
    most_profitable_coin: str = ""
    avg_leverage:       float = 0.0
    profit_factor:      float = 0.0   # gross wins / gross losses


class TradeAnalyser:

    def analyse(self, wallet_info, trades: List[CompletedTrade]) -> WalletStats:
        stats = WalletStats(
            address         = wallet_info.address,
            rank            = wallet_info.rank,
            leaderboard_pnl = wallet_info.pnl,
            leaderboard_roi = wallet_info.roi_pct,
            account_value   = wallet_info.account_value,
            all_trades      = sorted(trades, key=lambda t: t.close_time, reverse=True),
        )

        if not trades:
            return stats

        # ── Core metrics ──────────────────────────────────────────────────────
        stats.total_trades   = len(trades)
        stats.win_count      = sum(1 for t in trades if t.net_pnl > 0)
        stats.loss_count     = sum(1 for t in trades if t.net_pnl <= 0)
        stats.win_rate       = stats.win_count / stats.total_trades * 100
        stats.total_pnl      = sum(t.closed_pnl for t in trades)
        stats.total_fees     = sum(t.fees for t in trades)
        stats.net_pnl        = sum(t.net_pnl for t in trades)
        stats.avg_net_pnl    = stats.net_pnl / stats.total_trades
        stats.best_trade_pnl = max(t.net_pnl for t in trades)
        stats.worst_trade_pnl = min(t.net_pnl for t in trades)
        stats.avg_roi_pct    = sum(t.roi_pct for t in trades) / stats.total_trades
        stats.avg_notional   = sum(t.notional_usd for t in trades) / stats.total_trades

        # ── Duration ──────────────────────────────────────────────────────────
        durations = sorted(t.duration_hrs for t in trades)
        stats.avg_duration_hr    = sum(durations) / len(durations)
        stats.min_duration_hr    = durations[0]
        stats.max_duration_hr    = durations[-1]
        stats.median_duration_hr = durations[len(durations) // 2]

        # ── Direction bias ────────────────────────────────────────────────────
        longs  = [t for t in trades if t.direction == "long"]
        shorts = [t for t in trades if t.direction == "short"]
        stats.long_count  = len(longs)
        stats.short_count = len(shorts)
        stats.long_pnl    = sum(t.net_pnl for t in longs)
        stats.short_pnl   = sum(t.net_pnl for t in shorts)
        stats.long_win_rate  = sum(1 for t in longs  if t.net_pnl > 0) / len(longs)  * 100 if longs  else 0
        stats.short_win_rate = sum(1 for t in shorts if t.net_pnl > 0) / len(shorts) * 100 if shorts else 0

        # ── Profit factor ─────────────────────────────────────────────────────
        gross_win  = sum(t.net_pnl for t in trades if t.net_pnl > 0)
        gross_loss = abs(sum(t.net_pnl for t in trades if t.net_pnl < 0))
        stats.profit_factor = gross_win / gross_loss if gross_loss > 0 else float("inf")

        # ── Per-coin stats ────────────────────────────────────────────────────
        coin_map: Dict[str, List[CompletedTrade]] = {}
        for t in trades:
            coin_map.setdefault(t.coin, []).append(t)

        for coin, coin_trades in coin_map.items():
            cs = CoinStats(coin=coin)
            cs.trade_count    = len(coin_trades)
            cs.win_count      = sum(1 for t in coin_trades if t.net_pnl > 0)
            cs.loss_count     = cs.trade_count - cs.win_count
            cs.win_rate       = cs.win_count / cs.trade_count * 100
            cs.total_pnl      = sum(t.closed_pnl for t in coin_trades)
            cs.total_fees     = sum(t.fees for t in coin_trades)
            cs.net_pnl        = sum(t.net_pnl for t in coin_trades)
            cs.avg_net_pnl    = cs.net_pnl / cs.trade_count
            cs.best_trade     = max(t.net_pnl for t in coin_trades)
            cs.worst_trade    = min(t.net_pnl for t in coin_trades)
            cs.avg_roi_pct    = sum(t.roi_pct for t in coin_trades) / cs.trade_count
            cs.avg_duration_hr = sum(t.duration_hrs for t in coin_trades) / cs.trade_count
            cs.avg_notional   = sum(t.notional_usd for t in coin_trades) / cs.trade_count
            cs.long_count     = sum(1 for t in coin_trades if t.direction == "long")
            cs.short_count    = sum(1 for t in coin_trades if t.direction == "short")
            cs.long_pnl       = sum(t.net_pnl for t in coin_trades if t.direction == "long")
            cs.short_pnl      = sum(t.net_pnl for t in coin_trades if t.direction == "short")
            stats.coin_stats[coin] = cs

        # ── Preferred coins (by trade count) ──────────────────────────────────
        stats.preferred_coins = sorted(
            stats.coin_stats.keys(),
            key=lambda c: stats.coin_stats[c].trade_count,
            reverse=True
        )[:5]

        # ── Most profitable coin ──────────────────────────────────────────────
        if stats.coin_stats:
            stats.most_profitable_coin = max(
                stats.coin_stats.keys(),
                key=lambda c: stats.coin_stats[c].net_pnl
            )

        # ── Best and worst trades ─────────────────────────────────────────────
        sorted_by_pnl = sorted(trades, key=lambda t: t.net_pnl, reverse=True)
        stats.best_trades  = sorted_by_pnl[:5]
        stats.worst_trades = sorted_by_pnl[-5:][::-1]

        return stats
