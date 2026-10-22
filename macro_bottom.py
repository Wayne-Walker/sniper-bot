#!/usr/bin/env python3
"""
Macro Bottom Scanner — cycle "accumulation zone" signal for majors.
═════════════════════════════════════════════════════════════════════════════

A slow, BTC-anchored read of how close each major is to a bear-market bottom.
Unlike exhaustion_watch.py (1h reversal timer), this works on WEEKLY data and a
daily cadence — it tells you *when to start accumulating*, not where to fill.

Scored 0–100 from five free factors (no on-chain/FRED needed):
  • at/below the 200-week MA (30) — every prior BTC bear bottomed here
  • weekly RSI ≤40 + divergence (15) — washed out and turning
  • weekly structure turn     (15) — higher-low OR a confirmed V-bottom + 20W reclaim
  • Fear & Greed ≤20 sustained (20) — market-wide capitulation (shared)
  • funding persistently negative (20) — crowded shorts on that coin

Zones: <40 wait · 40–59 watch · 60–79 accumulate · ≥80 deep value.
BTC is the anchor — alt zones are shown alongside BTC's zone for context.

The score is UNSIGNED — every factor measures distress happening now, and distress
ends at the low, so it collapses in the week price turns and 0/100 reads the same
before a bottom and after one. `cycle_phase()` supplies that missing direction
(markup · markdown · capitulation · basing · recovery) from the same candles, and
a move into `recovery` is its own alert: the low is behind us.

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
from datetime import datetime, timedelta, timezone
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
DEFEND_MAX_RATIO = 1.10       # price/200W above which a flush-and-reclaim stops counting
CONFIDENCE = {"BTC": "normal", "ETH": "normal", "BNB": "normal",
              "SOL": "reduced", "XRP": "reduced"}
NOTES = {
    "SOL": "young 200W history (~1 cycle) — weight lightly",
    "XRP": "idiosyncratic (regulatory-driven) — doesn't always track BTC's cycle",
    "BNB": "exchange token — historically resilient, scores 'less deep' than L1s",
}
ZONE_RANK = {"wait": 0, "watch": 1, "accumulate": 2, "deep_value": 3}
# Daily early-turn check — the weekly structure test needs 3 bars either side of a
# swing low, so on weekly candles it confirms a bottom ~3 weeks after the low prints.
# The same test on DAILY candles confirms in ~3 days. It earns no score (daily noise
# must never move the cycle read) and only runs for coins already close to the zone,
# so it costs one extra request per qualifying coin — usually none.
EARLY_TURN_MIN_SCORE = float(os.getenv("MACRO_EARLY_TURN_SCORE", "50"))
EARLY_TURN_MA = 50            # daily bars — the MA a daily turn must reclaim

# ── Cycle phase ─────────────────────────────────────────────────────────
# The 0-100 score is UNSIGNED: every factor in it (below-200W, washed-out RSI,
# fear, negative funding) measures distress happening NOW, and distress ends at
# the low. So the score is structurally guaranteed to collapse in the week price
# turns — BTC 2026 went 48 → 5 in one week — and a 0/100 "wait" then reads
# identically whether the bottom is 12 weeks BEHIND us or a year ahead.
# The phase supplies that missing direction, entirely from the weekly candles we
# already fetched (no extra request): where the cycle high and low are, how far
# price has come off the low, and whether it has reclaimed the 20W MA.
CYCLE_LOOKBACK = 156          # weeks (~3y, about one cycle) to locate the cycle high
MIN_BEAR_DD = 25.0            # % drawdown off the cycle high before a bear counts
RECOVERY_RALLY = 20.0         # % off the low required to call a recovery
CONFIRM_WEEKS = 4             # weeks the low must be behind us before it is "confirmed"
MARKUP_NEAR_HIGH = 10.0       # within this % of the cycle high = markup, no bottom in play
# V-bottom confirmation. The higher-low test compares the last two weekly swing
# lows, so a low that is simply never retested can NEVER confirm: the cycle low is
# the most recent swing low and it is lower than the one before it, so the test
# returns False until a new swing low prints above it. BTC 2026 is the case —
# low $57,800 (2026-06-29), +45% off it and the 20W reclaimed, and the weekly test
# still read False. That is a permanent miss, strictly worse than a late one, so a
# V is confirmed on time + rally off the lowest low of the recent window instead.
V_TURN_RALLY = 20.0           # % off the swing low before a V counts as turned
V_TURN_WEEKS = 4              # weeks the low must be behind us (not a live low)
V_TURN_LOOKBACK = 26          # the low must be the lowest in this many weeks, and this recent
PHASE_LABEL = {
    "markup":       "📈 MARKUP",
    "markdown":     "📉 MARKDOWN",
    "capitulation": "🩸 CAPITULATION",
    "basing":       "🧱 BASING",
    "recovery":     "🌱 RECOVERY",
}
PHASE_MEANS = {
    "markup":       "price is at/near its cycle high — there is no bottom in play; "
                    "a 0/100 score here means *the top side*, not a missed entry",
    "markdown":     "still falling — the low is not in yet, keep powder dry",
    "capitulation": "the flush is happening NOW — this is the window the score is built for",
    "basing":       "the low is behind us but unconfirmed — price is building a base, "
                    "not yet recovering",
    "recovery":     "the low is *behind* us — a low score here means the bottom has "
                    "PASSED, not that it is still ahead",
}

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
  cycle = WHICH SIDE of the bottom the score is on (the score itself can't say):
    markup       — at/near the cycle high; no bottom in play
    markdown     — still falling, the low is not in
    capitulation — the flush is happening now (this is the score's window)
    basing       — low is behind us but unconfirmed
    recovery     — low is behind us and price has turned; a LOW score here means
                   the bottom has PASSED, not that it is still ahead
  BTC is the anchor — trust an alt bottom more when BTC is also in its zone.
  → Telegram pings ONLY when a coin ENTERS accumulate, a weekly structure turn
    confirms, or the cycle enters RECOVERY. No ping = nothing has crossed;
    this log is the full picture.
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
    """Weekly spot closes/highs/lows + open times for the 200W MA, RSI, structure
    and the cycle-phase read."""
    r = requests.get(SPOT_URL,
                     params={"symbol": coin + "USDT", "interval": "1w", "limit": limit},
                     timeout=20)
    r.raise_for_status()
    kl = r.json()
    closes = [float(k[4]) for k in kl]
    highs = [float(k[2]) for k in kl]
    lows = [float(k[3]) for k in kl]
    times = [datetime.fromtimestamp(k[0] / 1000, timezone.utc) for k in kl]
    return closes, highs, lows, times


def spot_daily(coin: str, limit: int = 260) -> tuple:
    """Daily spot closes/lows for the early (daily) structure turn."""
    r = requests.get(SPOT_URL,
                     params={"symbol": coin + "USDT", "interval": "1d", "limit": limit},
                     timeout=20)
    r.raise_for_status()
    kl = r.json()
    return [float(k[4]) for k in kl], [float(k[3]) for k in kl]


def early_structure_turn(coin: str) -> bool:
    """The weekly `_structure_turn` test run on DAILY candles: a higher low after a
    lower low, plus a close back above the 50-day MA. Never throws — an early read
    failing must not cost the weekly score its run."""
    try:
        closes, lows = spot_daily(coin)
    except Exception:
        return False
    if len(closes) < EARLY_TURN_MA + 10:
        return False
    idx = ew.local_minima(lows, span=3)
    if len(idx) < 2:
        return False
    return bool(lows[idx[-1]] > lows[idx[-2]] and closes[-1] > _sma(closes, EARLY_TURN_MA))


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


_FNG_HISTORY: dict | None = None


def fng_history() -> dict:
    """Full daily Fear & Greed history as {date: value}, fetched once per process
    and shared by every coin. Empty dict on failure — the reconstruction below
    then simply reports a lower floor, it never fails the run."""
    global _FNG_HISTORY
    if _FNG_HISTORY is None:
        try:
            r = requests.get(FNG_URL, params={"limit": 0, "format": "json"}, timeout=25)
            r.raise_for_status()
            _FNG_HISTORY = {
                datetime.fromtimestamp(int(x["timestamp"]), timezone.utc).date(): int(x["value"])
                for x in r.json().get("data", [])
            }
        except Exception:
            _FNG_HISTORY = {}
    return _FNG_HISTORY


def _fng_avg_at(week_open: datetime, hist: dict) -> float | None:
    """The trailing-14d F&G average as it stood at that week's CLOSE."""
    end = (week_open + timedelta(days=6)).date()
    vals = [hist[end - timedelta(days=k)] for k in range(14) if end - timedelta(days=k) in hist]
    return sum(vals) / len(vals) if len(vals) >= 7 else None


def capitulation_peak(closes: list, lows: list, times: list, since: int) -> tuple:
    """Re-score every week from `since` to now and return (peak score, its week).

    This recovers the one number the scanner used to compute daily and throw away:
    how much capitulation the cycle actually printed. Without it a post-bottom card
    can say the low is behind us but not whether it was ever *washed out*.

    Deliberately a FLOOR, always rendered with "≥": it rebuilds the 200W, RSI and
    structure factors from the candles already in hand plus one cached F&G request,
    but NOT funding (no free weekly history deep enough), so it can only understate
    by up to 20 points. Costs no per-coin request."""
    hist = fng_history()
    best, best_wk = 0.0, None
    for i in range(max(since, 30), len(closes)):
        c, lo = closes[:i + 1], lows[:i + 1]
        ma = _sma(c, 200)
        if not ma:
            continue
        ratio = c[-1] / ma
        win = min(DEFENSE_WEEKS, len(c))
        rl, rc = lo[-win:], c[-win:]
        rw = sum(1 for l, x in zip(rl, rc) if l < ma and x > ma)
        dw = min((l / ma - 1) * 100 for l in rl)
        s_below = 15.0 + 15.0 * min(1.0, (1.0 - ratio) / 0.20) if ratio <= 1.0 else 0.0
        s_above = max(0.0, 15.0 * (1 - (ratio - 1) / 0.10)) if ratio > 1.0 else 0.0
        prox = min(1.0, max(0.0, 1.0 - (ratio - 1.0) / (DEFEND_MAX_RATIO - 1.0)))
        s_def = min(30.0, 12.0 + rw * 2.5 + min(6.0, -dw)) * prox if rw >= 1 else 0.0
        s200 = max(s_below if ratio <= 1.0 else s_above, s_def)
        rr = ew.rsi(c)
        rsi_w = rr[-1] if rr else 50.0
        s_rsi = 10.0 if rsi_w <= 40 else max(0.0, 10.0 * (1 - (rsi_w - 40) / 10))
        if _bullish_divergence(c):
            s_rsi = min(15.0, s_rsi + 5.0)
        s_st = 15.0 if _structure_turn(c, lo) else 0.0
        g = _fng_avg_at(times[i], hist)
        s_fg = 0.0 if g is None else (20.0 if g <= 20 else max(0.0, 20.0 * (1 - (g - 20) / 15)))
        tot = s200 + s_rsi + s_st + s_fg
        if tot > best:
            best, best_wk = tot, times[i]
    return round(best, 1), (best_wk.strftime("%Y-%m-%d") if best_wk else None)


def cycle_phase(closes: list, highs: list, lows: list, times: list, score: float) -> dict:
    """Where in the cycle this coin sits — the DIRECTION the 0-100 score lacks.

    Locates the cycle high over CYCLE_LOOKBACK weeks, then the lowest low after it,
    and reads the recovery off that low. All from candles already fetched."""
    n = len(closes)
    win = min(CYCLE_LOOKBACK, n)
    hi_i = max(range(n - win, n), key=lambda i: highs[i])
    lo_i = min(range(hi_i, n), key=lambda i: lows[i])
    high, low, price = highs[hi_i], lows[lo_i], closes[-1]
    drawdown = (low / high - 1) * 100 if high else 0.0
    off_high = (price / high - 1) * 100 if high else 0.0
    off_low = (price / low - 1) * 100 if low else 0.0
    weeks_since_low = n - 1 - lo_i
    reclaimed_20w = price > _sma(closes, 20)

    if -drawdown < MIN_BEAR_DD or -off_high <= MARKUP_NEAR_HIGH:
        # No meaningful drawdown in the lookback, or price is back at the highs.
        phase = "markup"
    elif (weeks_since_low >= CONFIRM_WEEKS and off_low >= RECOVERY_RALLY and reclaimed_20w):
        phase = "recovery"
    elif score >= ZONE_WATCH:
        phase = "capitulation"          # the distress is happening right now
    elif weeks_since_low < CONFIRM_WEEKS:
        phase = "markdown"              # the low is still live/unconfirmed
    else:
        phase = "basing"

    peak, peak_wk = (capitulation_peak(closes, lows, times, hi_i)
                     if phase != "markup" else (0.0, None))
    return {
        "phase": phase,
        "cycle_high": round(high, 4), "cycle_high_week": times[hi_i].strftime("%Y-%m-%d"),
        "cycle_low": round(low, 4), "cycle_low_week": times[lo_i].strftime("%Y-%m-%d"),
        "drawdown_pct": round(drawdown, 1),
        "off_high_pct": round(off_high, 1),
        "off_low_pct": round(off_low, 1),
        "weeks_since_low": weeks_since_low,
        "reclaimed_20w": reclaimed_20w,
        "peak_score": peak,              # a FLOOR — funding excluded, render with "≥"
        "peak_score_week": peak_wk,
    }


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


def turn_kind(closes: list, lows: list) -> str | None:
    """Which bottoming structure the weekly chart has confirmed, if any.

      "higher_low" — the classic: a higher low after a lower low, 20W reclaimed.
      "v_bottom"   — the case the higher-low test can never see (see V_TURN_*):
                     the lowest low of the last V_TURN_LOOKBACK weeks, at least
                     V_TURN_WEEKS behind us, with price V_TURN_RALLY% off it.

    Both require the 20W reclaim, so neither fires on a low that is still falling."""
    idx = ew.local_minima(lows, span=3)
    if not idx or closes[-1] <= _sma(closes, 20):
        return None
    last = idx[-1]
    if len(idx) >= 2 and lows[last] > lows[idx[-2]]:
        return "higher_low"
    weeks = len(lows) - 1 - last
    window = lows[-V_TURN_LOOKBACK:] if len(lows) >= V_TURN_LOOKBACK else lows
    rally = (closes[-1] / lows[last] - 1) * 100 if lows[last] else 0.0
    if (lows[last] <= min(window) and V_TURN_WEEKS <= weeks < V_TURN_LOOKBACK
            and rally >= V_TURN_RALLY):
        return "v_bottom"
    return None


def _structure_turn(closes: list, lows: list) -> bool:
    """Has a weekly bottoming structure confirmed — either shape. Kept as a bool
    because it is an all-or-nothing scoring factor; `turn_kind` says which."""
    return turn_kind(closes, lows) is not None


# ── Scoring ─────────────────────────────────────────────────────────────
def score_coin(coin: str) -> dict | None:
    try:
        closes, highs, lows, times = spot_weekly(coin)
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
    tkind = turn_kind(closes, lows)
    struct = tkind is not None

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
    # Scored as three candidates, strongest wins — the branch ORDER must not decide
    # it, or a coin oscillating around the MA swings ~15 pts on which side it closed.
    # BELOW: closed under the level — deeper = more (full at -20%).
    s_below = 15.0 + 15.0 * min(1.0, (1.0 - ratio) / 0.20) if ratio <= 1.0 else 0.0
    # ABOVE: untested baseline, fades from 15 at the MA to 0 by +10%.
    s_above = max(0.0, 15.0 * (1 - (ratio - 1) / 0.10)) if ratio > 1.0 else 0.0
    # DEFEND: flush-and-reclaim, but only while price is still AT the level. A week
    # that wicked below and closed far above is a breakout leaving the MA behind, not
    # buyers defending it — ungated, that one candle kept paying ~20 pts for the whole
    # DEFENSE_WEEKS window (BTC 2026-09: +28% vs 200W, still flagged 🛡️ defended).
    # Fades on the same runway as s_above, so neither boundary has a cliff.
    prox = min(1.0, max(0.0, 1.0 - (ratio - 1.0) / (DEFEND_MAX_RATIO - 1.0)))
    s_defend = (min(30.0, 12.0 + reclaim_weeks * 2.5 + min(6.0, -deepest_wick)) * prox
                if reclaim_weeks >= 1 else 0.0)
    # below/above are mutually exclusive by ratio, so they form one positional base
    # that also takes ties — otherwise an all-zero score (>+10%) mislabels as "below".
    base = (s_below, "below") if ratio <= 1.0 else (s_above, "above")
    s200, ma200_mode = max(base, (s_defend, "defend"), key=lambda c: c[0])
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
    # Direction for the unsigned score — see the "Cycle phase" tunables.
    cycle = cycle_phase(closes, highs, lows, times, score)

    return {
        "coin": coin, "score": score, "zone": zone, "structure_turn": struct,
        "turn_kind": tkind,          # higher_low | v_bottom | None
        "confidence": CONFIDENCE.get(coin, "normal"), "factors": f, "cycle": cycle,
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


def attach_early_turn(res: dict) -> dict:
    """Set res["early_turn"] — the daily read, gated so it only costs a request for a
    coin already near the zone and whose weekly turn hasn't confirmed yet. Mutates
    and returns `res` (one place, so the scanner and the on-demand card agree)."""
    res["early_turn"] = bool(res["score"] >= EARLY_TURN_MIN_SCORE
                             and not res["structure_turn"]
                             and early_structure_turn(res["coin"]))
    return res


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


def _state_row(res: dict) -> dict:
    """The per-coin dedup record. One builder so the seed path and the update path
    can never drift apart."""
    return {"zone": res["zone"], "structure_turn": res["structure_turn"],
            "early_turn": res["early_turn"],
            "phase": (res.get("cycle") or {}).get("phase")}


def format_alert(res: dict, btc: dict | None, kind: str) -> str:
    raw = res["raw"]
    coin, zone = res["coin"], res["zone"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    phase = (res.get("cycle") or {}).get("phase")
    title = ("🟢 *MACRO STRUCTURE TURN*" if kind == "structure"
             else "🟡 *MACRO EARLY TURN (daily)*" if kind == "early"
             else f"🌱 *MACRO CYCLE PHASE — {(phase or '').upper()}*" if kind == "phase"
             else f"📉 *MACRO {zone.upper().replace('_', ' ')} ZONE*")
    fng = f"{raw['fear_greed_14d']:.0f}" if raw["fear_greed_14d"] is not None else "n/a"
    fund = f"{raw['funding_ann']:+.0f}%/yr" if raw["funding_ann"] is not None else "n/a"
    lines = [
        f"{title} — {coin}   (score {res['score']:.0f}/100)   ({now})",
        f"${raw['price']:,.4f}  |  {raw['pct_vs_200w']:+.0f}% vs 200W MA  |  wRSI {raw['weekly_rsi']:.0f}",
        f"Fear&Greed(14d) {fng}  |  funding {fund}  |  {res['confidence']} confidence",
    ]
    lines += _cycle_lines(res.get("cycle"))
    if raw.get("ma200_mode") == "defend":
        lines.append(f"🛡️ 200W *defended* — {raw['reclaim_weeks_8w']}/{DEFENSE_WEEKS} wks wicked "
                     f"below & reclaimed (deepest {raw['deepest_wick_pct']:+.0f}%)")
    elif raw.get("ma200_mode") == "below":
        lines.append(f"200W: sustained below (deepest {raw['deepest_wick_pct']:+.0f}%)")
    if btc and coin != "BTC":
        # The anchor reads BTC's PHASE as well as its zone: "not bottomed yet" is
        # wrong for a BTC that is already in recovery, it just isn't cheap any more.
        btc_phase = (btc.get("cycle") or {}).get("phase")
        if ZONE_RANK[btc["zone"]] >= 2:
            anchor = "  ✅ aligned — BTC is in its own buy zone"
        elif btc_phase == "recovery":
            anchor = "  ✅ BTC is already past its cycle low (but out of the buy zone)"
        elif btc_phase == "markup":
            anchor = "  ⚠️ BTC is at cycle highs — no bottom in play"
        else:
            anchor = "  ⚠️ BTC has not bottomed yet"
        lines.append(f"BTC anchor: *{btc['zone']}* ({btc['score']:.0f}/100"
                     + (f", {btc_phase}" if btc_phase else "") + ")" + anchor)
    f = res["factors"]
    tk = {"higher_low": " (higher low)", "v_bottom": " (V-bottom)"}.get(res.get("turn_kind"), "")
    lines += [
        "",
        (f"Factors — 200W {f['below_200w']:.0f} · wRSI {f['weekly_rsi']:.0f} · "
         f"structure {f['structure']:.0f}{tk} · F&G {f['fear_greed']:.0f} · "
         f"funding {f['funding']:.0f}"),
    ]
    if coin in NOTES:
        lines.append(f"_Note: {NOTES[coin]}._")

    if kind != "early" and res.get("early_turn"):
        lines.append("🟡 Daily early turn: daily higher-low + 50D reclaim "
                     "(weekly structure not confirmed yet)")

    # ── How to read (footer) ──
    if kind == "phase":
        means = PHASE_MEANS.get(phase, "cycle phase changed")
    elif kind == "early":
        means = ("the DAILY chart turned first — higher-low + 50D reclaim — while the "
                 "weekly has *not* confirmed. An early warning, ~3 weeks ahead of the "
                 "weekly test; size accordingly and wait for the weekly turn to add")
    elif kind == "structure":
        means = ("weekly V-bottom confirmed — the low was never retested, so no higher "
                 "low will print; confirmed instead on time + a 20%+ rally off it"
                 if res.get("turn_kind") == "v_bottom"
                 else "weekly higher-low + 20W reclaim → a bottom is likely forming")
    elif res["zone"] == "deep_value":
        means = "rare deep-value zone → historically the best time to accumulate"
    elif res["zone"] == "accumulate":
        means = "the *buy* zone → start scaling in (DCA), not a single entry"
    elif res["zone"] == "watch":
        means = "getting closer — not a buy yet"
    else:
        # A low score on its own is ambiguous — it is the SAME 0/100 whether the
        # bottom is a year ahead or three months behind. The phase is what says
        # which, so it speaks here whenever we have one.
        means = PHASE_MEANS.get(phase, "not close to a bottom — keep your powder dry")
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


def _cycle_lines(cyc: dict | None) -> list:
    """The cycle-phase block. This is what stops a 0/100 reading being ambiguous:
    it says whether the bottom is ahead of us or behind us."""
    if not cyc:
        return []
    out = [f"\n*Cycle:* {PHASE_LABEL.get(cyc['phase'], cyc['phase'])}"]
    if cyc["phase"] == "markup":
        out.append(f"At/near the cycle high ({cyc['off_high_pct']:+.0f}% from "
                   f"${cyc['cycle_high']:,.0f}, {cyc['cycle_high_week']}) — no bottom in play.")
        return out
    out.append(f"High ${cyc['cycle_high']:,.0f} ({cyc['cycle_high_week']}) → "
               f"low ${cyc['cycle_low']:,.0f} ({cyc['cycle_low_week']}), "
               f"{cyc['drawdown_pct']:+.0f}%")
    out.append(f"Now {cyc['off_low_pct']:+.0f}% off that low, {cyc['weeks_since_low']}w later"
               f"  |  20W MA {'reclaimed ✅' if cyc['reclaimed_20w'] else 'not reclaimed ❌'}")
    if cyc["peak_score_week"]:
        out.append(f"Peak capitulation reading *≥{cyc['peak_score']:.0f}/100* "
                   f"({cyc['peak_score_week']})"
                   + ("  — reached the buy zone" if cyc["peak_score"] >= ZONE_ACCUMULATE
                      else "  — never reached the buy zone"))
    return out


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
        attach_early_turn(res)
        node = dict(res)
        node["btc_zone"] = btc["zone"] if btc else "unknown"
        node["btc_phase"] = (btc.get("cycle") or {}).get("phase", "unknown") if btc else "unknown"
        payload[coin] = node

        raw = res["raw"]
        dfn = (f" · 200W {raw['ma200_mode']}"
               + (f" {raw['reclaim_weeks_8w']}/{DEFENSE_WEEKS}wk" if raw["ma200_mode"] == "defend" else ""))
        cyc = res.get("cycle") or {}
        print(f"  {coin:5}: {res['score']:5.1f}/100  {res['zone']:11}  {cyc.get('phase',''):13}"
              f"({raw['pct_vs_200w']:+.0f}% vs 200W, wRSI {raw['weekly_rsi']:.0f}{dfn})  "
              f"[{res['confidence']}]"
              + (f"  🟢 {res['turn_kind']}" if res.get("turn_kind") else "")
              + ("  🟡 early daily turn" if res["early_turn"] else ""))
        if cyc.get("phase") not in (None, "markup"):
            print(f"         cycle: {cyc['drawdown_pct']:+.0f}% to ${cyc['cycle_low']:,.0f} "
                  f"({cyc['cycle_low_week']}), now {cyc['off_low_pct']:+.0f}% off it "
                  f"{cyc['weeks_since_low']}w later · peak capitulation ≥{cyc['peak_score']:.0f}")

        prev = state.get(coin)
        new_rank = ZONE_RANK[res["zone"]]
        # First time we see a coin → seed state silently (no alert burst on deploy).
        if prev is None:
            state[coin] = _state_row(res)
            changed = True
            continue

        prev_rank = ZONE_RANK.get(prev.get("zone", "wait"), 0)
        new_phase = (res.get("cycle") or {}).get("phase")
        # Zone-entry alert: stepped UP into accumulate/deep.
        if new_rank >= ZONE_RANK["accumulate"] and new_rank > prev_rank:
            if ew.send_telegram_private(format_alert(res, btc, "zone")):
                changed = True
                print(f"    📉 {res['zone']} zone alert sent.")
        # Structure-turn alert: flipped false → true. Suppressed in `recovery`,
        # where the 🌱 phase alert already says the low is behind us — a V-bottom
        # confirms on nearly the same conditions (rally off the low + 20W reclaim),
        # so without this one event would ping twice.
        if (res["structure_turn"] and not prev.get("structure_turn")
                and new_phase != "recovery"):
            if ew.send_telegram_private(format_alert(res, btc, "structure")):
                changed = True
                print("    🟢 structure-turn alert sent.")
        # Early (daily) turn: flipped false → true, and the weekly hasn't confirmed —
        # a heads-up that the daily chart turned first, not a zone call.
        elif res["early_turn"] and not prev.get("early_turn"):
            if ew.send_telegram_private(format_alert(res, btc, "early")):
                changed = True
                print("    🟡 early (daily) turn alert sent.")
        # Cycle phase: alert ONLY on entering `recovery` — "the low is behind us".
        # That is the read the score can never give, because every factor in it has
        # already decayed by the time it is true. Other transitions update state
        # silently; they are gradual and would just be noise.
        # `prev` written before this feature has no "phase" key at all — seed it
        # silently rather than treating a missing key as a transition.
        if "phase" in prev and new_phase == "recovery" and prev.get("phase") != "recovery":
            if ew.send_telegram_private(format_alert(res, btc, "phase")):
                changed = True
                print("    🌱 cycle-phase RECOVERY alert sent.")

        state[coin] = _state_row(res)
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
    attach_early_turn(res)
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
