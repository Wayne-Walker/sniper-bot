#!/usr/bin/env python3
"""
Ratio Watch — alt/BTC (and other) ratio-chart monitor.
═════════════════════════════════════════════════════════════════════════════

Watches ratio charts (default XRP/BTC) — an altcoin priced in BTC rather than
USD, i.e. whether the alt is GAINING or LOSING ground against BTC (rotation
timing). Reuses the exhaustion engine's primitives (RSI, TRAMA trend, S/R);
there's no funding on a spot ratio, so it's price-structure only.

Prices are handled in **sats** (ratio × 1e8) — how alt/BTC pairs are quoted
("XRP = 1,682 sats") and, conveniently, the scale at which S/R clusters cleanly
(compute_sr rounds to 6dp, which collapses raw 1.6e-5 ratios). RSI and TRAMA are
scale-invariant, so the sats scaling only affects display + level resolution.

Data: the native Binance spot pair ({NUM}{DEN}, e.g. XRPBTC) when it exists,
else synthesized from the two USDT legs ({NUM}USDT / {DEN}USDT), close-by-close.

Alerts (private chat only, transition-dedup via reports/ratio_state.json):
  • TRAMA trend flip           (up/flat/down changes)
  • TRAMA reclaim / lose       (price crosses the adaptive trend line)
  • shelf break                (price crosses the dominant S/R level)
Plus one daily snapshot (all pairs, 1h + 1d read) at RATIO_SNAPSHOT_HOUR UTC.

Usage:
  python ratio_watch.py                 # scan all RATIO_PAIRS
  python ratio_watch.py XRP/BTC         # one pair, print read (no alert/seed)
  pm2 start ecosystem.config.js --only ratio-watch
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

import exhaustion_watch as ew  # rsi / trama / trama_trend / compute_sr / telegram / TRAMA_*

SPOT = "https://api.binance.com/api/v3/klines"
STATE_FILE = ROOT / "reports" / "ratio_state.json"
SATS = 1e8

PAIRS = [p.strip().upper() for p in os.getenv("RATIO_PAIRS", "XRP/BTC").split(",") if p.strip()]
SNAPSHOT_HOUR = int(os.getenv("RATIO_SNAPSHOT_HOUR", "8"))   # UTC hour for the daily snapshot


def disp(price: float, unit: str) -> str:
    """Readable ratio price. BTC-quoted values (internally sats) are shown in sats
    for small alts and in BTC for large ones (e.g. ETH/BTC), so the number never
    balloons to millions of sats. Non-BTC ratios pass through as-is."""
    if unit != "sats":
        return f"{price:.6g} {unit}"
    btc = price / SATS
    return f"{btc:.5f} BTC" if btc >= 0.001 else f"{price:,.0f} sats"


# ── Data ────────────────────────────────────────────────────────────────
def _spot_klines(symbol: str, interval: str, limit: int = 300) -> tuple | None:
    """(closes, highs, lows) from Binance spot, or None if the symbol/venue fails."""
    try:
        r = requests.get(SPOT, params={"symbol": symbol, "interval": interval,
                                       "limit": limit}, timeout=15)
        if r.status_code != 200:
            return None
        k = r.json()
        if not k:
            return None
        return ([float(x[4]) for x in k], [float(x[2]) for x in k], [float(x[3]) for x in k])
    except Exception:
        return None


def ratio_series(num: str, den: str, interval: str, limit: int = 300) -> tuple:
    """(closes, highs, lows, mode) for {num}/{den}. BTC-quoted ratios are returned
    in sats. Prefers the native {num}{den} spot pair; falls back to synthesizing
    from the USDT legs. Returns (None, mode) if neither source is available."""
    scale = SATS if den == "BTC" else 1.0
    if den in ("BTC", "ETH", "USDT"):                 # common native quote assets
        nat = _spot_klines(num + den, interval, limit)
        if nat:
            c, h, l = nat
            return [x * scale for x in c], [x * scale for x in h], [x * scale for x in l], "native"
    a = _spot_klines(num + "USDT", interval, limit)
    b = _spot_klines(den + "USDT", interval, limit)
    if a and b:
        n = min(len(a[0]), len(b[0]))
        (ac, ah, al), bc = (a[0][-n:], a[1][-n:], a[2][-n:]), b[0][-n:]
        # ratio bars: alt high/low over BTC close (BTC intrabar range is ignored —
        # a fine approximation for a slow positioning chart).
        c = [ac[i] / bc[i] * scale for i in range(n)]
        h = [ah[i] / bc[i] * scale for i in range(n)]
        l = [al[i] / bc[i] * scale for i in range(n)]
        return c, h, l, "synthetic"
    return None, None, None, "none"


def _strong(zones: list) -> dict | None:
    """The dominant zone among the nearest few — most touches, ties broken nearest."""
    return max(zones[:4], key=lambda z: z["touches"], default=None) if zones else None


def evaluate_ratio(pair: str, interval: str = "1h", tlen: int | None = None) -> dict | None:
    """Structure read for a ratio on one timeframe. Returns None on thin history."""
    num, den = pair.split("/")
    tlen = tlen or (ew.TRAMA_LEN_D if interval == "1d" else ew.TRAMA_LEN)
    closes, highs, lows, mode = ratio_series(num, den, interval)
    if not closes or len(closes) < 60:
        return None
    price = closes[-1]
    rsi_list = ew.rsi(closes)
    rsi_now = rsi_list[-1] if rsi_list else 50.0
    tval, tslope, trend = ew.trama_trend(price, ew.trama(closes, highs, lows, length=tlen))
    sr = ew.compute_sr(highs, lows, price, tol=0.008)
    chg1 = (price / closes[-2] - 1) * 100 if len(closes) > 1 else 0.0
    n7 = 8 if interval == "1d" else 25                       # ~7d daily / ~24h hourly
    chg7 = (price / closes[-n7] - 1) * 100 if len(closes) > n7 else 0.0
    return {
        "pair": pair, "num": num, "den": den, "interval": interval, "mode": mode,
        "unit": "sats" if den == "BTC" else den,
        "price": price, "rsi": rsi_now, "trama": tval, "trama_slope": tslope,
        "trend": trend, "above_trama": (price >= tval) if tval else None,
        "sr": sr, "res": _strong(sr["resistance"]), "sup": _strong(sr["support"]),
        "chg1": chg1, "chg7": chg7,
    }


# ── Formatting ──────────────────────────────────────────────────────────
def _p(ev: dict, price: float | None = None) -> str:
    return disp(ev["price"] if price is None else price, ev["unit"])


def _tclr(ev: dict) -> str:
    return "🟢" if ev["above_trama"] else "🔴"


def _read(ev: dict) -> str:
    """One-line plain-English rotation read from trend + TRAMA side."""
    num, den = ev["num"], ev["den"]
    if ev["trend"] == "up" and ev["above_trama"]:
        return f"{num} gaining vs {den} — rotation into {num}."
    if ev["trend"] == "down" and not ev["above_trama"]:
        return f"{num} losing to {den} — {den} outperforming."
    if ev["above_trama"]:
        return f"{num}/{den} firm but not trending — watch for follow-through."
    return f"{num}/{den} weak/ranging — no rotation into {num} yet."


def _levels(ev: dict) -> str:
    u = ev["unit"]
    res = " · ".join(f"{disp(z['price'], u)} ({z['touches']}t)" for z in ev["sr"]["resistance"][:3]) or "—"
    sup = " · ".join(f"{disp(z['price'], u)} ({z['touches']}t)" for z in ev["sr"]["support"][:3]) or "—"
    return f"res {res}  |  sup {sup}"


def format_alert(ev: dict, events: list, ed: dict | None = None) -> str:
    now = datetime.now(timezone.utc).strftime("%H:%M UTC")
    head = " · ".join(events)

    def tf_line(tag, e):
        return (f"  {tag}: {_p(e)} · RSI {e['rsi']:.0f} · TRAMA {disp(e['trama'], e['unit'])} "
                f"({e['trama_slope']:+.1f}%) · price {'above' if e['above_trama'] else 'below'} {_tclr(e)}")

    lines = [f"⚡ *{ev['pair']}* — {head}  ({now})", tf_line("1H", ev)]
    if ed:
        lines.append(tf_line("Daily", ed))
    lines += [f"trend 1H {ev['trend']}  ·  {_levels(ev)}", f"_{_read(ev)}_"]
    if ev["mode"] == "synthetic":
        lines.append("_(synthetic ratio from USDT legs — no native pair)_")
    return "\n".join(lines)


def format_snapshot(reads: list) -> str:
    """reads = list of (pair, ev_1h, ev_1d)."""
    now = datetime.now(timezone.utc).strftime("%H:%M UTC")
    lines = [f"📊 *RATIO SNAPSHOT*  ({now})", ""]
    for pair, e1, ed in reads:
        prim = e1 or ed
        if not prim:
            lines += [f"*{pair}* — no data", ""]
            continue
        lines.append(f"*{pair}*   {_p(prim)}   (24h {prim['chg1'] if prim is e1 else prim['chg7']:+.1f}%)")
        for tf, ev in (("1H", e1), ("1D", ed)):
            if ev:
                lines.append(f"  {tf}: RSI {ev['rsi']:.0f} · trend {ev['trend']} · "
                             f"TRAMA {disp(ev['trama'], ev['unit'])} "
                             f"(price {'above' if ev['above_trama'] else 'below'} {_tclr(ev)})")
        if prim:
            lines.append(f"  {_levels(prim)}")
            lines.append(f"  _{_read(prim)}_")
        lines.append("")
    lines.append("_Alt priced in BTC (small alts in sats ×1e8, larger shown in BTC). "
                 "Positioning read — odds, not certainty._")
    return "\n".join(lines)


# ── State / transitions ─────────────────────────────────────────────────
def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def detect_events(ev: dict, prev: dict | None, tf: str = "1H") -> list:
    """Transitions since the last run, each labelled with the timeframe `tf`
    (e.g. 'Reclaimed Daily TRAMA'). Empty on first sight (seed silently)."""
    if not prev:
        return []
    events = []
    if prev.get("trend") and prev["trend"] != ev["trend"]:
        events.append(f"{tf} TREND FLIP → {ev['trend']}")
    if prev.get("above_trama") is not None and prev["above_trama"] != ev["above_trama"]:
        events.append(f"RECLAIMED {tf} TRAMA" if ev["above_trama"] else f"LOST {tf} TRAMA")
    pp = prev.get("price")
    if pp is not None:
        if ev["res"] and pp < ev["res"]["price"] <= ev["price"]:
            events.append(f"{tf} BROKE {disp(ev['res']['price'], ev['unit'])} shelf ({ev['res']['touches']}t)")
        if ev["sup"] and pp > ev["sup"]["price"] >= ev["price"]:
            events.append(f"{tf} LOST {disp(ev['sup']['price'], ev['unit'])} support ({ev['sup']['touches']}t)")
    return events


def _snapshot(ev: dict) -> dict:
    return {"trend": ev["trend"], "above_trama": ev["above_trama"],
            "price": round(ev["price"], 4), "ts": int(time.time())}


# ── Main ────────────────────────────────────────────────────────────────
def scan_one(pair: str) -> None:
    """Print a one-off read for a single pair (no state, no alert)."""
    e1, ed = evaluate_ratio(pair, "1h"), evaluate_ratio(pair, "1d")
    print(format_snapshot([(pair, e1, ed)]))


def main() -> None:
    print("\n" + "=" * 64)
    print("  RATIO WATCH —", ", ".join(PAIRS))
    print("  " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 64)

    state = _load_state()
    reads, changed = [], False
    for pair in PAIRS:
        if "/" not in pair:
            print(f"  {pair}: not a NUM/DEN ratio — skip"); continue
        e1 = evaluate_ratio(pair, "1h")
        ed = evaluate_ratio(pair, "1d")
        reads.append((pair, e1, ed))
        if not e1:
            print(f"  {pair}: no 1h data (thin history / no pair) — skip"); continue

        # Per-timeframe state under the pair: {"1h": snap, "1d": snap}. Old flat
        # state (pre-timeframe) has no "1h"/"1d" keys → detect_events seeds silently.
        prev = state.get(pair) or {}
        events = detect_events(e1, prev.get("1h"), "1H")
        if ed:
            events += detect_events(ed, prev.get("1d"), "Daily")
        print(f"  {pair}: {disp(e1['price'], e1['unit'])}  RSI {e1['rsi']:.0f}  "
              f"trend {e1['trend']} ({e1['trama_slope']:+.1f}%)  "
              f"{'above' if e1['above_trama'] else 'below'} TRAMA"
              + (f"  →  {' · '.join(events)}" if events else ""))
        if events:
            if ew.send_telegram_private(format_alert(e1, events, ed)):
                print(f"    📣 alert sent ({' · '.join(events)})")
        state[pair] = {"1h": _snapshot(e1), "1d": _snapshot(ed) if ed else None}
        changed = True

    if changed:
        _save_state(state)

    # Daily snapshot (all pairs) at the configured UTC hour — once per day.
    if datetime.now(timezone.utc).hour == SNAPSHOT_HOUR and datetime.now().minute < 20:
        last = state.get("_snapshot_day")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if last != today and any(e1 for _, e1, _ in reads):
            if ew.send_telegram_private(format_snapshot(reads)):
                print("  📊 daily snapshot sent.")
                state["_snapshot_day"] = today
                _save_state(state)


if __name__ == "__main__":
    if len(sys.argv) > 1 and "/" in sys.argv[1]:
        scan_one(sys.argv[1].upper())
    else:
        main()
