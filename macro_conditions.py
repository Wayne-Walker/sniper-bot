#!/usr/bin/env python3
"""
Macro Conditions — a lightweight "don't fight the macro" regime read.
═════════════════════════════════════════════════════════════════════════════

Crypto trades like a long-duration, high-beta risk asset, so it leans with the
macro wind: the US 10-year Treasury yield (the risk-free benchmark / discount
rate) and the dollar (DXY). Both RISING = tightening financial conditions =
headwind for crypto (risk-off). Both FALLING = easing = tailwind (risk-on).

This is a slow, background REGIME input — not a trade trigger. It's recorded on
every tracked setup (so we can later measure whether counter-macro setups lose)
and shown as one line on the alerts/digest.

Data: Yahoo Finance daily closes (no key) — ^TNX (10Y yield, nominal) and
DX-Y.NYB (DXY). Real yields (TIPS) are theoretically cleaner but have no clean
free ticker; nominal 10Y + DXY is a well-understood practical proxy.

Publishes intelligence/macro_conditions to Firebase (shared with cryptex) and
caches reports/macro_conditions.json (read by the exhaustion engine). Fail-safe:
if Yahoo is unreachable it leaves the last good cache in place.

Usage:
  python macro_conditions.py            # fetch, cache, publish (daily cron)
  python macro_conditions.py --show     # print the current read
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

import exhaustion_watch as ew  # firebase handle + private telegram (reused)

CACHE = ROOT / "reports" / "macro_conditions.json"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
UA = {"User-Agent": "Mozilla/5.0"}

# Deadbands for a ~1-month (21 trading day) change — yields are noisier per point
# than the dollar, so the dollar's band is tighter.
YIELD_BAND_PCT = float(os.getenv("MACRO_YIELD_BAND", "2.0"))   # ±% move to count as a trend
DXY_BAND_PCT = float(os.getenv("MACRO_DXY_BAND", "1.0"))


def _yahoo_closes(symbol: str, rng: str = "3mo") -> list | None:
    try:
        r = requests.get(YAHOO.format(symbol),
                         params={"interval": "1d", "range": rng}, headers=UA, timeout=15)
        if r.status_code != 200:
            return None
        q = r.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        closes = [c for c in q if c is not None]
        return closes or None
    except Exception:
        return None


def _trend(closes: list, band_pct: float) -> tuple:
    """(direction, 21d % change). up / down / flat with a deadband."""
    if not closes or len(closes) < 22:
        return "flat", 0.0
    chg = (closes[-1] / closes[-22] - 1) * 100
    d = "up" if chg > band_pct else ("down" if chg < -band_pct else "flat")
    return d, chg


def compute() -> dict | None:
    """Fetch 10Y + DXY and derive the risk regime. None if data unavailable."""
    tnx = _yahoo_closes("^TNX")            # 10Y yield in % (e.g. 4.69)
    dxy = _yahoo_closes("DX-Y.NYB")        # dollar index
    if not tnx or not dxy:
        return None
    y_dir, y_chg = _trend(tnx, YIELD_BAND_PCT)
    d_dir, d_chg = _trend(dxy, DXY_BAND_PCT)

    tighten = (y_dir == "up") + (d_dir == "up")     # rising yields/dollar = tightening
    ease = (y_dir == "down") + (d_dir == "down")
    net = ease - tighten                            # -2..+2 (negative = risk-off)
    if net <= -1:
        regime, note = "risk_off", "tightening — headwind for crypto longs"
    elif net >= 1:
        regime, note = "risk_on", "easing — tailwind for crypto longs"
    else:
        regime, note = "neutral", "mixed — no clear macro edge"
    strength = "strong" if abs(net) == 2 else ("mild" if abs(net) == 1 else "flat")

    return {
        "regime": regime, "strength": strength, "net": net, "note": note,
        "yield_10y": round(tnx[-1], 2), "yield_chg21": round(y_chg, 1), "yield_dir": y_dir,
        "dxy": round(dxy[-1], 2), "dxy_chg21": round(d_chg, 1), "dxy_dir": d_dir,
        "ts": int(time.time()),
        "asof": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


def _save_cache(m: dict) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(m, indent=2), encoding="utf-8")


def publish(m: dict) -> None:
    if not ew._firebase_ready():
        return
    try:
        ew._fb_db.reference("intelligence/macro_conditions").set(m)
        print("  📡 Published intelligence/macro_conditions to Firebase.")
    except Exception as e:
        print(f"  [WARN] Firebase macro publish failed: {type(e).__name__}: {str(e)[:100]}")


def format_line(m: dict) -> str:
    emoji = {"risk_off": "🔴", "risk_on": "🟢", "neutral": "⚪"}.get(m["regime"], "")
    return (f"{emoji} MACRO {m['regime'].replace('_', ' ').upper()} ({m['strength']}) — "
            f"10Y {m['yield_10y']:.2f}% {m['yield_dir']} ({m['yield_chg21']:+.1f}% 1mo), "
            f"DXY {m['dxy']:.1f} {m['dxy_dir']} ({m['dxy_chg21']:+.1f}%)\n{m['note']}")


def main() -> None:
    print("\n" + "=" * 64)
    print("  MACRO CONDITIONS —", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 64)
    m = compute()
    if not m:
        print("  [WARN] macro data unavailable (Yahoo unreachable) — keeping last cache.")
        return
    print("  " + format_line(m).replace("\n", "\n  "))
    _save_cache(m)
    publish(m)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("--show", "--macro"):
        try:
            print(format_line(json.loads(CACHE.read_text())))
        except (OSError, json.JSONDecodeError):
            m = compute()
            print(format_line(m) if m else "Macro data unavailable.")
    else:
        main()
