#!/usr/bin/env python3
"""
USDT Dominance — the risk-on/off rotation gauge for BTC + alts.
═════════════════════════════════════════════════════════════════════════════

USDT.D = USDT market cap / total crypto market cap. It moves INVERSELY to crypto:
money parked in stables (USDT.D ↑) = risk-off, crypto bleeds; money leaving stables
(USDT.D ↓) = risk-on, BTC + alts rally. So USDT.D's own support/resistance is a
crypto timing tool:

  USDT.D rejecting RESISTANCE / breaking SUPPORT   → 🟢 rally window for BTC/alts
  USDT.D bouncing SUPPORT / breaking RESISTANCE    → 🔴 flight to safety, crypto down

Data: CoinGecko `/global` gives the authoritative CURRENT USDT.D (free). There's no
free source for authoritative USDT.D HISTORY, so S/R is seeded once from a
RECONSTRUCTED backfill — USDT mcap history ÷ Σ(top-N coins' mcap history), ~96%
coverage, then calibrated so "today" matches the authoritative value. Going
forward the real `/global` sample is appended daily and comes to dominate.

Publishes intelligence/usdt_dominance to Firebase and alerts the private chat when
USDT.D is AT a key level — it breaks support/resistance, or tests and rejects one.
Drift states (rising / falling / ranging) update the stored read but stay silent:
they are the slow background, not a timing event. Runs hourly, so a break reaches
Telegram within the hour; the series still stores one point per day (today's sample
is replaced on each run), so S/R is unaffected by the polling rate.

Fail-safe: any fetch failure leaves the last good series in place.

Usage:
  python usdt_dominance.py            # hourly: fetch, backfill if empty, alert, publish
  python usdt_dominance.py --show     # print the current read (no alert)
  python usdt_dominance.py --brief    # plain-text read + levels + 7d/30d change (/usdtd)
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

import exhaustion_watch as ew  # compute_sr / firebase / private telegram

CG = "https://api.coingecko.com/api/v3"
UA = {"User-Agent": "Mozilla/5.0"}
STORE = ROOT / "reports" / "usdt_dominance.json"

BACKFILL_DAYS = int(os.getenv("USDTD_BACKFILL_DAYS", "90"))
BACKFILL_TOPN = int(os.getenv("USDTD_BACKFILL_TOPN", "15"))     # ~94% of total mcap (calibrated anyway)
BACKFILL_PACE_S = float(os.getenv("USDTD_BACKFILL_PACE_S", "2.0"))  # gap between CoinGecko calls
SR_TOL = float(os.getenv("USDTD_SR_TOL", "0.015"))             # cluster tolerance (relative)
NEAR_PCT = float(os.getenv("USDTD_NEAR_PCT", "2.0"))           # within this % of a level = "at" it
TREND_K = int(os.getenv("USDTD_TREND_K", "7"))                 # bars for the trend read

# Only these states are worth a push: USDT.D is AT a key level, either through it
# or rejecting it. Everything else ("rising"/"falling"/"ranging") is drift between
# levels — it moves the stored read but never pings.
BREAK_STATES = {"broke support", "broke resistance"}
TEST_STATES = {"rejecting resistance", "bouncing off support"}
LEVEL_STATES = BREAK_STATES | TEST_STATES

# Flap guard. Hourly polling means a state sitting on a level can oscillate
# (e.g. "rejecting resistance" → "falling" → "rejecting resistance"). Re-alerting
# the SAME level state is suppressed until this many hours have passed; a genuinely
# different level state (a test escalating into a break) always fires immediately.
REALERT_H = float(os.getenv("USDTD_REALERT_H", "24"))


# ── CoinGecko ────────────────────────────────────────────────────────────
def _cg(path: str, params: dict | None = None, tries: int = 3):
    for i in range(tries):
        try:
            r = requests.get(f"{CG}{path}", params=params, headers=UA, timeout=25)
            if r.status_code == 429 and i < tries - 1:
                time.sleep(5 * (i + 1))
                continue
            if r.status_code == 200:
                return r.json()
        except Exception:
            if i < tries - 1:
                time.sleep(3)
    return None


def fetch_current() -> dict | None:
    g = _cg("/global")
    if not g:
        return None
    d = g["data"]
    mp = d.get("market_cap_percentage", {})
    if mp.get("usdt") is None:
        return None
    return {"usdt_d": round(mp["usdt"], 3), "btc_d": round(mp.get("btc", 0), 1),
            "total_mcap": d["total_market_cap"]["usd"]}


def _mcap_hist(coin_id: str, days: int) -> dict:
    """coin_id -> {date: mcap} daily."""
    j = _cg(f"/coins/{coin_id}/market_chart",
            {"vs_currency": "usd", "days": days, "interval": "daily"})
    out = {}
    for ts, mc in (j or {}).get("market_caps", []):
        out[datetime.fromtimestamp(ts / 1000, timezone.utc).strftime("%Y-%m-%d")] = mc
    return out


def reconstruct_backfill(cur_usdt_d: float, days: int = BACKFILL_DAYS,
                         topn: int = BACKFILL_TOPN) -> list:
    """Reconstruct a daily USDT.D history: USDT mcap ÷ Σ(top-N coins' mcap), then
    calibrate so the most recent day equals the authoritative `cur_usdt_d`.
    Returns [{date, usdt_d, src:'recon'}], oldest first. [] on failure."""
    markets = _cg("/coins/markets", {"vs_currency": "usd", "order": "market_cap_desc",
                                     "per_page": topn, "page": 1})
    if not markets:
        return []
    ids = [m["id"] for m in markets]
    usdt_hist = _mcap_hist("tether", days)
    if not usdt_hist:
        return []
    total_by_date = {}
    for cid in ids:
        for date, mc in _mcap_hist(cid, days).items():
            total_by_date[date] = total_by_date.get(date, 0.0) + (mc or 0.0)
        time.sleep(BACKFILL_PACE_S)           # pace CoinGecko's free rate limit
    recon = {}
    for date, umc in usdt_hist.items():
        tot = total_by_date.get(date)
        if tot:
            recon[date] = umc / tot * 100.0
    if not recon:
        return []
    # calibrate to the authoritative current value (removes the ~4% coverage bias)
    latest = max(recon)
    factor = (cur_usdt_d / recon[latest]) if recon[latest] else 1.0
    return [{"date": d, "usdt_d": round(recon[d] * factor, 3), "src": "recon"}
            for d in sorted(recon)]


# ── Store ────────────────────────────────────────────────────────────────
def _load() -> dict:
    if not STORE.exists():
        return {"points": [], "state": None}
    try:
        return json.loads(STORE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"points": [], "state": None}


def _save(data: dict) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ── Signal ───────────────────────────────────────────────────────────────
def usdtd_signal(series: list) -> dict:
    """S/R on the USDT.D series + the INVERSE crypto read. series = list of floats."""
    cur = series[-1]
    sr = ew.compute_sr(series, series, cur, tol=SR_TOL)      # series as highs & lows
    res = sr["resistance"][0]["price"] if sr["resistance"] else None
    sup = sr["support"][0]["price"] if sr["support"] else None
    k = min(TREND_K, len(series) - 1)
    prev = series[-1 - k] if k else cur
    ma = sum(series[-10:]) / len(series[-10:])
    rising = cur > ma and cur > prev
    falling = cur < ma and cur < prev
    near_res = bool(res and cur <= res and (res - cur) / cur * 100 <= NEAR_PCT)
    near_sup = bool(sup and cur >= sup and (cur - sup) / cur * 100 <= NEAR_PCT)

    # 🟢 = bullish for crypto, 🔴 = bearish (USDT.D is inverse)
    if res and prev <= res < cur:
        state, emoji, crypto = "broke resistance", "🔴🔴", "flight to stables — crypto BEARISH"
    elif sup and prev >= sup > cur:
        state, emoji, crypto = "broke support", "🟢🟢", "money leaving stables — crypto BULLISH"
    elif near_res and falling:
        state, emoji, crypto = "rejecting resistance", "🟢", "rally window for BTC/alts (rotation out of stables)"
    elif near_sup and rising:
        state, emoji, crypto = "bouncing off support", "🔴", "risk-off building — crypto bearish"
    elif falling:
        state, emoji, crypto = "falling", "🟢", "supportive for crypto"
    elif rising:
        state, emoji, crypto = "rising", "🔴", "headwind for crypto"
    else:
        state, emoji, crypto = "ranging", "⚪", "no clear rotation"

    return {"usdt_d": round(cur, 2), "state": state, "emoji": emoji, "crypto": crypto,
            "trend": "rising" if rising else "falling" if falling else "flat",
            "resistance": round(res, 2) if res else None,
            "support": round(sup, 2) if sup else None,
            "ts": int(time.time()), "asof": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}


def format_line(sig: dict) -> str:
    lv = []
    if sig["support"]:
        lv.append(f"sup {sig['support']}%")
    if sig["resistance"]:
        lv.append(f"res {sig['resistance']}%")
    return (f"{sig['emoji']} *USDT.D {sig['usdt_d']}%* — {sig['state']} ({sig['trend']})"
            f"{('  ·  ' + ' / '.join(lv)) if lv else ''}\n_{sig['crypto']}_")


def format_alert(sig: dict) -> str:
    """Push text for a level event. Headed so it is distinguishable at a glance from
    the daily read: a break is actionable now, a test is a level holding (so far)."""
    if sig["state"] in BREAK_STATES:
        head = "🚨 *USDT.D — KEY LEVEL BROKEN*"
    else:
        head = "📍 *USDT.D — AT A KEY LEVEL*"
    return f"{head}\n{format_line(sig)}"


def should_alert(sig: dict, prev: dict | None) -> tuple:
    """(send?, why) for the current read. Alerts only on a LEVEL_STATES state, and
    suppresses a repeat of the same level state inside REALERT_H so an oscillating
    level can't spam the chat. `prev` is the last state actually alerted."""
    state = sig["state"]
    if state not in LEVEL_STATES:
        return False, f"{state} — drift, not a level event"
    if not prev or prev.get("state") != state:
        return True, f"level event: {state}"
    age_h = (time.time() - prev.get("ts", 0)) / 3600
    if age_h >= REALERT_H:
        return True, f"level event: {state} (still, {age_h:.0f}h on)"
    return False, f"{state} already alerted {age_h:.1f}h ago (repeat gate {REALERT_H:.0f}h)"


def publish(sig: dict) -> None:
    if not ew._firebase_ready():
        return
    try:
        ew._fb_db.reference("intelligence/usdt_dominance").set(sig)
        print("  📡 Published intelligence/usdt_dominance to Firebase.")
    except Exception as e:
        print(f"  [WARN] Firebase usdt_dominance publish failed: {type(e).__name__}: {str(e)[:100]}")


# ── Main ─────────────────────────────────────────────────────────────────
def _series_floats(data: dict) -> list:
    return [p["usdt_d"] for p in data.get("points", [])]


def format_brief(data: dict) -> str:
    """Plain-text read for the multi-bot `/usdtd` Telegram command. No Markdown —
    that reply path sends without a parse mode, so `*`/`_` would show literally.
    Local cache only (no network), so it answers instantly."""
    pts = data.get("points", [])
    s = [p["usdt_d"] for p in pts]
    if len(s) < 12:
        return "USDT.D history not built yet — run `python usdt_dominance.py`."
    sig = usdtd_signal(s)
    lines = [
        f"{sig['emoji']} USDT.D {sig['usdt_d']}% — {sig['state']} ({sig['trend']})",
        f"→ {sig['crypto']}",
    ]
    lv = []
    if sig["support"]:
        lv.append(f"support {sig['support']}%")
    if sig["resistance"]:
        lv.append(f"resistance {sig['resistance']}%")
    if lv:
        lines.append(f"Levels: {' / '.join(lv)}")
    ch = []
    if len(s) > 7:
        ch.append(f"7d {s[-1] - s[-8]:+.2f}pp")
    if len(s) > 30:
        ch.append(f"30d {s[-1] - s[-31]:+.2f}pp")
    if ch:
        lines.append(f"Change: {' · '.join(ch)}")
    lines += [
        "",
        "USDT.D is INVERSE to crypto: losing support / rejecting resistance = risk-on;",
        "bouncing support / breaking resistance = risk-off.",
        f"As of {pts[-1].get('date', '?')} (updated hourly).",
    ]
    return "\n".join(lines)


def main() -> None:
    print("\n" + "=" * 64)
    print("  USDT DOMINANCE —", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 64)
    cur = fetch_current()
    if not cur:
        print("  [WARN] CoinGecko /global unavailable — keeping last series.")
        return
    data = _load()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # One-time reconstructed backfill so S/R works from day one.
    if len(data["points"]) < 30:
        print(f"  Seeding S/R history — reconstructing ~{BACKFILL_DAYS}d from top "
              f"{BACKFILL_TOPN} coins (one-time, paced)…")
        bf = reconstruct_backfill(cur["usdt_d"])
        have = {p["date"] for p in data["points"]}
        data["points"] = sorted(bf + [p for p in data["points"] if p["date"] not in
                                {b["date"] for b in bf}], key=lambda p: p["date"])
        print(f"  backfilled {len(bf)} days.")

    # Append/replace today's authoritative sample.
    data["points"] = [p for p in data["points"] if p["date"] != today]
    data["points"].append({"date": today, "usdt_d": cur["usdt_d"], "src": "global"})
    data["points"].sort(key=lambda p: p["date"])

    series = _series_floats(data)
    if len(series) < 12:
        print(f"  USDT.D {cur['usdt_d']}% — only {len(series)} points, S/R needs more.")
        _save(data)
        return

    sig = usdtd_signal(series)
    print("  " + format_line(sig).replace("\n", "\n  "))
    publish(sig)

    # Alert on level events only — see should_alert(). `state` tracks the current
    # read every run; `alert` remembers what was last actually pushed, so the flap
    # gate survives restarts.
    send, why = should_alert(sig, data.get("alert"))
    if send:
        if ew.send_telegram_private(format_alert(sig)):
            print(f"    📣 alert sent — {why}.")
            data["alert"] = {"state": sig["state"], "ts": int(time.time())}
        else:
            print(f"    [WARN] alert delivery failed — {why}; will retry next run.")
    else:
        print(f"    (no alert — {why})")
    data["state"] = sig["state"]
    _save(data)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("--show", "--usdtd"):
        d = _load()
        s = _series_floats(d)
        print(format_line(usdtd_signal(s)) if len(s) >= 12
              else "USDT.D history not built yet — run `python usdt_dominance.py`.")
    elif len(sys.argv) > 1 and sys.argv[1] == "--brief":
        print(format_brief(_load()))
    else:
        main()
