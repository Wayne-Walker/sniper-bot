import os
import csv
from datetime import datetime, timezone
from typing import List
from src.analysis.trade_analyser import WalletStats
from src.data.fills_fetcher import CompletedTrade
from src.config import config
from src.utils.logger import get_logger

log = get_logger(__name__)

# ── Reports directory — always resolved relative to cwd (where main.py lives) ─
REPORTS_DIR = os.path.join(os.getcwd(), "reports")

W    = 72
SEP  = "═" * W
SEP2 = "─" * W
SEP3 = "·" * W


# ── Helpers ───────────────────────────────────────────────────────────────────

def _bar(pct: float, w: int = 20) -> str:
    f = int(round(max(0.0, min(100.0, pct)) / 100 * w))
    return "█" * f + "░" * (w - f)

def _pnl(v: float) -> str:
    return f"${v:>+12,.2f}"

def _dur(hrs: float) -> str:
    if hrs <= 0:     return "0m"
    if hrs < 1:      return f"{int(hrs * 60)}m"
    if hrs < 24:     return f"{hrs:.1f}h"
    if hrs < 24*365: return f"{hrs/24:.1f}d"
    return f"{hrs/24/365:.1f}yr"

def _dir(d: str) -> str:
    return "▲ LONG " if d == "long" else "▼ SHORT"

def _c(text: str, width: int) -> str:
    return text.center(width)

def _trade_row(t: CompletedTrade) -> str:
    is_orphan = t.open_tx == "pre-window"
    open_date = f"{'~'+t.close_date:<17}" if is_orphan else f"{t.open_date:<17}"
    dur_str   = ">1yr  " if is_orphan else f"{_dur(t.duration_hrs):>6}"
    flag      = " *" if is_orphan else "  "
    return (
        f"  {t.coin:<6} {_dir(t.direction):<8} "
        f"{open_date} {t.close_date:<17} "
        f"{dur_str}  "
        f"{t.size:>9.4f}  "
        f"${t.notional_usd:>10,.0f}  "
        f"${t.open_price:>9,.4f}  "
        f"${t.close_price:>9,.4f}  "
        f"${t.net_pnl:>+10,.2f}  "
        f"{t.roi_pct:>+6.2f}%  "
        f"${t.fees:>7,.2f}{flag}"
    )


# ── Report Generator ──────────────────────────────────────────────────────────

class ReportGenerator:

    # ── Multi-wallet summary ──────────────────────────────────────────────────

    def summary_report(self, all_stats: List[WalletStats]) -> str:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines = [
            "", SEP,
            _c("HYPERLIQUID WALLET TRACKER", W),
            _c("Most Successful Trading Wallets", W),
            _c(f"Generated: {now}  |  Last {config.LOOKBACK_DAYS} days", W),
            SEP, "",
            "  WALLET OVERVIEW",
            SEP2,
            f"  {'#':<3} {'WALLET':<13} {'TRADES':>6} {'WIN%':>6} {'NET PNL':>13} "
            f"{'AVG PNL':>11} {'PROF.F':>7} {'AVG DUR':>8} {'TOP COIN':<10}",
            SEP3,
        ]

        for s in all_stats:
            pf = f"{s.profit_factor:.2f}" if s.profit_factor != float("inf") else "∞"
            lines.append(
                f"  {s.rank:<3} {s.address[:11]}..  "
                f"{s.total_trades:>6}  {s.win_rate:>5.1f}%  "
                f"${s.net_pnl:>+11,.0f}  ${s.avg_net_pnl:>+9,.0f}  "
                f"{pf:>7}  {_dur(s.avg_duration_hr):>8}  "
                f"{s.most_profitable_coin:<10}"
            )

        for s in all_stats:
            lines += self._wallet_block(s)

        lines += ["", SEP, _c("END OF REPORT", W), SEP, ""]
        report = "\n".join(lines)
        self._save_txt(report, "wallet_tracker")
        return report

    # ── Single wallet deep dive ───────────────────────────────────────────────

    def wallet_report(self, stats: WalletStats) -> str:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines = [
            "", SEP,
            _c("WALLET DEEP DIVE", W),
            _c(stats.address, W),
            _c(f"Generated: {now}  |  Last {config.LOOKBACK_DAYS} days", W),
            SEP, "",
        ]
        lines += self._wallet_block(stats, full=True)
        lines += ["", SEP, _c("END OF REPORT", W), SEP, ""]
        report = "\n".join(lines)
        self._save_txt(report, f"wallet_{stats.address[:10]}")
        return report

    # ── CSV export ────────────────────────────────────────────────────────────

    def save_csv(self, all_stats: List[WalletStats]) -> str:
        ts          = datetime.now().strftime("%Y%m%d_%H%M%S")
        reports_dir = os.path.join(os.getcwd(), "reports")
        os.makedirs(reports_dir, exist_ok=True)

        # ── Closed trades ─────────────────────────────────────────────────────
        closed_fn  = os.path.join(reports_dir, f"trades_{ts}.csv")
        fieldnames = [
            "wallet", "rank", "coin", "direction",
            "open_date", "close_date", "duration_hrs", "duration_str",
            "size", "notional_usd", "open_price", "close_price",
            "closed_pnl", "fees", "net_pnl", "roi_pct",
            "open_tx", "close_tx",
        ]
        closed_rows = 0
        with open(closed_fn, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for s in all_stats:
                for t in s.all_trades:
                    writer.writerow({
                        "wallet": t.wallet, "rank": s.rank,
                        "coin": t.coin, "direction": t.direction,
                        "open_date": t.open_date, "close_date": t.close_date,
                        "duration_hrs": f"{t.duration_hrs:.2f}",
                        "duration_str": t.duration_str,
                        "size": f"{t.size:.6f}", "notional_usd": f"{t.notional_usd:.2f}",
                        "open_price": f"{t.open_price:.6f}",
                        "close_price": f"{t.close_price:.6f}",
                        "closed_pnl": f"{t.closed_pnl:.2f}", "fees": f"{t.fees:.2f}",
                        "net_pnl": f"{t.net_pnl:.2f}", "roi_pct": f"{t.roi_pct:.4f}",
                        "open_tx": t.open_tx, "close_tx": t.close_tx,
                    })
                    closed_rows += 1
        log.info(f"Closed trades CSV → {closed_fn}  ({closed_rows} rows)")

        # ── Open positions ────────────────────────────────────────────────────
        open_fn     = os.path.join(reports_dir, f"open_positions_{ts}.csv")
        open_fields = [
            "wallet", "rank", "coin", "direction",
            "size", "notional_usd", "entry_price", "mark_price",
            "unrealized_pnl", "roi_pct", "leverage",
        ]
        open_rows = 0
        with open(open_fn, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=open_fields)
            writer.writeheader()
            for s in all_stats:
                for p in s.open_positions:
                    writer.writerow({
                        "wallet": p.wallet, "rank": s.rank,
                        "coin": p.coin, "direction": p.direction,
                        "size": f"{p.size:.6f}", "notional_usd": f"{p.notional_usd:.2f}",
                        "entry_price": f"{p.entry_price:.6f}",
                        "mark_price": f"{p.mark_price:.6f}",
                        "unrealized_pnl": f"{p.unrealized_pnl:.2f}",
                        "roi_pct": f"{p.roi_pct:.4f}", "leverage": f"{p.leverage:.1f}",
                    })
                    open_rows += 1
        log.info(f"Open positions CSV → {open_fn}  ({open_rows} rows)")

        return closed_fn

    # ── Wallet block ──────────────────────────────────────────────────────────

    def _wallet_block(self, s: WalletStats, full: bool = False) -> List[str]:
        lines = [
            "", SEP,
            f"  WALLET #{s.rank}  {s.address}",
            f"  Leaderboard PnL: ${s.leaderboard_pnl:>+14,.0f}  "
            f"ROI: {s.leaderboard_roi:>+7.1f}%  "
            f"Account Value: ${s.account_value:>10,.0f}",
            SEP2,
        ]

        if s.total_trades == 0:
            msg = "  No completed trades found in this period."
            if s.extended_lookback_used:
                msg += f"  (searched {config.LOOKBACK_DAYS * 3}d)"
            elif s.open_positions:
                msg += (
                    f"\n  ⚠️  This wallet has {len(s.open_positions)} open position(s) "
                    f"but no CLOSED trades in the {config.LOOKBACK_DAYS}d window.\n"
                    f"     The position(s) were likely opened before the lookback period.\n"
                    f"     Tip: increase LOOKBACK_DAYS in .env to capture the open trade."
                )
            lines.append(msg)
            # Still show open positions even if no closed trades
            if s.open_positions:
                lines += self._open_positions_block(s)
            return lines

        # Performance summary
        pf_str = f"{s.profit_factor:.2f}" if s.profit_factor != float("inf") else "∞"
        lines += [
            "  PERFORMANCE SUMMARY",
            SEP3,
            f"  Trades        : {s.total_trades}   "
            f"Win Rate: {_bar(s.win_rate)} {s.win_rate:.1f}%  "
            f"({s.win_count}W / {s.loss_count}L)",
            f"  Net PnL       : {_pnl(s.net_pnl)}   Avg/trade: {_pnl(s.avg_net_pnl)}",
            f"  Gross PnL     : {_pnl(s.total_pnl)}   Fees paid: ${s.total_fees:>+10,.2f}",
            f"  Best trade    : {_pnl(s.best_trade_pnl)}   Worst:    {_pnl(s.worst_trade_pnl)}",
            f"  Avg ROI/trade : {s.avg_roi_pct:>+8.2f}%        Profit factor: {pf_str}",
            f"  Avg notional  : ${s.avg_notional:>10,.0f}",
            "",
            "  DURATION PROFILE",
            SEP3,
            f"  Avg: {_dur(s.avg_duration_hr):<8}  "
            f"Median: {_dur(s.median_duration_hr):<8}  "
            f"Min: {_dur(s.min_duration_hr):<8}  "
            f"Max: {_dur(s.max_duration_hr)}",
            "",
            "  DIRECTION BIAS",
            SEP3,
            f"  LONG  : {s.long_count:>3} trades  "
            f"{_bar(s.long_win_rate, 15)} {s.long_win_rate:.0f}% win  "
            f"Net PnL: {_pnl(s.long_pnl)}",
            f"  SHORT : {s.short_count:>3} trades  "
            f"{_bar(s.short_win_rate, 15)} {s.short_win_rate:.0f}% win  "
            f"Net PnL: {_pnl(s.short_pnl)}",
            "",
            "  PER-COIN BREAKDOWN  (sorted by net PnL)",
            SEP3,
            f"  {'COIN':<8} {'TRADES':>6} {'WIN%':>6} {'NET PNL':>13} {'AVG PNL':>11} "
            f"{'AVG ROI':>8} {'AVG DUR':>8} {'LONG':>5} {'SHORT':>6}",
            SEP3,
        ]

        for cs in sorted(s.coin_stats.values(), key=lambda c: c.net_pnl, reverse=True):
            lines.append(
                f"  {cs.coin:<8} {cs.trade_count:>6}  {cs.win_rate:>5.1f}%  "
                f"${cs.net_pnl:>+11,.0f}  ${cs.avg_net_pnl:>+9,.0f}  "
                f"{cs.avg_roi_pct:>+7.2f}%  {_dur(cs.avg_duration_hr):>8}  "
                f"{cs.long_count:>5}  {cs.short_count:>6}"
            )

        # Best trades
        TRADE_HDR = (
            f"  {'COIN':<6} {'DIR':<8} {'OPEN DATE':<17} {'CLOSE DATE':<17} "
            f"{'DUR':>6} {'SIZE':>10} {'NOTIONAL':>12} "
            f"{'ENTRY':>11} {'EXIT':>11} {'PNL':>12} {'ROI':>7} {'FEES':>9}"
        )
        FOOTNOTE = "  * open date unknown — position was opened before the lookback window"
        lines += ["", "  TOP 5 BEST TRADES  (sorted by ROI %)", SEP3, TRADE_HDR, SEP3]
        for t in sorted(s.best_trades, key=lambda x: x.roi_pct, reverse=True):
            lines.append(_trade_row(t))
        if any(t.open_tx == "pre-window" for t in s.best_trades):
            lines.append(FOOTNOTE)

        lines += ["", "  TOP 5 WORST TRADES  (sorted by ROI %)", SEP3, TRADE_HDR, SEP3]
        for t in sorted(s.worst_trades, key=lambda x: x.roi_pct):
            lines.append(_trade_row(t))
        if any(t.open_tx == "pre-window" for t in s.worst_trades):
            lines.append(FOOTNOTE)

        if full:
            lines += [
                "",
                f"  COMPLETE TRADE HISTORY  ({len(s.all_trades)} trades, sorted by close date)",
                SEP3, TRADE_HDR, SEP3,
            ]
            for t in s.all_trades:
                lines.append(_trade_row(t))
            if any(t.open_tx == "pre-window" for t in s.all_trades):
                lines.append(FOOTNOTE)

        # Always show open positions at the end
        if s.open_positions:
            lines += self._open_positions_block(s)

        return lines

    def _open_positions_block(self, s: WalletStats) -> List[str]:
        lines = [
            "", SEP3,
            f"  ⚡ CURRENTLY OPEN POSITIONS  ({len(s.open_positions)} positions — not yet closed)",
            SEP3,
            f"  {'COIN':<8} {'DIR':<8} {'SIZE':>10} {'NOTIONAL':>12} "
            f"{'ENTRY':>12} {'MARK':>12} {'UNREAL PNL':>12} {'ROI':>7} {'LEV':>5}",
            SEP3,
        ]
        for p in sorted(s.open_positions, key=lambda x: x.notional_usd, reverse=True):
            pnl_str = f"${p.unrealized_pnl:>+10,.2f}"
            lines.append(
                f"  {p.coin:<8} "
                f"{'▲ LONG' if p.direction == 'long' else '▼ SHORT':<8} "
                f"{p.size:>10.4f}  "
                f"${p.notional_usd:>10,.0f}  "
                f"${p.entry_price:>10,.4f}  "
                f"${p.mark_price:>10,.4f}  "
                f"{pnl_str}  "
                f"{p.roi_pct:>+6.1f}%  "
                f"{p.leverage:>4.1f}x"
            )
        total_notional  = sum(p.notional_usd for p in s.open_positions)
        total_unrealized = sum(p.unrealized_pnl for p in s.open_positions)
        lines += [
            SEP3,
            f"  Total notional: ${total_notional:>12,.0f}   "
            f"Total unrealized PnL: ${total_unrealized:>+12,.2f}",
        ]
        return lines

    # ── Save helpers ──────────────────────────────────────────────────────────

    def _save_txt(self, content: str, label: str) -> str:
        reports_dir = os.path.join(os.getcwd(), "reports")
        os.makedirs(reports_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fn = os.path.join(reports_dir, f"{label}_{ts}.txt")
        with open(fn, "w", encoding="utf-8") as f:
            f.write(content)
        log.info(f"TXT saved  → {fn}")
        return fn
