import os
from datetime import datetime, timezone
from typing import List, Optional
from src.analysis.divergence_engine import MarketAnalysis, CoinSentiment, PriceCluster
from src.data.leaderboard_fetcher import WalletRank
from src.config import config
from src.utils.logger import get_logger

log = get_logger(__name__)

REPORTS_DIR = os.path.join(os.getcwd(), "reports")

W  = 68                     # report width
SEP  = "═" * W
SEP2 = "─" * W
SEP3 = "·" * W


def _bar(pct: float, width: int = 30) -> str:
    """ASCII progress bar."""
    filled = int(round(pct / 100 * width))
    return "█" * filled + "░" * (width - filled)


def _dir_icon(direction: str) -> str:
    return "▲ LONG " if direction == "long" else "▼ SHORT" if direction == "short" else "◆ MIXED"


class ReportGenerator:

    # ── Global market report ──────────────────────────────────────────────────

    def global_report(
        self,
        analysis: MarketAnalysis,
        winners:  List[WalletRank],
        losers:   List[WalletRank],
    ) -> str:
        now   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines = []

        lines += [
            "", SEP,
            _center("HYPERLIQUID SMART MONEY SENTIMENT REPORT", W),
            _center(f"Generated: {now}", W),
            _center(f"Window: {config.LEADERBOARD_WINDOW.upper()}  |  Top {config.TOP_N_WALLETS} Winners & Losers", W),
            SEP, "",
        ]

        # Overview
        lines += [
            "  OVERVIEW",
            SEP2,
            f"  Wallets analysed  : {analysis.winner_count} winners / {analysis.loser_count} losers",
            f"  Total positions   : {analysis.total_positions}",
            f"  Coins with data   : {len(analysis.coins)}",
            f"  Divergence signals: {len(analysis.top_divergences)}",
            "",
        ]

        # Leaderboard snapshot
        lines += self._leaderboard_block(winners, losers)

        # Sentiment table — all coins with positions
        lines += self._sentiment_table(analysis)

        # Divergence signals
        lines += [
            "", SEP,
            _center("TOP DIVERGENCE SIGNALS", W),
            SEP,
        ]
        if not analysis.top_divergences:
            lines += ["", "  No divergence signals at this time.", ""]
        else:
            for i, s in enumerate(analysis.top_divergences, 1):
                lines += self._coin_detail_block(s, i)

        lines += ["", SEP, _center("END OF REPORT", W), SEP, ""]
        report = "\n".join(lines)
        saved  = self._save(report, "global")
        return report, saved

    # ── Coin-specific report ──────────────────────────────────────────────────

    def coin_report(
        self,
        coin:      str,
        sentiment: Optional[CoinSentiment],
        winners:   List[WalletRank],
        losers:    List[WalletRank],
    ) -> str:
        now   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines = ["", SEP,
                 _center(f"${coin.upper()} — SMART MONEY REPORT", W),
                 _center(f"Generated: {now}", W),
                 SEP, ""]

        if sentiment is None:
            lines += [
                f"  No open positions found for ${coin} among top",
                f"  {config.TOP_N_WALLETS} winners or losers.",
                "", SEP, ""]
        else:
            lines += self._coin_detail_block(sentiment, 1, full=True)
            lines += ["", SEP, ""]

        if config.SAVE_REPORTS:
            self._save("\n".join(lines), f"coin_{coin.upper()}")
        report = "\n".join(lines)
        saved  = self._save(report, f"coin_{coin.upper()}")
        return report, saved

    # ── Section builders ──────────────────────────────────────────────────────

    def _leaderboard_block(self, winners: List[WalletRank], losers: List[WalletRank]) -> List[str]:
        lines = [
            "  TOP 5 WINNERS",
            SEP2,
        ]
        for i in range(5):
            w = winners[i] if i < len(winners) else None
            if w:
                lines.append(f"  #{w.rank:<2} {w.address}  PnL: ${w.pnl:>+12,.0f}  ROI:{w.roi_pct:>7.1f}%")

        lines += ["", "  TOP 5 LOSERS", SEP2]
        for i in range(5):
            l = losers[i] if i < len(losers) else None
            if l:
                lines.append(f"  #{l.rank:<2} {l.address}  PnL: ${l.pnl:>+12,.0f}  ROI:{l.roi_pct:>7.1f}%")

        lines.append("")
        return lines

    def _sentiment_table(self, analysis: MarketAnalysis) -> List[str]:
        lines = [
            "  SENTIMENT TABLE  (by coin)",
            SEP2,
            f"  {'COIN':<8}  {'SMART MONEY':^28}  {'DUMB MONEY':^28}  SIGNAL",
            f"  {'':8}  {'L%  notional%  lev':^28}  {'L%  notional%  lev':^28}",
            SEP3,
        ]

        # Sort: divergences first, then winners-only, then alignment
        order = {"DIVERGENCE": 0, "WINNERS_ONLY": 1, "ALIGNMENT": 2, "NO_SIGNAL": 3}
        sorted_coins = sorted(
            analysis.coins.values(),
            key=lambda s: (order.get(s.signal_type, 9), -abs(s.divergence_score))
        )

        for s in sorted_coins:
            signal_icon = {"DIVERGENCE": "⚡", "WINNERS_ONLY": "👀", "ALIGNMENT": "≈", "NO_SIGNAL": "–"}.get(s.signal_type, "")

            w_l_pct  = f"{s.winner_long_pct:>4.0f}%"
            w_n_pct  = f"{s.winner_notional_long_pct:>4.0f}%"
            w_lev    = f"{s.winner_avg_leverage:>3.1f}x"
            l_l_pct  = f"{s.loser_long_pct:>4.0f}%"
            l_n_pct  = f"{s.loser_notional_long_pct:>4.0f}%"
            l_lev    = f"{s.loser_avg_leverage:>3.1f}x"

            # Direction arrows for quick scan
            w_arrow = "▲" if s.winner_notional_long_pct >= 50 else "▼"
            l_arrow = "▲" if s.loser_notional_long_pct  >= 50 else "▼"

            lines.append(
                f"  {s.coin:<8}  {w_arrow} {w_l_pct} wallets  {w_n_pct} ntl  {w_lev}  "
                f"  {l_arrow} {l_l_pct} wallets  {l_n_pct} ntl  {l_lev}  "
                f"  {signal_icon} {s.net_sentiment}"
            )

        lines.append("")
        return lines

    def _coin_detail_block(self, s: CoinSentiment, index: int, full: bool = False) -> List[str]:
        mark_str = f"  Mark: ${s.mark_price:,.4f}" if s.mark_price else ""
        lines = [
            "",
            f"  [{index}] ${s.coin}  —  {s.net_sentiment}  [{s.signal_type}]{mark_str}",
            SEP2,
        ]

        # ── Long/Short % breakdown ────────────────────────────────────────────
        lines += [
            "  LONG / SHORT BREAKDOWN",
            "",
            "  SMART MONEY (Winners)",
            f"  Wallets  : {_bar(s.winner_long_pct, 20)} {s.winner_long_pct:>5.1f}% LONG  |  "
            f"{_bar(s.winner_short_pct, 20)} {s.winner_short_pct:>5.1f}% SHORT",
            f"  Notional : {_bar(s.winner_notional_long_pct, 20)} {s.winner_notional_long_pct:>5.1f}% LONG  |  "
            f"{100-s.winner_notional_long_pct:>5.1f}% SHORT",
            f"  Long  → {s.winner_long_count:>2} wallets  ${s.winner_long_notional:>12,.0f}  avg lev: {s.winner_long_avg_lev:.1f}x"
            + (f"  avg entry: ${s.winner_avg_entry_long:,.4f}" if s.winner_avg_entry_long else ""),
            f"  Short → {s.winner_short_count:>2} wallets  ${s.winner_short_notional:>12,.0f}  avg lev: {s.winner_short_avg_lev:.1f}x"
            + (f"  avg entry: ${s.winner_avg_entry_short:,.4f}" if s.winner_avg_entry_short else ""),
            f"  Unrealized PnL : ${s.winner_total_unrealized:>+12,.2f}",
            "",
            "  DUMB MONEY (Losers)",
            f"  Wallets  : {_bar(s.loser_long_pct, 20)} {s.loser_long_pct:>5.1f}% LONG  |  "
            f"{_bar(s.loser_short_pct, 20)} {s.loser_short_pct:>5.1f}% SHORT",
            f"  Notional : {_bar(s.loser_notional_long_pct, 20)} {s.loser_notional_long_pct:>5.1f}% LONG  |  "
            f"{100-s.loser_notional_long_pct:>5.1f}% SHORT",
            f"  Long  → {s.loser_long_count:>2} wallets  ${s.loser_long_notional:>12,.0f}  avg lev: {s.loser_long_avg_lev:.1f}x"
            + (f"  avg entry: ${s.loser_avg_entry_long:,.4f}" if s.loser_avg_entry_long else ""),
            f"  Short → {s.loser_short_count:>2} wallets  ${s.loser_short_notional:>12,.0f}  avg lev: {s.loser_short_avg_lev:.1f}x"
            + (f"  avg entry: ${s.loser_avg_entry_short:,.4f}" if s.loser_avg_entry_short else ""),
            f"  Unrealized PnL : ${s.loser_total_unrealized:>+12,.2f}",
        ]

        # ── Price clusters ────────────────────────────────────────────────────
        lines += ["", SEP3, "  ENTRY PRICE CLUSTERS  (where positions were opened)", SEP3]

        lines += ["", "  Smart Money entry concentration:"]
        if s.winner_clusters:
            for c in s.winner_clusters[:3]:
                bar = _bar(c.pct_of_group, 25)
                lines.append(
                    f"  {_dir_icon(c.direction)}  ${c.price_level:>12,.4f}  "
                    f"{bar} {c.pct_of_group:>5.1f}%  "
                    f"{c.wallet_count} wallet{'s' if c.wallet_count != 1 else ''}  "
                    f"${c.total_notional:>10,.0f}"
                )
        else:
            lines.append("  No cluster data available.")

        lines += ["", "  Dumb Money entry concentration:"]
        if s.loser_clusters:
            for c in s.loser_clusters[:3]:
                bar = _bar(c.pct_of_group, 25)
                lines.append(
                    f"  {_dir_icon(c.direction)}  ${c.price_level:>12,.4f}  "
                    f"{bar} {c.pct_of_group:>5.1f}%  "
                    f"{c.wallet_count} wallet{'s' if c.wallet_count != 1 else ''}  "
                    f"${c.total_notional:>10,.0f}"
                )
        else:
            lines.append("  No cluster data available.")

        # ── Per-wallet breakdown (full mode only) ─────────────────────────────
        if full:
            lines += self._wallet_breakdown(s)

        # ── Divergence score + trade plan ─────────────────────────────────────
        lines += [
            "",
            SEP3,
            f"  DIVERGENCE SCORE  :  {s.divergence_score:>+14,.0f}",
            f"  (positive = smart money net long vs dumb money net short)",
            "",
            "  ➤  TRADE PLAN:",
        ]
        # Word-wrap the recommendation at ~64 chars
        for chunk in _wrap(s.trade_recommendation, 64):
            lines.append(f"     {chunk}")

        return lines

    def _wallet_breakdown(self, s: CoinSentiment) -> List[str]:
        HDR = f"  {'WALLET ADDRESS':<44} {'DIR':<6} {'NOTIONAL':>12} {'ENTRY':>12} {'LEV':>5} {'UNREAL PnL':>12} {'ROI':>7}"

        def _rows(positions, sort_key, reverse=True) -> List[str]:
            rows = []
            for p in sorted(positions, key=sort_key, reverse=reverse)[:10]:
                rows.append(
                    f"  {p.address:<44}"
                    f"  {'▲ L' if p.direction=='long' else '▼ S'}"
                    f"  ${p.notional_usd:>10,.0f}"
                    f"  ${p.entry_price:>10,.4f}"
                    f"  {p.leverage:>4.1f}x"
                    f"  ${p.unrealized_pnl:>+10,.2f}"
                    f"  {p.roi_pct:>+6.1f}%"
                )
            return rows or ["  No positions."]

        lines = [
            "", SEP3,
            "  ① SMART MONEY — TOP 10 BY ROI %  (highest conviction, best performers)",
            SEP3, HDR, SEP3,
        ]
        lines += _rows(s.winner_positions, lambda x: x.roi_pct)

        lines += [
            "", SEP3,
            "  ② SMART MONEY — TOP 10 BY NOTIONAL  (biggest positions, price movers)",
            SEP3, HDR, SEP3,
        ]
        lines += _rows(s.winner_positions, lambda x: x.notional_usd)

        lines += [
            "", SEP3,
            "  ③ DUMB MONEY — TOP 10 BY ROI %  (biggest losers on % basis)",
            SEP3, HDR, SEP3,
        ]
        lines += _rows(s.loser_positions, lambda x: x.roi_pct)

        lines += [
            "", SEP3,
            "  ④ DUMB MONEY — TOP 10 BY NOTIONAL  (biggest losing positions)",
            SEP3, HDR, SEP3,
        ]
        lines += _rows(s.loser_positions, lambda x: x.notional_usd)

        return lines

    def _save(self, report: str, label: str) -> str:
        reports_dir = os.path.join(os.getcwd(), "reports")
        os.makedirs(reports_dir, exist_ok=True)
        ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
        fn  = os.path.join(reports_dir, f"report_{label}_{ts}.txt")
        with open(fn, "w", encoding="utf-8") as f:
            f.write(report)
        log.info(f"Report saved → {fn}")
        return fn


# ── Helpers ───────────────────────────────────────────────────────────────────

def _center(text: str, width: int) -> str:
    return text.center(width)

def _wrap(text: str, width: int) -> List[str]:
    words, lines, current = text.split(), [], ""
    for w in words:
        if len(current) + len(w) + 1 <= width:
            current = (current + " " + w).strip()
        else:
            if current:
                lines.append(current)
            current = w
    if current:
        lines.append(current)
    return lines or [""]
