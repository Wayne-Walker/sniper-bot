from dataclasses import dataclass, field
from typing import List, Dict, Optional
from src.data.position_fetcher import OpenPosition
from src.utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class PriceCluster:
    price_level:    float
    wallet_count:   int
    total_notional: float
    direction:      str     # "long", "short", "mixed"
    group:          str     # "winner" or "loser"
    pct_of_group:   float   # % of group total notional


@dataclass
class WalletPosition:
    address:        str
    group:          str
    direction:      str
    notional_usd:   float
    entry_price:    float
    leverage:       float
    unrealized_pnl: float
    roi_pct:        float
    wallet_pnl:     float


@dataclass
class CoinSentiment:
    coin: str

    # Winner stats
    winner_long_count:       int   = 0
    winner_short_count:      int   = 0
    winner_long_notional:    float = 0.0
    winner_short_notional:   float = 0.0
    winner_long_pct:         float = 0.0   # % of winner wallets long
    winner_short_pct:        float = 0.0
    winner_notional_long_pct: float = 0.0  # % of winner notional that is long
    winner_avg_leverage:     float = 0.0
    winner_long_avg_lev:     float = 0.0
    winner_short_avg_lev:    float = 0.0
    winner_avg_entry_long:   float = 0.0
    winner_avg_entry_short:  float = 0.0
    winner_total_unrealized: float = 0.0

    # Loser stats
    loser_long_count:        int   = 0
    loser_short_count:       int   = 0
    loser_long_notional:     float = 0.0
    loser_short_notional:    float = 0.0
    loser_long_pct:          float = 0.0
    loser_short_pct:         float = 0.0
    loser_notional_long_pct: float = 0.0
    loser_avg_leverage:      float = 0.0
    loser_long_avg_lev:      float = 0.0
    loser_short_avg_lev:     float = 0.0
    loser_avg_entry_long:    float = 0.0
    loser_avg_entry_short:   float = 0.0
    loser_total_unrealized:  float = 0.0

    # Price clusters
    winner_clusters: List[PriceCluster]  = field(default_factory=list)
    loser_clusters:  List[PriceCluster]  = field(default_factory=list)

    # Per-wallet breakdown
    winner_positions: List[WalletPosition] = field(default_factory=list)
    loser_positions:  List[WalletPosition] = field(default_factory=list)

    # Derived
    divergence_score:     float = 0.0
    gap_pct:              float = 0.0   # |divergence| / total_notional * 100
    net_sentiment:        str   = ""
    signal_type:          str   = ""
    trade_recommendation: str   = ""
    mark_price:           float = 0.0


@dataclass
class MarketAnalysis:
    coins:           Dict[str, CoinSentiment] = field(default_factory=dict)
    top_divergences: List[CoinSentiment]      = field(default_factory=list)
    winner_count:    int  = 0
    loser_count:     int  = 0
    total_positions: int  = 0


class DivergenceEngine:

    CLUSTER_BUCKETS = 5

    def analyse(self, positions: List[OpenPosition]) -> MarketAnalysis:
        if not positions:
            log.warning("No positions to analyse")
            return MarketAnalysis()

        ma = MarketAnalysis(total_positions=len(positions))
        ma.winner_count = len({p.wallet for p in positions if p.wallet_group == "winner"})
        ma.loser_count  = len({p.wallet for p in positions if p.wallet_group == "loser"})

        log.info(f"Analysing {len(positions)} positions — {ma.winner_count} winners / {ma.loser_count} losers")

        coin_map: Dict[str, List[OpenPosition]] = {}
        for p in positions:
            coin_map.setdefault(p.coin, []).append(p)

        for coin, pos_list in coin_map.items():
            ma.coins[coin] = self._compute_sentiment(coin, pos_list)

        ma.top_divergences = sorted(
            [s for s in ma.coins.values() if s.signal_type == "DIVERGENCE"],
            key=lambda x: abs(x.divergence_score),
            reverse=True
        )[:10]

        log.info(f"Divergence signals found: {len(ma.top_divergences)}")
        return ma

    def analyse_coin(self, coin: str, positions: List[OpenPosition]) -> Optional[CoinSentiment]:
        filtered = [p for p in positions if p.coin.upper() == coin.upper()]
        if not filtered:
            return None
        return self._compute_sentiment(coin, filtered)

    # ── Core computation ──────────────────────────────────────────────────────

    def _compute_sentiment(self, coin: str, positions: List[OpenPosition]) -> CoinSentiment:
        s = CoinSentiment(coin=coin)

        winners = [p for p in positions if p.wallet_group == "winner"]
        losers  = [p for p in positions if p.wallet_group == "loser"]

        # Mark price — latest available
        marks = [p.mark_price for p in positions if p.mark_price > 0]
        s.mark_price = marks[0] if marks else 0.0

        # ── Winners ───────────────────────────────────────────────────────────
        w_longs  = [p for p in winners if p.direction == "long"]
        w_shorts = [p for p in winners if p.direction == "short"]
        wn = len(winners)

        s.winner_long_count       = len(w_longs)
        s.winner_short_count      = len(w_shorts)
        s.winner_long_notional    = sum(p.notional_usd for p in w_longs)
        s.winner_short_notional   = sum(p.notional_usd for p in w_shorts)
        s.winner_long_pct         = len(w_longs)  / wn * 100 if wn else 0
        s.winner_short_pct        = len(w_shorts) / wn * 100 if wn else 0
        w_ntl                     = s.winner_long_notional + s.winner_short_notional
        s.winner_notional_long_pct = s.winner_long_notional / w_ntl * 100 if w_ntl else 0
        s.winner_avg_leverage     = self._avg(winners, "leverage")
        s.winner_long_avg_lev     = self._avg(w_longs,  "leverage")
        s.winner_short_avg_lev    = self._avg(w_shorts, "leverage")
        s.winner_avg_entry_long   = self._wavg(w_longs,  "entry_price", "notional_usd")
        s.winner_avg_entry_short  = self._wavg(w_shorts, "entry_price", "notional_usd")
        s.winner_total_unrealized = sum(p.unrealized_pnl for p in winners)

        # ── Losers ────────────────────────────────────────────────────────────
        l_longs  = [p for p in losers if p.direction == "long"]
        l_shorts = [p for p in losers if p.direction == "short"]
        ln = len(losers)

        s.loser_long_count        = len(l_longs)
        s.loser_short_count       = len(l_shorts)
        s.loser_long_notional     = sum(p.notional_usd for p in l_longs)
        s.loser_short_notional    = sum(p.notional_usd for p in l_shorts)
        s.loser_long_pct          = len(l_longs)  / ln * 100 if ln else 0
        s.loser_short_pct         = len(l_shorts) / ln * 100 if ln else 0
        l_ntl                     = s.loser_long_notional + s.loser_short_notional
        s.loser_notional_long_pct = s.loser_long_notional / l_ntl * 100 if l_ntl else 0
        s.loser_avg_leverage      = self._avg(losers, "leverage")
        s.loser_long_avg_lev      = self._avg(l_longs,  "leverage")
        s.loser_short_avg_lev     = self._avg(l_shorts, "leverage")
        s.loser_avg_entry_long    = self._wavg(l_longs,  "entry_price", "notional_usd")
        s.loser_avg_entry_short   = self._wavg(l_shorts, "entry_price", "notional_usd")
        s.loser_total_unrealized  = sum(p.unrealized_pnl for p in losers)

        # ── Clusters and per-wallet breakdown ─────────────────────────────────
        s.winner_clusters  = self._build_clusters(winners, "winner")
        s.loser_clusters   = self._build_clusters(losers,  "loser")
        s.winner_positions = self._to_wallet_positions(winners)
        s.loser_positions  = self._to_wallet_positions(losers)

        # ── Divergence score ──────────────────────────────────────────────────
        winner_net = s.winner_long_notional - s.winner_short_notional
        loser_net  = s.loser_long_notional  - s.loser_short_notional
        s.divergence_score = winner_net - loser_net

        w_dir = "long" if winner_net >= 0 else "short"
        l_dir = "long" if loser_net  >= 0 else "short"
        has_w = wn > 0
        has_l = ln > 0

        if has_w and has_l and w_dir != l_dir:
            s.signal_type = "DIVERGENCE"
        elif has_w and not has_l:
            s.signal_type = "WINNERS_ONLY"
        elif has_w and has_l and w_dir == l_dir:
            s.signal_type = "ALIGNMENT"
        else:
            s.signal_type = "NO_SIGNAL"

        total_ntl = abs(winner_net) + abs(loser_net)
        score_pct = abs(s.divergence_score) / max(total_ntl, 1) * 100
        s.gap_pct  = score_pct   # ← store for EdgeFilter
        direction  = "BULLISH" if s.divergence_score > 0 else "BEARISH"
        if score_pct > 75:
            s.net_sentiment = f"STRONGLY {direction}"
        elif score_pct > 40:
            s.net_sentiment = f"MODERATELY {direction}"
        else:
            s.net_sentiment = f"SLIGHTLY {direction}"

        s.trade_recommendation = self._build_recommendation(s)
        return s

    # ── Price clustering ──────────────────────────────────────────────────────

    def _build_clusters(self, positions: List[OpenPosition], group: str) -> List[PriceCluster]:
        if not positions:
            return []
        entries = [p.entry_price for p in positions if p.entry_price > 0]
        if not entries:
            return []

        min_px = min(entries)
        max_px = max(entries)
        total_notional = sum(p.notional_usd for p in positions)

        if min_px == max_px:
            dirs = {p.direction for p in positions}
            return [PriceCluster(
                price_level=min_px, wallet_count=len(positions),
                total_notional=total_notional,
                direction=list(dirs)[0] if len(dirs) == 1 else "mixed",
                group=group, pct_of_group=100.0
            )]

        bucket_size = (max_px - min_px) / self.CLUSTER_BUCKETS
        buckets: Dict[int, List[OpenPosition]] = {i: [] for i in range(self.CLUSTER_BUCKETS)}

        for p in positions:
            if p.entry_price > 0:
                idx = min(int((p.entry_price - min_px) / bucket_size), self.CLUSTER_BUCKETS - 1)
                buckets[idx].append(p)

        result = []
        for idx, bp in buckets.items():
            if not bp:
                continue
            ntl  = sum(p.notional_usd for p in bp)
            dirs = {p.direction for p in bp}
            result.append(PriceCluster(
                price_level    = min_px + (idx + 0.5) * bucket_size,
                wallet_count   = len(bp),
                total_notional = ntl,
                direction      = list(dirs)[0] if len(dirs) == 1 else "mixed",
                group          = group,
                pct_of_group   = ntl / total_notional * 100 if total_notional else 0,
            ))

        return sorted(result, key=lambda c: c.total_notional, reverse=True)

    def _to_wallet_positions(self, positions: List[OpenPosition]) -> List[WalletPosition]:
        return sorted([
            WalletPosition(
                address        = p.wallet,
                group          = p.wallet_group,
                direction      = p.direction,
                notional_usd   = p.notional_usd,
                entry_price    = p.entry_price,
                leverage       = p.leverage,
                unrealized_pnl = p.unrealized_pnl,
                roi_pct        = p.roi_pct,
                wallet_pnl     = p.wallet_pnl,
            )
            for p in positions
        ], key=lambda x: x.notional_usd, reverse=True)

    def _build_recommendation(self, s: CoinSentiment) -> str:
        mark = f" (mark ${s.mark_price:,.4f})" if s.mark_price else ""
        if s.signal_type == "DIVERGENCE":
            if s.divergence_score > 0:
                e = f" Winners avg long entry: ${s.winner_avg_entry_long:,.4f}." if s.winner_avg_entry_long else ""
                return (
                    f"LONG ${s.coin}{mark} — {s.winner_long_pct:.0f}% of smart money longs "
                    f"(${s.winner_long_notional:,.0f} @ {s.winner_long_avg_lev:.1f}x) "
                    f"vs {s.loser_short_pct:.0f}% of losers short "
                    f"(${s.loser_short_notional:,.0f} @ {s.loser_short_avg_lev:.1f}x).{e}"
                )
            else:
                e = f" Winners avg short entry: ${s.winner_avg_entry_short:,.4f}." if s.winner_avg_entry_short else ""
                return (
                    f"SHORT ${s.coin}{mark} — {s.winner_short_pct:.0f}% of smart money short "
                    f"(${s.winner_short_notional:,.0f} @ {s.winner_short_avg_lev:.1f}x) "
                    f"vs {s.loser_long_pct:.0f}% of losers long "
                    f"(${s.loser_long_notional:,.0f} @ {s.loser_long_avg_lev:.1f}x).{e}"
                )
        elif s.signal_type == "WINNERS_ONLY":
            d = "long" if s.winner_long_notional >= s.winner_short_notional else "short"
            p = s.winner_long_pct if d == "long" else s.winner_short_pct
            return f"WATCH ${s.coin} — {p:.0f}% of smart money is {d}. No loser confirmation yet."
        elif s.signal_type == "ALIGNMENT":
            d = "long" if s.winner_long_notional >= s.winner_short_notional else "short"
            return (f"NEUTRAL ${s.coin} — Smart money {s.winner_long_pct:.0f}% long, "
                    f"losers {s.loser_long_pct:.0f}% long. Both aligned {d}. No edge.")
        return f"NO SIGNAL for ${s.coin}"

    @staticmethod
    def _avg(positions: list, attr: str) -> float:
        vals = [getattr(p, attr) for p in positions if getattr(p, attr, 0)]
        return sum(vals) / len(vals) if vals else 0.0

    @staticmethod
    def _wavg(positions: list, val_attr: str, wt_attr: str) -> float:
        tw = sum(getattr(p, wt_attr, 0) for p in positions)
        if not tw:
            return 0.0
        return sum(getattr(p, val_attr, 0) * getattr(p, wt_attr, 0) for p in positions) / tw
