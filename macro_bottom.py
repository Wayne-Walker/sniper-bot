#!/usr/bin/env python3
"""
Macro Bottom Scanner — cycle "accumulation zone" signal for majors.
═════════════════════════════════════════════════════════════════════════════

A slow, BTC-anchored read of how close each major is to a bear-market bottom.
Unlike exhaustion_watch.py (1h reversal timer), this works on WEEKLY data and a
daily cadence — it tells you *when to start accumulating*, not where to fill.

Scored 0–100 from five free factors (no on-chain/FRED needed):
  • below 200-week MA        (25) — every prior BTC bear bottomed here
  • weekly RSI ≤40 + divergence (15) — washed out and turning
  • weekly structure turn     (20) — higher-low after a lower-low + 20W reclaim
  • Fear & Greed ≤20 sustained (20) — market-wide capitulation (shared)
  • funding persistently negative (20) — crowded shorts on that coin

Zones: <40 wait · 40–59 watch · 60–79 accumulate · ≥80 deep value.
BTC is the anchor — alt zones are shown alongside BTC's zone for context.

Data: Binance SPOT weekly candles (200W MA / RSI / structure) + perp funding +
alternative.me Fear & Greed. Publishes macro/{COIN} to Firebase and alerts the
private Telegram chat on band transitions (dedup via reports/macro_state.json).

Usage:
  python macro_bottom.py                 # default coins (BTC,ETH,SOL,XRP,BNB)
  python macro_bottom.py BTC,ETH         # explicit list
  pm2 start ecosystem.config.js --only macro-bottom
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

# Reuse RSI, swing detection, funding, Firebase + private-Telegram from the watcher.
import exhaustion_watch as ew

SPOT_URL = "https://api.binance.com/api/v3/klines"
FNG_URL = "https://api.alternative.me/fng/"
STATE_FILE = ROOT / "reports" / "macro_state.json"

# ── Tunables ────────────────────────────────────────────────────────────
COINS = [c.strip().upper() for c in
         os.getenv("MACRO_COINS", "BTC,ETH,SOL,XRP,BNB").split(",") if c.strip()]
ZONE_ACCUMULATE = 60          # score to enter the accumulation zone (alert)
ZONE_DEEP = 80                # score for the deep-value band
ZONE_WATCH = 40
FUNDING_WINDOW = 21           # ~7 days of 8h funding periods
DEFENSE_WEEKS = 8             # lookback for 200W flush-and-reclaim defense
CONFIDENCE = {"BTC": "normal", "ETH": "normal", "BNB": "normal",
              "SOL": "reduced", "XRP": "reduced"}
NOTES = {
    "SOL": "young 200W history (~1 cycle) — weight lightly",
    "XRP": "idiosyncratic (regulatory-driven) — doesn't always track BTC's cycle",
    "BNB": "exchange token — historically resilient, scores 'less deep' than L1s",
}
ZONE_RANK = {"wait": 0, "watch": 1, "accumulate": 2, "deep_value": 3}

_LEGEND = f"""  ── HOW TO READ ──────────────────────────────────────────────────
  score 0-100 = how close each coin is to a bear-market bottom.
    wait       (<{ZONE_WATCH})     — not close; keep your powder dry
    watch      ({ZONE_WATCH}-{ZONE_ACCUMULATE - 1})  — capitulation building; get ready, don't buy yet
    ACCUMULATE ({ZONE_ACCUMULATE}-{ZONE_DEEP - 1})  — start DCA-ing in (the "buy" zone)
    DEEP VALUE ({ZONE_DEEP}+)    — rare, historically the best accumulation
  200W = price vs the 200-week moving average (the cycle-bottom line):
    below  — trading under it (deep value)
    defend — wicking below it but closing back above (buyers defending)  🛡️
    above  — untested, no bottom signal there yet
  wRSI = weekly RSI (≤40 = washed out).  [reduced] = SOL/XRP (young/odd history).
  BTC is the anchor — trust an alt bottom more when BTC is also in its zone.
  → Telegram pings ONLY when a coin ENTERS accumulate or a weekly structure
    turn confirms. No ping = nothing has crossed; this log is the full picture.
  ─────────────────────────────────────────────────────────────────"""


def _tg_score_legend() -> str:
    """The 'HOW TO READ' score-band table for the Telegram macro message.
    A monospace code block keeps the columns aligned; derived from the ZONE_*
    thresholds so it never drifts from the scoring."""
    rows = [
        ("wait", f"(<{ZONE_WATCH})", "not close; keep your powder dry"),
        ("watch", f"({ZONE_WATCH}-{ZONE_ACCUMULATE - 1})", "capitulation building; don't buy yet"),
        ("ACCUMULATE", f"({ZONE_ACCUMULATE}-{ZONE_DEEP - 1})", 'start DCA-ing in (the "buy" zone)'),
        ("DEEP VALUE", f"({ZONE_DEEP}+)", "rare, historically best accumulation"),
    ]
    body = "\n".join(f"{lbl:<10} {rng:<7} — {desc}" for lbl, rng, desc in rows)
    return ("📖 *How to read the score* — closeness to a bear-market bottom (0–100):\n"
            "```\n" + body + "\n```")


# ── Data ────────────────────────────────────────────────────────────────
def spot_weekly(coin: str, limit: int = 1000) -> tuple:
    """Weekly spot closes/highs/lows for the 200W MA + RSI + structure."""
    r = requests.get(SPOT_URL,
                     params={"symbol": coin + "USDT", "interval": "1w", "limit": limit},
                     timeout=20)
    r.raise_for_status()
    kl = r.json()
    closes = [float(k[4]) for k in kl]
    highs = [float(k[2]) for k in kl]
    lows = [float(k[3]) for k in kl]
    return closes, highs, lows


def fear_greed_avg(n: int = 14) -> float | None:
    """Trailing n-day average of the crypto Fear & Greed index (market-wide)."""
    try:
        r = requests.get(FNG_URL, params={"limit": n, "format": "json"}, timeout=15)
        r.raise_for_status()
        vals = [int(x["value"]) for x in r.json().get("data", [])]
        return sum(vals) / len(vals) if vals else None
    except Exception:
        return None


def funding_ann_mean(coin: str) -> float | None:
    """Mean annualized perp funding over the trailing window (negative = crowded shorts)."""
    try:
        rates = ew.BINANCE.funding_rates(coin, limit=FUNDING_WINDOW)
        if not rates:
            return None
        return (sum(rates) / len(rates)) * ew.BINANCE.fund_per_year * 100.0
    except Exception:
        return None


def _sma(vals: list, n: int) -> float:
    w = vals[-n:] if len(vals) >= n else vals
    return sum(w) / len(w) if w else 0.0


def _bullish_divergence(closes: list) -> bool:
    """Weekly price lower-low but RSI higher-low over the last two swing lows."""
    r = ew.rsi(closes)
    if len(r) < 10:
        return False
    rec = closes[len(closes) - len(r):]
    idx = ew.local_minima(rec, span=3)
    if len(idx) < 2:
        return False
    t1, t2 = idx[-2], idx[-1]
    return rec[t2] < rec[t1] and r[t2] > r[t1]


def _structure_turn(closes: list, lows: list) -> bool:
    """Higher-low after a lower-low on the weekly + price back above the 20W MA."""
    idx = ew.local_minima(lows, span=3)
    if len(idx) < 2:
        return False
    higher_low = lows[idx[-1]] > lows[idx[-2]]
    reclaim = closes[-1] > _sma(closes, 20)
    return bool(higher_low and reclaim)


# ── Scoring ─────────────────────────────────────────────────────────────
def score_coin(coin: str) -> dict | None:
    try:
        closes, highs, lows = spot_weekly(coin)
    except Exception as e:
        print(f"  [WARN] {coin}: weekly candles failed: {e}")
        return None
    if len(closes) < 30:
        print(f"  [WARN] {coin}: not enough weekly history ({len(closes)})")
        return None

    price = closes[-1]
    ma200 = _sma(closes, 200)
    r = ew.rsi(closes)
    rsi_w = r[-1] if r else 50.0
    fng = fear_greed_avg(14)
    fund_ann = funding_ann_mean(coin)
    diverg = _bullish_divergence(closes)
    struct = _structure_turn(closes, lows)

    # 200W relationship over the last DEFENSE_WEEKS: count weeks that wicked BELOW
    # the 200W MA but CLOSED back above it (buyers defending the level).
    win = min(DEFENSE_WEEKS, len(closes))
    r_lows, r_closes = lows[-win:], closes[-win:]
    reclaim_weeks = sum(1 for lo, cl in zip(r_lows, r_closes) if lo < ma200 and cl > ma200)
    deepest_wick = min((lo / ma200 - 1) * 100 for lo in r_lows) if ma200 else 0.0
    ratio = price / ma200 if ma200 else 2.0

    f = {}
    # 200W factor (30): sustained-BELOW (deep value) OR flush-and-reclaim DEFENSE,
    # whichever is stronger — both are bottoming signals, so we don't punish either.
    if ratio <= 1.0:                       # closed below — deeper = more (full at -20%)
        s200, ma200_mode = 15.0 + 15.0 * min(1.0, (1.0 - ratio) / 0.20), "below"
    elif reclaim_weeks >= 1:               # above, but defending after a flush below
        s200, ma200_mode = min(30.0, 12.0 + reclaim_weeks * 2.5 + min(6.0, -deepest_wick)), "defend"
    else:                                  # above the level, untested
        s200, ma200_mode = max(0.0, 15.0 * (1 - (ratio - 1) / 0.10)), "above"
    f["below_200w"] = round(s200, 1)
    # weekly RSI (15): 10 for washed-out RSI, +5 for bullish divergence
    s_rsi = 10.0 if rsi_w <= 40 else max(0.0, 10.0 * (1 - (rsi_w - 40) / 10))
    f["weekly_rsi"] = round(min(15.0, s_rsi + (5.0 if diverg else 0.0)), 1)
    # structure turn (15)
    f["structure"] = 15.0 if struct else 0.0
    # Fear & Greed (20): full ≤20, fading to 0 by 35
    f["fear_greed"] = round(0.0 if fng is None else
                            (20.0 if fng <= 20 else max(0.0, 20.0 * (1 - (fng - 20) / 15))), 1)
    # funding (20): full ≤ -20%/yr, 0 at/above 0
    f["funding"] = round(0.0 if fund_ann is None else
                         (20.0 if fund_ann <= -20 else max(0.0, 20.0 * (-fund_ann / 20))), 1)

    score = round(sum(f.values()), 1)
    zone = ("deep_value" if score >= ZONE_DEEP else
            "accumulate" if score >= ZONE_ACCUMULATE else
            "watch" if score >= ZONE_WATCH else "wait")

    return {
        "coin": coin, "score": score, "zone": zone, "structure_turn": struct,
        "confidence": CONFIDENCE.get(coin, "normal"), "factors": f,
        "raw": {
            "price": round(price, 4), "ma200": round(ma200, 4),
            "pct_vs_200w": round((ratio - 1) * 100, 1),
            "weekly_rsi": round(rsi_w, 1),
            "fear_greed_14d": round(fng, 1) if fng is not None else None,
            "funding_ann": round(fund_ann, 1) if fund_ann is not None else None,
            "bullish_divergence": diverg, "weeks": len(closes),
            "ma200_mode": ma200_mode,          # below | defend | above
            "reclaim_weeks_8w": reclaim_weeks, # weeks wicked below 200W & closed above
            "deepest_wick_pct": round(deepest_wick, 1),
        },
        "ts": int(time.time()),
    }


# ── State / alerts ──────────────────────────────────────────────────────
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


def format_alert(res: dict, btc: dict | None, kind: str) -> str:
    raw = res["raw"]
    coin, zone = res["coin"], res["zone"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    title = ("🟢 *MACRO STRUCTURE TURN*" if kind == "structure"
             else f"📉 *MACRO {zone.upper().replace('_', ' ')} ZONE*")
    fng = f"{raw['fear_greed_14d']:.0f}" if raw["fear_greed_14d"] is not None else "n/a"
    fund = f"{raw['funding_ann']:+.0f}%/yr" if raw["funding_ann"] is not None else "n/a"
    lines = [
        f"{title} — {coin}   (score {res['score']:.0f}/100)   ({now})",
        f"${raw['price']:,.4f}  |  {raw['pct_vs_200w']:+.0f}% vs 200W MA  |  wRSI {raw['weekly_rsi']:.0f}",
        f"Fear&Greed(14d) {fng}  |  funding {fund}  |  {res['confidence']} confidence",
    ]
    if raw.get("ma200_mode") == "defend":
        lines.append(f"🛡️ 200W *defended* — {raw['reclaim_weeks_8w']}/{DEFENSE_WEEKS} wks wicked "
                     f"below & reclaimed (deepest {raw['deepest_wick_pct']:+.0f}%)")
    elif raw.get("ma200_mode") == "below":
        lines.append(f"200W: sustained below (deepest {raw['deepest_wick_pct']:+.0f}%)")
    if btc and coin != "BTC":
        lines.append(f"BTC anchor: *{btc['zone']}* ({btc['score']:.0f}/100)"
                     + ("  ✅ aligned" if ZONE_RANK[btc["zone"]] >= 2 else "  ⚠️ BTC not bottomed yet"))
    f = res["factors"]
    lines += [
        "",
        (f"Factors — 200W {f['below_200w']:.0f} · wRSI {f['weekly_rsi']:.0f} · "
         f"structure {f['structure']:.0f} · F&G {f['fear_greed']:.0f} · funding {f['funding']:.0f}"),
    ]
    if coin in NOTES:
        lines.append(f"_Note: {NOTES[coin]}._")

    # ── How to read (footer) ──
    if kind == "structure":
        means = "weekly higher-low + 20W reclaim → a bottom is likely forming"
    elif res["zone"] == "deep_value":
        means = "rare deep-value zone → historically the best time to accumulate"
    elif res["zone"] == "accumulate":
        means = "the *buy* zone → start scaling in (DCA), not a single entry"
    else:
        means = "getting closer — not a buy yet"
    lines += [
        "",
        f"📖 *What this means:* {means}.",
        "",
        _tg_score_legend(),
        "_BTC is the anchor — an alt bottom is stronger when BTC is in its zone too._",
        "_200W 🛡️ = price keeps wicking below the 200-week MA and reclaiming (buyers defending)._",
        "_Decision support — odds, not certainty._",
    ]
    return "\n".join(lines)


def publish(payload: dict) -> None:
    if not payload or not ew._firebase_ready():
        return
    try:
        ew._fb_db.reference("macro").update(payload)
        print(f"  📡 Published {len(payload)} macro states to Firebase (macro/).")
    except Exception as e:
        print(f"  [WARN] Firebase macro publish failed: {type(e).__name__}: {str(e)[:120]}")


# ── Main ────────────────────────────────────────────────────────────────
def parse_coins() -> list:
    if len(sys.argv) > 1:
        return [c.strip().upper() for c in sys.argv[1].split(",") if c.strip()]
    return COINS


def main() -> None:
    print("\n" + "=" * 68)
    print("  MACRO BOTTOM SCANNER — cycle accumulation zone (weekly)")
    print("  " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 68)
    print(_LEGEND)

    coins = parse_coins()
    results = {c: score_coin(c) for c in coins}
    btc = results.get("BTC")
    state = _load_state()
    payload, changed = {}, False

    for coin in coins:
        res = results.get(coin)
        if res is None:
            continue
        node = dict(res)
        node["btc_zone"] = btc["zone"] if btc else "unknown"
        payload[coin] = node

        raw = res["raw"]
        dfn = (f" · 200W {raw['ma200_mode']}"
               + (f" {raw['reclaim_weeks_8w']}/{DEFENSE_WEEKS}wk" if raw["ma200_mode"] == "defend" else ""))
        print(f"  {coin:5}: {res['score']:5.1f}/100  {res['zone']:11}  "
              f"({raw['pct_vs_200w']:+.0f}% vs 200W, wRSI {raw['weekly_rsi']:.0f}{dfn})  "
              f"[{res['confidence']}]")

        prev = state.get(coin)
        new_rank = ZONE_RANK[res["zone"]]
        # First time we see a coin → seed state silently (no alert burst on deploy).
        if prev is None:
            state[coin] = {"zone": res["zone"], "structure_turn": res["structure_turn"]}
            changed = True
            continue

        prev_rank = ZONE_RANK.get(prev.get("zone", "wait"), 0)
        # Zone-entry alert: stepped UP into accumulate/deep.
        if new_rank >= ZONE_RANK["accumulate"] and new_rank > prev_rank:
            if ew.send_telegram_private(format_alert(res, btc, "zone")):
                changed = True
                print(f"    📉 {res['zone']} zone alert sent.")
        # Structure-turn alert: flipped false → true.
        if res["structure_turn"] and not prev.get("structure_turn"):
            if ew.send_telegram_private(format_alert(res, btc, "structure")):
                changed = True
                print("    🟢 structure-turn alert sent.")

        state[coin] = {"zone": res["zone"], "structure_turn": res["structure_turn"]}
        changed = True

    publish(payload)
    if changed:
        _save_state(state)


def macro_review_cli(coin_raw: str) -> tuple:
    """On-demand macro read for `--macro <COIN>`. Returns (exit_code, message).
    Validates input; needs enough weekly history for a 200W cycle read."""
    coin = (coin_raw or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{1,15}", coin):
        return 2, "⚠️ Invalid coin — e.g. `/review BTC macro`."
    res = score_coin(coin)
    if res is None:
        return 4, (f"❓ *{coin}* — not enough weekly history for a macro read "
                   f"(needs ~30+ weeks; new/low-cap coins have no 200W cycle).")
    pct = res["raw"]["pct_vs_200w"]
    # A coin well ABOVE its 200W MA is in a bull/parabolic phase — the bottom
    # scanner doesn't apply. Say so plainly instead of a misleading "wait" card.
    if coin not in COINS and pct > 40:
        return 0, (f"📈 *{coin}* is *{pct:+.0f}% above its 200W MA* (wRSI "
                   f"{res['raw']['weekly_rsi']:.0f}) — a bull/parabolic phase, *not* near a cycle "
                   f"bottom. The macro scanner finds *bottoms*, so it doesn't apply here.\n"
                   f"_For a near-term read, use `/review {coin}` (exhaustion / S-R)._")
    btc = res if coin == "BTC" else (score_coin("BTC") or res)
    msg = format_alert(res, btc, "zone")
    if coin not in COINS:
        msg = f"_({coin} isn't a tracked major — macro read is lower-confidence.)_\n\n" + msg
    return 0, msg


if __name__ == "__main__":
    # On-demand macro read: `python macro_bottom.py --macro <COIN>` prints to stdout.
    if len(sys.argv) > 1 and sys.argv[1] == "--macro":
        code, out = macro_review_cli(sys.argv[2] if len(sys.argv) > 2 else "")
        print(out)
        sys.exit(code)
    main()
