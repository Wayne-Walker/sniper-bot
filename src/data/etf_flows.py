"""
ETF Flows — Bitcoin spot ETF daily flow data (free, scraped from Bitbo).

Source: https://bitbo.io/treasuries/etf-flows/
        Plain-HTML table, no auth, no rate limit. Values are in US$ millions.

What we extract:
  - Latest day's total net flow
  - Per-issuer breakdown (IBIT, FBTC, GBTC, …)
  - 7-day cumulative flow (signal: are inflows persistent or one-off?)
  - Trend direction (consecutive in/outflow days)

Note: ETH / SOL / XRP ETF flow data is *not* freely available in raw HTML
from any source we tested (Bitbo: BTC only; CoinGlass / SoSoValue / CMC:
JS-rendered or paid API). Add those when a paid CoinGlass key is acquired.
"""

import re
import requests
from dataclasses import dataclass, field
from datetime import datetime
from typing import List
from src.utils.logger import get_logger

log = get_logger(__name__)

BITBO_URL = "https://bitbo.io/treasuries/etf-flows/"
TIMEOUT   = 15

# Bitbo rows look like:
#   Date | IBIT | FBTC | GBTC | BTC | BITB | ARKB | HODL | BTCO | BRRR
#        | EZBC | MSBT | BTCW | DEFI | Totals
# Values are in millions USD.
EXPECTED_CELLS = 15


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class ETFDay:
    """One day of BTC ETF flow data (millions USD)."""
    date:        str  = ""          # e.g. "May 12, 2026"
    per_issuer:  dict = field(default_factory=dict)  # ticker → flow $M
    total:       float = 0.0        # total net flow $M


@dataclass
class BTCETFFlows:
    """Aggregated BTC ETF flow data."""
    days:           List[ETFDay] = field(default_factory=list)
    latest_total:   float = 0.0    # most-recent day, $M
    sum_7d:         float = 0.0    # cumulative last 7 days, $M
    sum_30d:        float = 0.0    # cumulative last 30 days (if available), $M
    consecutive:    int   = 0      # signed: positive = N inflow days, negative = N outflow days
    ibit_latest:    float = 0.0    # IBIT specifically (largest mover), $M


# ── Scraper ──────────────────────────────────────────────────────────────────

def _fetch_html() -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
    }
    resp = requests.get(BITBO_URL, headers=headers, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.text


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).strip()


def _parse_float(s: str) -> float:
    s = s.replace(",", "").replace("$", "").strip()
    if not s or s == "-":
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def parse_bitbo(html: str) -> List[ETFDay]:
    """Parse Bitbo's ETF flows table into a list of ETFDay entries."""
    table_match = re.search(r"<table[^>]*>(.*?)</table>", html, re.DOTALL)
    if not table_match:
        raise RuntimeError("Bitbo HTML structure changed — no <table> found")

    table = table_match.group(1)

    # Extract column headers (ticker order)
    headers = [
        _strip_html(h)
        for h in re.findall(r"<th[^>]*>(.*?)</th>", table, re.DOTALL)
    ]
    # We only care about the per-row columns (Date + tickers + Totals).
    # Summary stats (Total/Average/Max/Min) follow but aren't per-row cells.
    if len(headers) < EXPECTED_CELLS:
        raise RuntimeError(
            f"Bitbo headers changed: expected ≥{EXPECTED_CELLS}, got {len(headers)}"
        )

    row_headers = headers[:EXPECTED_CELLS]  # Date, tickers..., Totals
    tickers = row_headers[1:-1]              # drop Date and Totals

    days: List[ETFDay] = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.DOTALL):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
        if len(cells) < EXPECTED_CELLS:
            continue

        values = [_strip_html(c) for c in cells[:EXPECTED_CELLS]]
        date_str = values[0]
        ticker_flows = {t: _parse_float(values[i + 1]) for i, t in enumerate(tickers)}
        total = _parse_float(values[-1])

        days.append(ETFDay(date=date_str, per_issuer=ticker_flows, total=total))

    if not days:
        raise RuntimeError("Bitbo table parsed but no data rows found")

    return days


def fetch_btc_etf_flows() -> BTCETFFlows:
    """Fetch and aggregate BTC ETF flows from Bitbo."""
    log.info("Fetching BTC ETF flows (Bitbo)...")
    html = _fetch_html()
    days = parse_bitbo(html)

    flows = BTCETFFlows(days=days)
    flows.latest_total = days[0].total
    flows.sum_7d  = sum(d.total for d in days[:7])
    flows.sum_30d = sum(d.total for d in days[:30])
    flows.ibit_latest = days[0].per_issuer.get("IBIT", 0.0)

    # Consecutive streak (signed)
    sign_of_latest = 1 if flows.latest_total > 0 else (-1 if flows.latest_total < 0 else 0)
    streak = 0
    for d in days:
        s = 1 if d.total > 0 else (-1 if d.total < 0 else 0)
        if s == 0 or s != sign_of_latest:
            break
        streak += 1
    flows.consecutive = streak * sign_of_latest

    log.info(
        f"  Latest ({days[0].date}): ${flows.latest_total:+,.1f}M  "
        f"7d cum: ${flows.sum_7d:+,.0f}M  "
        f"streak: {flows.consecutive:+d} days"
    )
    return flows


# ── Formatting ───────────────────────────────────────────────────────────────

def format_etf_flows(flows: BTCETFFlows) -> str:
    if not flows.days:
        return ""

    lines = [
        "*BTC ETF FLOWS* (US Spot, $M)",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    latest = flows.days[0]
    arrow_latest = "🟢" if flows.latest_total >= 0 else "🔴"
    arrow_7d     = "🟢" if flows.sum_7d       >= 0 else "🔴"

    lines.append(
        f"  {arrow_latest} *{latest.date}*: `${flows.latest_total:+,.1f}M`"
    )
    lines.append(
        f"  {arrow_7d} *7d cumulative*: `${flows.sum_7d:+,.0f}M`"
    )

    if flows.consecutive:
        word = "inflow" if flows.consecutive > 0 else "outflow"
        lines.append(
            f"  Streak: `{abs(flows.consecutive)}` consecutive {word} day(s)"
        )

    # Top issuers today (>$5M abs)
    top = sorted(
        latest.per_issuer.items(),
        key=lambda kv: abs(kv[1]),
        reverse=True,
    )
    movers = [(t, v) for t, v in top if abs(v) >= 5.0]
    if movers:
        lines.append("  Top movers today:")
        for t, v in movers[:5]:
            arr = "▲" if v >= 0 else "▼"
            lines.append(f"    {arr} {t}: `${v:+,.1f}M`")

    # Signal
    sig = _derive_etf_signal(flows)
    if sig:
        lines.append("")
        lines.append(f"  {sig}")

    lines.append("")
    lines.append("_ETH/SOL/XRP ETF flows: paid data only_")
    return "\n".join(lines)


def _derive_etf_signal(flows: BTCETFFlows) -> str:
    """Return a single high-signal interpretation, or empty string."""
    # Heavy single-day move
    if flows.latest_total >= 500:
        return "🟢 Heavy BTC ETF inflow today (>$500M) — strong institutional bid"
    if flows.latest_total <= -500:
        return "🔴 Heavy BTC ETF outflow today (>$500M) — institutions de-risking"

    # 7-day cumulative
    if flows.sum_7d >= 1500:
        return "🟢 Sustained 7d ETF inflow (>$1.5B) — institutional accumulation"
    if flows.sum_7d <= -1500:
        return "🔴 Sustained 7d ETF outflow (>$1.5B) — institutional distribution"

    # Streak
    if flows.consecutive >= 5:
        return f"🟢 {flows.consecutive}-day inflow streak — persistent bid"
    if flows.consecutive <= -5:
        return f"🔴 {abs(flows.consecutive)}-day outflow streak — persistent selling"

    return ""


# ── CLI test ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    flows = fetch_btc_etf_flows()
    print(format_etf_flows(flows))
