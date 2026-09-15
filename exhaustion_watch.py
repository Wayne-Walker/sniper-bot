#!/usr/bin/env python3
"""
Exhaustion Watcher — reversal early-warning for Hyperliquid perps (both ways).
═════════════════════════════════════════════════════════════════════════════

Polls Hyperliquid for a coin (default UNI) and fires a Telegram alert when a
strong move starts showing *exhaustion* — the conditions that historically
precede a reversal. It auto-detects the move's direction and scores the right
side:

  TOP (pumping) — looking for a pullback:
    • overbought      — RSI(14) stretched (>=70, peaked >=75)
    • divergence      — price higher high but RSI lower high (bearish)
    • funding_hot     — funding annualized high (crowded longs)
    • funding_accel   — funding rising fast (longs piling in — leads the top)
    • volume_climax   — huge-volume bar that fails to make a new high (blow-off)
    • stalling        — hugging the high but closes rolling over

  BOTTOM (sold off) — looking for a bounce:
    • oversold        — RSI(14) washed out (<=30, troughed <=25)
    • divergence      — price lower low but RSI higher low (bullish)
    • funding_cold    — funding deeply negative (crowded shorts)
    • funding_accel   — funding dropping fast (shorts piling in — leads the low)
    • volume_climax   — huge-volume bar that fails to make a new low (capitulation)
    • basing          — hugging the low but closes turning up

A "gate" must be true first — a real up-move (pump) or down-move (dump) — so it
stays quiet during chop and only speaks when there's a move worth fading.

[reversal] Cross-exchange confluence (Hyperliquid + Binance):
  Each coin is scored independently on every venue it trades on, funding
  normalized to annualized % (HL settles hourly, Binance every 8h).
    • Dual-listed coin (on BOTH) — STRICT AND: both venues must agree on
      direction AND each independently clear MIN_SIGNALS before it alerts.
      One venue alone is never enough. The alert shows both venues' signals.
    • Single-venue coin (e.g. TLM/VET on Binance only) — no partner to confirm
      with, so it falls back to single-source: alerts on that one venue's own
      MIN_SIGNALS, tagged "<venue> only — no confluence partner".

Dedup: each coin alerts at most once per COOLDOWN_HOURS per direction, UNLESS
the signal count increases (escalation) or the direction flips — then it
re-alerts immediately. State lives in reports/exhaustion_state.json.

Usage:
  python exhaustion_watch.py                 # default coins (UNI)
  python exhaustion_watch.py SOL,UNI,ARB     # explicit list
  COIN=UNI python exhaustion_watch.py        # via env
  pm2 start ecosystem.config.js              # on schedule (see exhaustion-watch)
"""

import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

try:                                   # optional — publishing is a no-op without it
    import firebase_admin
    from firebase_admin import credentials as _fb_credentials, db as _fb_db
except ImportError:
    firebase_admin = None

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")  # so EXHAUSTION_COINS / COIN can come from .env

from morning_briefing import send_telegram

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
STATE_FILE = ROOT / "reports" / "exhaustion_state.json"

# ── Tunables ────────────────────────────────────────────────────────────
COOLDOWN_HOURS = 6          # min gap between repeat alerts for same coin
MIN_SIGNALS = 2             # how many signals must fire to alert
RSI_OVERBOUGHT = 70.0       # RSI(14) 1h overbought line (top)
RSI_PEAK = 75.0             # require RSI to have *peaked* here in last 24h (top)
RSI_OVERSOLD = 30.0         # RSI(14) 1h oversold line (bottom)
RSI_TROUGH = 25.0           # require RSI to have *troughed* here in last 24h (bottom)
FUNDING_ANN_HOT = 35.0      # annualized % funding considered "crowded longs"
FUNDING_ANN_COLD = -35.0    # annualized % funding considered "crowded shorts"
FUNDING_ACCEL_ANN = 20.0    # |pp/yr| jump (recent vs prior 6h) = traders piling in
VOL_CLIMAX_MULT = 3.0       # bar volume vs baseline to count as a climax
PUMP_24H_PCT = 8.0          # 24h move that opens the "pump gate"
DUMP_24H_PCT = -8.0         # 24h move that opens the "dump gate"
NEAR_EXTREME_PCT = 3.0      # within this % of the 72h high/low also opens the gate
# Default coin list when none is passed on the CLI / via COIN — set in .env as
# EXHAUSTION_COINS (comma-separated), e.g. EXHAUSTION_COINS=UNI,SOL,BTC
DEFAULT_COINS = [c.strip().upper()
                 for c in os.getenv("EXHAUSTION_COINS", "UNI").split(",")
                 if c.strip()]


# ── Data sources ────────────────────────────────────────────────────────
# Each source exposes the same primitives (universe / ctx / candles /
# funding_rates) so the evaluator is venue-agnostic. Funding is normalized to
# an annualized % via `fund_per_year`, because HL settles funding hourly while
# Binance settles every 8h — without this, Binance funding would read ~8x too
# hot and trip false signals.
#
# Performance: per-run whole-market data (HL metaAndAssetCtxs; Binance
# premiumIndex + 24h ticker) is fetched ONCE and cached on the source, so
# adding coins costs only per-coin candle calls — not N heavy market-wide
# calls. `prefetch()` (re)loads that snapshot at the start of each run. All
# HTTP goes through a 429-aware retry with exponential backoff.
HTTP_MAX_TRIES = 5
HTTP_BACKOFF_BASE = 1.5     # seconds: 1.5, 3, 6, 12 …
HL_PACING_S = 0.05          # gentle spacing between HL calls to avoid bursts
RATE_LIMIT_ALERT_THRESHOLD = 12   # 429 retries in one run before pinging Telegram

# Per-run rate-limit tally (reset at the start of each run in main()).
#   retries  = 429s that were backed off and succeeded on a later try
#   failures = requests that exhausted all retries → data actually dropped
_RL = {"HL": {"retries": 0, "failures": 0},
       "Binance": {"retries": 0, "failures": 0}}


def _note_rl(venue: str, status: int, attempt: int, wait: float, gave_up: bool) -> None:
    if gave_up:
        _RL[venue]["failures"] += 1
        print(f"  [RATE-LIMIT] {venue} {status} — gave up after {HTTP_MAX_TRIES} tries "
              f"(data dropped)")
    else:
        _RL[venue]["retries"] += 1
        print(f"  [RATE-LIMIT] {venue} {status} — backoff {wait:.1f}s "
              f"(retry {attempt}/{HTTP_MAX_TRIES - 1})")


def _post(payload: dict) -> dict:
    for i in range(HTTP_MAX_TRIES):
        resp = requests.post(HL_INFO_URL, json=payload, timeout=15)
        if resp.status_code == 429 and i < HTTP_MAX_TRIES - 1:
            wait = HTTP_BACKOFF_BASE * (2 ** i)
            _note_rl("HL", 429, i + 1, wait, gave_up=False)
            time.sleep(wait)
            continue
        if resp.status_code == 429:
            _note_rl("HL", 429, i + 1, 0, gave_up=True)
        resp.raise_for_status()
        if HL_PACING_S:
            time.sleep(HL_PACING_S)
        return resp.json()


class HyperliquidSource:
    name = "HL"
    fund_per_year = 24 * 365          # funding settles hourly

    def __init__(self):
        self._names = None            # list of coin names, in ctx order
        self._ctxs = None             # parallel list of asset ctxs
        self._uni = None              # set of names

    def prefetch(self) -> None:
        """Load the whole-market snapshot once (mark/funding/prevDay for all)."""
        meta, ctxs = _post({"type": "metaAndAssetCtxs"})
        self._names = [a["name"] for a in meta["universe"]]
        self._ctxs = ctxs
        self._uni = set(self._names)

    def universe(self) -> set:
        if self._uni is None:
            self.prefetch()
        return self._uni

    def ctx(self, coin: str) -> dict:
        if self._names is None:
            self.prefetch()
        if coin not in self._uni:
            raise ValueError(f"{coin} not on Hyperliquid")
        c = self._ctxs[self._names.index(coin)]
        return {
            "mark": float(c["markPx"]),
            "prev_day": float(c["prevDayPx"]),
            "funding_rate": float(c["funding"]),
            "oi": float(c.get("openInterest", 0) or 0),
        }

    def candles(self, coin: str, lookback_days: int = 12) -> list:
        now = int(time.time() * 1000)
        start = now - lookback_days * 86_400_000
        raw = _post({"type": "candleSnapshot",
                     "req": {"coin": coin, "interval": "1h",
                             "startTime": start, "endTime": now}})
        return [{"o": float(x["o"]), "c": float(x["c"]), "h": float(x["h"]),
                 "l": float(x["l"]), "v": float(x["v"])} for x in raw]

    def funding_rates(self, coin: str, lookback_hours: int = 96) -> list:
        """Per-period funding rates, oldest first (not annualized)."""
        start = int((time.time() - lookback_hours * 3600) * 1000)
        fh = _post({"type": "fundingHistory", "coin": coin, "startTime": start})
        return [float(x["fundingRate"]) for x in fh]


class BinanceSource:
    name = "Binance"
    fund_per_year = 3 * 365           # funding settles every 8h
    FAPI = "https://fapi.binance.com"

    def __init__(self):
        self._prem = None             # symbol -> premiumIndex row (mark, funding)
        self._tick = None             # symbol -> 24h ticker row (openPrice)
        self._uni = None

    def _get(self, path: str, **params) -> dict:
        for i in range(HTTP_MAX_TRIES):
            r = requests.get(self.FAPI + path, params=params, timeout=15)
            if r.status_code in (418, 429) and i < HTTP_MAX_TRIES - 1:
                wait = HTTP_BACKOFF_BASE * (2 ** i)
                _note_rl("Binance", r.status_code, i + 1, wait, gave_up=False)
                time.sleep(wait)
                continue
            if r.status_code in (418, 429):
                _note_rl("Binance", r.status_code, i + 1, 0, gave_up=True)
            r.raise_for_status()
            return r.json()

    def prefetch(self) -> None:
        """Load mark/funding (premiumIndex) and 24h opens (ticker) for all
        symbols in two market-wide calls, instead of 2 calls per coin."""
        prem = self._get("/fapi/v1/premiumIndex")          # array, all symbols
        self._prem = {d["symbol"]: d for d in prem}
        tick = self._get("/fapi/v1/ticker/24hr")           # array, all symbols
        self._tick = {d["symbol"]: d for d in tick}

    def universe(self) -> set:
        if self._uni is None:
            info = self._get("/fapi/v1/exchangeInfo")
            self._uni = {s["baseAsset"] for s in info["symbols"]
                         if s.get("quoteAsset") == "USDT"
                         and s.get("contractType") == "PERPETUAL"
                         and s.get("status") == "TRADING"}
        return self._uni

    def ctx(self, coin: str) -> dict:
        if self._prem is None:
            self.prefetch()
        sym = coin + "USDT"
        prem = self._prem.get(sym)
        tick = self._tick.get(sym)
        if prem is None or tick is None:
            raise ValueError(f"{coin} not on Binance perps")
        return {
            "mark": float(prem["markPrice"]),
            "prev_day": float(tick["openPrice"]),   # price 24h ago
            "funding_rate": float(prem["lastFundingRate"]),
            "oi": 0.0,                               # unused by signals
        }

    def candles(self, coin: str, lookback_days: int = 12) -> list:
        raw = self._get("/fapi/v1/klines", symbol=coin + "USDT",
                        interval="1h", limit=min(1500, lookback_days * 24 + 24))
        # kline: [openTime, o, h, l, c, v, closeTime, ...]
        return [{"o": float(k[1]), "c": float(k[4]), "h": float(k[2]),
                 "l": float(k[3]), "v": float(k[5])} for k in raw]

    def funding_rates(self, coin: str, limit: int = 12) -> list:
        """Per-period (8h) funding rates, oldest first (not annualized)."""
        fh = self._get("/fapi/v1/fundingRate", symbol=coin + "USDT", limit=limit)
        return [float(x["fundingRate"]) for x in fh]


HL = HyperliquidSource()
BINANCE = BinanceSource()


# ── Indicators ──────────────────────────────────────────────────────────
def rsi(prices: list, period: int = 14) -> list:
    """Wilder's RSI. Returns one value per bar from index `period` onward."""
    if len(prices) <= period:
        return []
    gains, losses = [], []
    for i in range(1, len(prices)):
        ch = prices[i] - prices[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    out = []
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        rs = (avg_g / avg_l) if avg_l else 999.0
        out.append(100.0 - 100.0 / (1.0 + rs))
    return out


def local_maxima(vals: list, span: int = 2) -> list:
    """Indices that are >= all neighbours within `span` (simple swing highs)."""
    idx = []
    for i in range(span, len(vals) - span):
        window = vals[i - span:i + span + 1]
        if vals[i] == max(window):
            idx.append(i)
    return idx


def local_minima(vals: list, span: int = 2) -> list:
    """Indices that are <= all neighbours within `span` (simple swing lows)."""
    idx = []
    for i in range(span, len(vals) - span):
        window = vals[i - span:i + span + 1]
        if vals[i] == min(window):
            idx.append(i)
    return idx


def compute_sr(highs: list, lows: list, mark: float,
               tol: float = 0.006, span: int = 3, top_n: int = 4) -> dict:
    """Support/resistance zones from clustered swing highs/lows.

    Swing points within `tol` of each other are merged into one zone; the zone's
    `touches` count is its strength. Returns the nearest `top_n` zones above
    (resistance) and below (support) the current mark, nearest first."""
    swings = sorted([highs[i] for i in local_maxima(highs, span)] +
                    [lows[i] for i in local_minima(lows, span)])
    clusters = []
    for p in swings:
        if clusters:
            avg = clusters[-1]["sum"] / clusters[-1]["n"]
            if abs(p - avg) / avg <= tol:
                clusters[-1]["sum"] += p
                clusters[-1]["n"] += 1
                continue
        clusters.append({"sum": p, "n": 1})
    zones = [{"price": round(c["sum"] / c["n"], 6), "touches": c["n"]} for c in clusters]
    res = sorted((z for z in zones if z["price"] > mark * 1.0005),
                 key=lambda z: z["price"])[:top_n]
    sup = sorted((z for z in zones if z["price"] < mark * 0.9995),
                 key=lambda z: z["price"], reverse=True)[:top_n]
    return {"support": sup, "resistance": res}


# ── TRAMA (Trend Regularity Adaptive Moving Average, after LuxAlgo) ────────
# An adaptive MA whose speed = the frequency of new N-bar highs/lows, squared.
# It hugs price in trends and flattens in ranges — so its SLOPE is a clean
# "trend vs range" read: steep = trend (don't fight it), flat = range (fade OK).
TRAMA_LEN = int(os.getenv("EXHAUSTION_TRAMA_LEN", "99"))
TRAMA_LEN_D = int(os.getenv("EXHAUSTION_TRAMA_LEN_D", "50"))     # daily-timeframe length (~2 months)
TRAMA_SLOPE_K = 10           # bars over which the slope is measured
TRAMA_FLAT_PCT = 0.5         # |slope%| below this = "flat" (range)
TRAMA_STEEP = float(os.getenv("EXHAUSTION_TRAMA_STEEP", "1.5"))  # |slope%| above = strong trend


def trama(closes: list, highs: list, lows: list, length: int = TRAMA_LEN) -> tuple | None:
    """Return (value, slope_pct) or None. slope_pct = % change over TRAMA_SLOPE_K bars."""
    n = len(closes)
    if n < length + TRAMA_SLOPE_K + 2:
        return None
    hh = [max(highs[max(0, i - length + 1):i + 1]) for i in range(n)]
    ll = [min(lows[max(0, i - length + 1):i + 1]) for i in range(n)]
    flags = [0] * n
    for i in range(1, n):
        flags[i] = 1 if (hh[i] > hh[i - 1] or ll[i] < ll[i - 1]) else 0
    ama = closes[0]
    series = [ama]
    for i in range(1, n):
        lo = max(0, i - length + 1)
        tc = (sum(flags[lo:i + 1]) / (i - lo + 1)) ** 2     # trend consistency, squared
        ama = ama + tc * (closes[i] - ama)
        series.append(ama)
    prev = series[-1 - TRAMA_SLOPE_K]
    slope = (series[-1] / prev - 1) * 100 if prev else 0.0
    return round(series[-1], 8), round(slope, 2)


def trama_trend(mark: float, tr: tuple | None) -> tuple:
    """(trama_value, slope_pct, trend) — trend ∈ up|down|flat|? from price vs TRAMA + slope."""
    if not tr:
        return None, None, "?"
    val, slope = tr
    if slope > TRAMA_FLAT_PCT and mark > val:
        trend = "up"
    elif slope < -TRAMA_FLAT_PCT and mark < val:
        trend = "down"
    else:
        trend = "flat"
    return val, slope, trend


# ── Accumulation / Distribution (Chaikin A/D line) ────────────────────────
# Money flows into (accumulation) or out of (distribution) a coin: each bar adds
# ((close-low)-(high-close))/(high-low) × volume to a running line. A rising line
# = buyers absorbing; falling = sellers distributing. Its slope vs price is the
# tell — price up while A/D falls is distribution (weak), the reverse is stealth
# accumulation (strong).
ACCDIST_K = int(os.getenv("EXHAUSTION_ACCDIST_K", "24"))     # bars for the slope read
ACCDIST_BAND = float(os.getenv("EXHAUSTION_ACCDIST_BAND", "0.15"))  # |net MFV / volume| for a trend


def accdist_read(closes: list, highs: list, lows: list, vols: list, k: int = ACCDIST_K) -> dict:
    """{state, ad_slope, price_chg, divergence} over the last k bars. state ∈
    accumulation | distribution | neutral | ?. ad_slope = net money-flow volume as
    a fraction of the window's volume (comparable across coins)."""
    n = len(closes)
    if n < k + 2:
        return {"state": "?", "ad_slope": None, "price_chg": None, "divergence": None}
    net = 0.0
    for i in range(n - k, n):
        rng = highs[i] - lows[i]
        mfm = (((closes[i] - lows[i]) - (highs[i] - closes[i])) / rng) if rng else 0.0
        net += mfm * vols[i]
    vol_ref = sum(vols[n - k:]) or 1.0
    ad_slope = net / vol_ref
    price_chg = (closes[-1] / closes[-1 - k] - 1) * 100
    if ad_slope > ACCDIST_BAND:
        state = "accumulation"
    elif ad_slope < -ACCDIST_BAND:
        state = "distribution"
    else:
        state = "neutral"
    diverg = None
    if price_chg > 1 and ad_slope < -ACCDIST_BAND:
        diverg = "bearish (price up, A/D down — distribution)"
    elif price_chg < -1 and ad_slope > ACCDIST_BAND:
        diverg = "bullish (price down, A/D up — accumulation)"
    return {"state": state, "ad_slope": round(ad_slope, 3),
            "price_chg": round(price_chg, 1), "divergence": diverg}


def sr_breakout(mark: float, highs: list, lows: list, sr: dict, lookback: int = 24) -> str:
    """Is price breaking a level? New-extreme break first, else the bracketing S/R.
    `sr` is a compute_sr() result (resistance above / support below the mark)."""
    hh = max(highs[-lookback - 1:-1]) if len(highs) > lookback else max(highs[:-1] or [mark])
    ll = min(lows[-lookback - 1:-1]) if len(lows) > lookback else min(lows[:-1] or [mark])
    if mark >= hh:
        return f"breaking to new {lookback}-bar highs (${hh:.4f}) — bullish breakout"
    if mark <= ll:
        return f"breaking to new {lookback}-bar lows (${ll:.4f}) — bearish breakdown"
    res = sr["resistance"][0] if sr.get("resistance") else None
    sup = sr["support"][0] if sr.get("support") else None
    parts = []
    if res:
        parts.append(f"resistance ${res['price']:.4f} ({res['touches']}t, {(res['price'] / mark - 1) * 100:.1f}% up)")
    if sup:
        parts.append(f"support ${sup['price']:.4f} ({sup['touches']}t, {(1 - sup['price'] / mark) * 100:.1f}% down)")
    return "range-bound — " + " · ".join(parts) if parts else "range-bound (no clear level)"


# ── Signal evaluation ───────────────────────────────────────────────────
def _funding_accel(source, coin: str):
    """Return (accel_ann, recent_ann) for funding: recent 3 periods vs prior 6, or None."""
    try:
        rates = source.funding_rates(coin)
        if len(rates) < 9:
            return None
        recent = sum(rates[-3:]) / 3
        prior = sum(rates[-9:-3]) / 6
        ann = source.fund_per_year * 100.0
        return (recent - prior) * ann, recent * ann
    except Exception as e:
        print(f"  [WARN] {coin}@{source.name}: funding history failed: {e}")
        return None


# ── Doji reversal (indecision candle after a directional run) ─────────────
DOJI_BODY_MAX = 0.10          # body ≤ this fraction of the candle's range = a doji
DOJI_MIN_RANGE_PCT = 0.3      # candle range must be ≥ this % of price (not a dead bar)
DOJI_RUN_BARS = 6             # bars of directional move leading into the doji
DOJI_RUN_PCT = 2.0            # the run must be ≥ this % (up for a top, down for a bottom)


def _doji_signal(opens: list, closes: list, highs: list, lows: list, direction: str):
    """A doji (open ≈ close, real range) on the last COMPLETED bar, after a
    directional run into it — classic indecision/reversal tell. Returns a
    (key, description) signal tuple or None. `direction` is 'top' or 'bottom'."""
    if len(closes) < DOJI_RUN_BARS + 3:
        return None
    o, h, l, c = opens[-2], highs[-2], lows[-2], closes[-2]   # -1 is the forming bar
    rng = h - l
    if rng <= 0 or (rng / c) < DOJI_MIN_RANGE_PCT / 100 or abs(c - o) / rng > DOJI_BODY_MAX:
        return None
    upper, lower = h - max(o, c), min(o, c) - l               # wick lengths
    run_pct = (closes[-3] / closes[-2 - DOJI_RUN_BARS] - 1) * 100   # move into the doji
    if direction == "top" and run_pct >= DOJI_RUN_PCT:
        kind = "gravestone " if upper > 2 * max(lower, 1e-12) else ""
        return ("doji_reversal", f"{kind}doji after +{run_pct:.1f}% run (indecision at the highs)")
    if direction == "bottom" and run_pct <= -DOJI_RUN_PCT:
        kind = "dragonfly " if lower > 2 * max(upper, 1e-12) else ""
        return ("doji_reversal", f"{kind}doji after {run_pct:.1f}% drop (indecision at the lows)")
    return None


def is_doji(o: float, h: float, l: float, c: float) -> str | None:
    """Plain doji test on ONE candle — open≈close with a real range. Returns a label
    (gravestone doji | dragonfly doji | doji) or None. No directional-run or
    reversal-context requirement, unlike `_doji_signal` — used to flag a doji on the
    candle just before a momentum breakout (indecision at the launch)."""
    rng = h - l
    if rng <= 0 or (rng / c) < DOJI_MIN_RANGE_PCT / 100 or abs(c - o) / rng > DOJI_BODY_MAX:
        return None
    upper, lower = h - max(o, c), min(o, c) - l
    if upper > 2 * max(lower, 1e-12):
        return "gravestone doji"
    if lower > 2 * max(upper, 1e-12):
        return "dragonfly doji"
    return "doji"


def _eval_top(source, coin, closes, highs, vols, r, rsi_now, funding_ann, hi_72,
              from_high_pct, baseline_vol):
    signals = []

    # Overbought (stretched and recently peaked)
    rsi_peak_24h = max(r[-24:]) if len(r) >= 24 else max(r)
    if rsi_now >= RSI_OVERBOUGHT and rsi_peak_24h >= RSI_PEAK:
        signals.append(("overbought", f"RSI(14) 1h {rsi_now:.0f} (peaked {rsi_peak_24h:.0f})"))

    # Bearish RSI divergence — price higher high, RSI lower high
    offset = len(closes) - len(r)
    rec = closes[offset:]
    peaks = [p for p in local_maxima(rec) if p >= len(rec) - 36]
    if len(peaks) >= 2:
        p1, p2 = peaks[-2], peaks[-1]
        if rec[p2] > rec[p1] and r[p2] < r[p1] and r[p1] >= RSI_OVERBOUGHT:
            signals.append(("divergence",
                            f"price higher high but RSI lower high ({r[p1]:.0f}→{r[p2]:.0f})"))

    # Funding hot — crowded longs paying up
    if funding_ann >= FUNDING_ANN_HOT:
        signals.append(("funding_hot", f"funding ~{funding_ann:+.0f}%/yr (crowded longs)"))

    # Funding accelerating up — longs piling in (leads the top)
    fa = _funding_accel(source, coin)
    if fa and fa[0] >= FUNDING_ACCEL_ANN and fa[1] > 0:
        signals.append(("funding_accel",
                        f"funding rising fast (+{fa[0]:.0f}pp/yr → {fa[1]:+.0f}%/yr)"))

    # Volume climax — huge bar in last 6h that did NOT set a new high
    if baseline_vol > 0:
        for k in range(len(vols) - 6, len(vols)):
            if vols[k] >= VOL_CLIMAX_MULT * baseline_vol and highs[k] < hi_72:
                signals.append(("volume_climax",
                                f"{vols[k] / baseline_vol:.1f}x-vol bar failed to make new high"))
                break

    # Stalling at highs — near 72h high but last 3 closes flat/down
    if from_high_pct >= -NEAR_EXTREME_PCT and len(closes) >= 4:
        if closes[-1] <= closes[-3] and closes[-2] <= closes[-3]:
            signals.append(("stalling", "hugging the high but closes rolling over"))

    return signals


def _eval_bottom(source, coin, closes, lows, vols, r, rsi_now, funding_ann, lo_72,
                 from_low_pct, baseline_vol):
    signals = []

    # Oversold (washed out and recently troughed)
    rsi_trough_24h = min(r[-24:]) if len(r) >= 24 else min(r)
    if rsi_now <= RSI_OVERSOLD and rsi_trough_24h <= RSI_TROUGH:
        signals.append(("oversold", f"RSI(14) 1h {rsi_now:.0f} (troughed {rsi_trough_24h:.0f})"))

    # Bullish RSI divergence — price lower low, RSI higher low
    offset = len(closes) - len(r)
    rec = closes[offset:]
    troughs = [t for t in local_minima(rec) if t >= len(rec) - 36]
    if len(troughs) >= 2:
        t1, t2 = troughs[-2], troughs[-1]
        if rec[t2] < rec[t1] and r[t2] > r[t1] and r[t1] <= RSI_OVERSOLD:
            signals.append(("divergence",
                            f"price lower low but RSI higher low ({r[t1]:.0f}→{r[t2]:.0f})"))

    # Funding cold — crowded shorts paying up
    if funding_ann <= FUNDING_ANN_COLD:
        signals.append(("funding_cold", f"funding ~{funding_ann:+.0f}%/yr (crowded shorts)"))

    # Funding accelerating down — shorts piling in (leads the low)
    fa = _funding_accel(source, coin)
    if fa and fa[0] <= -FUNDING_ACCEL_ANN and fa[1] < 0:
        signals.append(("funding_accel",
                        f"funding dropping fast ({fa[0]:.0f}pp/yr → {fa[1]:+.0f}%/yr)"))

    # Volume climax — huge bar in last 6h that did NOT set a new low (capitulation)
    if baseline_vol > 0:
        for k in range(len(vols) - 6, len(vols)):
            if vols[k] >= VOL_CLIMAX_MULT * baseline_vol and lows[k] > lo_72:
                signals.append(("volume_climax",
                                f"{vols[k] / baseline_vol:.1f}x-vol bar failed to make new low"))
                break

    # Basing at lows — near 72h low but last 3 closes flat/up
    if from_low_pct <= NEAR_EXTREME_PCT and len(closes) >= 4:
        if closes[-1] >= closes[-3] and closes[-2] >= closes[-3]:
            signals.append(("basing", "hugging the low but closes turning up"))

    return signals


def evaluate(coin: str, source) -> dict | None:
    ctx = source.ctx(coin)
    candles = source.candles(coin)
    if len(candles) < 60:
        print(f"  [WARN] {coin}@{source.name}: not enough candle history ({len(candles)})")
        return None

    opens = [c["o"] for c in candles]
    closes = [c["c"] for c in candles]
    highs = [c["h"] for c in candles]
    lows = [c["l"] for c in candles]
    vols = [c["v"] for c in candles]

    mark = ctx["mark"]
    prev_day = ctx["prev_day"]
    chg_24h = (mark / prev_day - 1.0) * 100.0
    funding_ann = ctx["funding_rate"] * source.fund_per_year * 100.0
    oi = ctx["oi"]

    r = rsi(closes)
    if not r:
        print(f"  [WARN] {coin}: RSI unavailable")
        return None
    rsi_now = r[-1]

    hi_72 = max(highs[-72:]) if len(highs) >= 72 else max(highs)
    lo_72 = min(lows[-72:]) if len(lows) >= 72 else min(lows)
    from_high_pct = (mark / hi_72 - 1.0) * 100.0
    from_low_pct = (mark / lo_72 - 1.0) * 100.0

    # baseline hourly volume (exclude the most recent 6 active bars)
    base = vols[-72:-6] if len(vols) > 72 else vols[:-6]
    baseline_vol = (sum(base) / len(base)) if base else 0.0

    # Direction gates — which side (if any) is in play?
    pump = chg_24h >= PUMP_24H_PCT or from_high_pct >= -NEAR_EXTREME_PCT
    dump = chg_24h <= DUMP_24H_PCT or from_low_pct <= NEAR_EXTREME_PCT

    direction, signals, extreme_pct = None, [], from_high_pct
    # If somehow both fire (very tight range near both extremes), go by 24h sign.
    if pump and (not dump or chg_24h >= 0):
        direction = "top"
        extreme_pct = from_high_pct
        signals = _eval_top(source, coin, closes, highs, vols, r, rsi_now, funding_ann,
                            hi_72, from_high_pct, baseline_vol)
    elif dump:
        direction = "bottom"
        extreme_pct = from_low_pct
        signals = _eval_bottom(source, coin, closes, lows, vols, r, rsi_now, funding_ann,
                               lo_72, from_low_pct, baseline_vol)

    # Doji reversal — an indecision candle after a run into the extreme.
    if direction:
        dj = _doji_signal(opens, closes, highs, lows, direction)
        if dj:
            signals.append(dj)

    trama_val, trama_slope, trend = trama_trend(mark, trama(closes, highs, lows))

    return {
        "coin": coin,
        "source": source.name,
        "mark": mark,
        "chg_24h": chg_24h,
        "from_high_pct": from_high_pct,
        "from_low_pct": from_low_pct,
        "extreme_pct": extreme_pct,
        "rsi_now": rsi_now,
        "funding_ann": funding_ann,
        "oi": oi,
        "signals": signals,
        "direction": direction,
        "sr": compute_sr(highs, lows, mark),   # support/resistance zones
        "trama": trama_val,                    # adaptive trend MA
        "trama_slope": trama_slope,            # % slope over TRAMA_SLOPE_K bars
        "trend": trend,                        # up | down | flat | ?
        "recent_hi": max(highs[-2:]),          # last ~2 bars — for setup touch detection
        "recent_lo": min(lows[-2:]),
        "accdist": accdist_read(closes, highs, lows, vols),   # accumulation/distribution
        "breakout": sr_breakout(mark, highs, lows, compute_sr(highs, lows, mark)),
        # doji on the last COMPLETED candle (index -2; -1 is forming), regardless of
        # trend — surfaced on the momentum breakout as an indecision warning.
        "prev_doji": is_doji(opens[-2], highs[-2], lows[-2], closes[-2]) if len(closes) >= 2 else None,
    }


# ── State / dedup ───────────────────────────────────────────────────────
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


def should_alert(coin: str, direction: str, n_signals: int, state: dict) -> bool:
    """Alert if direction flipped, past cooldown, or signal count escalated."""
    rec = state.get(coin)
    if not rec:
        return True
    if rec.get("direction") != direction:
        return True  # flipped top<->bottom — a new setup, fire immediately
    age_h = (time.time() - rec.get("ts", 0)) / 3600.0
    if age_h >= COOLDOWN_HOURS:
        return True
    if n_signals > rec.get("n_signals", 0):
        return True  # escalation — fire even within cooldown
    return False


# ── Formatting ──────────────────────────────────────────────────────────
def _funding_tag(funding_ann: float) -> str:
    """🔥/🧊 marker when funding sits in an extreme (crowded) zone, else ''."""
    if funding_ann >= FUNDING_ANN_HOT:
        return " 🔥"
    if funding_ann <= FUNDING_ANN_COLD:
        return " 🧊"
    return ""


def _funding_footer() -> list:
    """Static 'how to read funding' note, derived from the live thresholds so it
    stays accurate if FUNDING_ANN_HOT/COLD/ACCEL are retuned."""
    return [
        "📖 *Reading funding* (annualized):",
        f"• Neutral ≈ +10%/yr.  🔥 ≥ +{FUNDING_ANN_HOT:.0f}% = crowded longs (top risk)  ·  "
        f"🧊 ≤ {FUNDING_ANN_COLD:.0f}% = crowded shorts (bottom risk)",
        f"• The _shift_ leads the turn — a ≥ {FUNDING_ACCEL_ANN:.0f}pp/yr jump means "
        f"the crowd is piling in fast.",
    ]


def _venue_block(ev: dict) -> list:
    """Per-venue detail lines for one exchange's evaluation."""
    if ev["direction"] == "top":
        extreme = f"{ev['from_high_pct']:+.1f}% from 72h high"
    else:
        extreme = f"{ev['from_low_pct']:+.1f}% from 72h low"
    lines = [
        f"*{ev['source']}* — ${ev['mark']:.4f}  |  24h {ev['chg_24h']:+.1f}%  |  {extreme}",
        f"  RSI {ev['rsi_now']:.0f}  |  funding {ev['funding_ann']:+.0f}%/yr"
        f"{_funding_tag(ev['funding_ann'])}  |  {len(ev['signals'])} signals",
    ]
    for _key, desc in ev["signals"]:
        lines.append(f"    • {desc}")
    return lines


def _doji_desc(evs) -> str | None:
    """First doji_reversal description across the given evaluation(s), or None."""
    for ev in evs:
        for k, desc in (ev or {}).get("signals", []):
            if k == "doji_reversal":
                return desc
    return None


def _btc_ratio_tag(coin: str) -> str | None:
    """Compact coin/BTC relative-strength tag for the exhaustion alert + digest,
    e.g. 'vs BTC: below TRAMA 🔴 (1D down)'. Uses the daily read (the positioning
    timeframe). None for BTC or when the ratio has no data. Lazy-imports
    ratio_watch to avoid a circular import; never raises."""
    if coin.upper() == "BTC":
        return None
    try:
        import ratio_watch as rw
        ed = rw.evaluate_ratio(f"{coin}/BTC", "1d")
    except Exception:
        return None
    if not ed:
        return None
    side = "above" if ed["above_trama"] else "below"
    clr = "🟢" if ed["above_trama"] else "🔴"
    return f"vs BTC: {side} TRAMA {clr} (1D {ed['trend']})"


def format_alert(coin: str, direction: str, mode: str, venues: dict,
                 plan: dict | None = None, conviction: str | None = None,
                 bias_state: str | None = None, ratio_tag: str | None = None,
                 macro_tag: str | None = None) -> str:
    """`venues` maps venue name -> evaluation dict. `mode` is 'confluence' or
    'single'. `plan`/`conviction` (from assess_exhaustion_trade) add the trade
    setup so the alert says the same thing the review will."""
    now_utc = datetime.now(timezone.utc).strftime("%H:%M UTC")
    if direction == "top":
        title = "⚠️ TOP EXHAUSTION"
        side = "SHORT"
        odds = "_Odds of a pullback rising — not a guaranteed top._"
    else:
        title = "🩸 BOTTOM EXHAUSTION"
        side = "LONG"
        odds = "_Odds of a bounce rising — not a guaranteed bottom._"

    if mode == "confluence":
        tag = f"confluence: {' + '.join(venues)}"
    else:
        only = next(iter(venues))
        tag = f"{only} only — no confluence partner"

    conv = f"  —  *{conviction.upper()} conviction*" if conviction else ""
    doji_flag = "  🕯️ DOJI" if _doji_desc(venues.values()) else ""
    lines = [f"*{title} — {coin}*  ({tag})  ({now_utc}){conv}{doji_flag}", ""]
    for name in venues:                       # HL first, then Binance (insertion order)
        lines += _venue_block(venues[name])
        lines.append("")
    if plan:                                  # the trade (matches what the review evaluates)
        trend = plan.get("trend", "?")
        tslope = plan.get("trama_slope", 0.0)
        tval = plan.get("trama")
        tdist = plan.get("trama_dist")
        trama_txt = (f"TRAMA ${tval:.4f}  ·  price {tdist:+.1f}% from it (slope {tslope:+.1f}%)"
                     if tval is not None and tdist is not None
                     else f"TRAMA n/a (slope {tslope:+.1f}%)")
        lines += [
            f"*Setup: {side}* at a strong level  ·  bias {bias_state or 'n/a'}",
            f"  trend {trend}  ·  {trama_txt}",
            f"  entry ${plan['entry']:.4f}  ·  stop ${plan['stop']:.4f}  ·  "
            f"target ${plan['target']:.4f}  ·  *R:R {plan['rr']}*",
            "",
        ]
    if ratio_tag:                             # relative strength vs BTC (positioning)
        lines += [ratio_tag, ""]
    if macro_tag:                             # macro regime (don't-fight-the-macro context)
        lines += [macro_tag, ""]
    lines.append(odds)
    lines.append("")
    lines += _funding_footer()
    return "\n".join(lines)


# ── Firebase publish (optional — feeds cryptex/multi-bot's exhaustion gate) ─
# The watcher's alerting is unchanged; this is a purely additive side-channel.
# Fail-safe: if firebase-admin isn't installed or the SA / DB URL env vars are
# absent, every function here becomes a silent no-op and the watcher runs as
# before. The credential is loaded by file path only and never printed.
_FB_READY = None                       # tri-state: None=untried, True/False


def _firebase_ready() -> bool:
    global _FB_READY
    if _FB_READY is not None:
        return _FB_READY
    sa = os.getenv("EXHAUSTION_FIREBASE_SA")
    url = os.getenv("EXHAUSTION_FIREBASE_DB_URL")
    if not (firebase_admin and sa and url and Path(sa).exists()):
        _FB_READY = False
        return False
    try:
        firebase_admin.initialize_app(_fb_credentials.Certificate(sa),
                                      {"databaseURL": url})
        _FB_READY = True
    except Exception as e:                      # never leak key material
        print(f"  [WARN] Firebase init failed — publishing disabled: {type(e).__name__}")
        _FB_READY = False
    return _FB_READY


def _funding_zone(funding_ann: float) -> str:
    if funding_ann >= FUNDING_ANN_HOT:
        return "hot"
    if funding_ann <= FUNDING_ANN_COLD:
        return "cold"
    return "neutral"


def gate_state(evs: dict, on_hl: bool, on_bn: bool) -> dict:
    """The bot-gate view of a coin: is there an *actionable* exhaustion right
    now, which way, and was it confluence-confirmed? Independent of alert
    cooldown — the gate cares about current market state, not whether we pinged.
    `confluence` is True only when both venues agreed and each cleared
    MIN_SIGNALS; single-venue coins report confluence=False."""
    direction, confluence, signals, funding = "none", False, 0, 0.0
    if on_hl and on_bn and "HL" in evs and "Binance" in evs:
        a, b = evs["HL"], evs["Binance"]
        if (a["direction"] and a["direction"] == b["direction"]
                and len(a["signals"]) >= MIN_SIGNALS
                and len(b["signals"]) >= MIN_SIGNALS):
            direction = a["direction"]
            confluence = True
            signals = len(a["signals"]) + len(b["signals"])
            funding = max((a["funding_ann"], b["funding_ann"]), key=abs)
    elif not (on_hl and on_bn) and evs:
        ev = next(iter(evs.values()))
        if ev["direction"] and len(ev["signals"]) >= MIN_SIGNALS:
            direction, signals, funding = ev["direction"], len(ev["signals"]), ev["funding_ann"]
    return {
        "direction": direction,            # "top" | "bottom" | "none"
        "confluence": confluence,          # both venues agreed
        "signals": signals,                # total signal count
        "funding_zone": _funding_zone(funding),
        "ts": int(time.time()),            # unix seconds — bot applies a staleness guard
    }


def publish_to_firebase(states: dict) -> None:
    """One batched RTDB write for all coins under `exhaustion/`. No-op if unconfigured."""
    if not states or not _firebase_ready():
        return
    try:
        _fb_db.reference("exhaustion").update(states)
        print(f"  📡 Published {len(states)} coin states to Firebase (exhaustion/).")
    except Exception as e:
        print(f"  [WARN] Firebase publish failed: {type(e).__name__}: {str(e)[:120]}")


def publish_decisions(decisions: dict) -> None:
    """One batched RTDB write for all coins under `decision/`. No-op if unconfigured."""
    if not decisions or not _firebase_ready():
        return
    try:
        _fb_db.reference("decision").update(decisions)
        print(f"  📡 Published {len(decisions)} decisions to Firebase (decision/).")
    except Exception as e:
        print(f"  [WARN] Firebase decision publish failed: {type(e).__name__}: {str(e)[:120]}")


# ── Decision layer (S/R + exhaustion + bot bias → a lean) ─────────────────
NEAR_LEVEL_PCT = 0.7        # within this % of a level counts as "at" it
STRONG_TOUCHES = 3          # touches for a level to count as "strong"
RSI_LONG_MAX = 45           # a medium long needs RSI pulled back (not overbought)
RSI_SHORT_MIN = 55          # a medium short needs RSI elevated (not oversold)
DIGEST_MAX_PER_SIDE = 8     # cap each side of the hourly digest
# Alert-quality gate: exhaustion only alerts when it's a *tradeable* setup —
# at a strong level, not fighting a strong opposing bias, acceptable R:R.
MIN_RR = float(os.getenv("EXHAUSTION_MIN_RR", "1.5"))
REQUIRE_CONFIRMATION = os.getenv("EXHAUSTION_REQUIRE_CONFIRMATION", "false").lower() == "true"


def assess_exhaustion_trade(direction: str, ev: dict, bias: dict | None) -> tuple:
    """Is this exhaustion a TRADEABLE setup, not just a raw signal? This is the
    gate that keeps the ALERT and the REVIEW consistent — it applies the same
    checks the review would: is it AT a strong S/R level (on the correct side),
    is it fighting a strong opposing trend, and is the R:R acceptable?

    Returns (ok, conviction, plan). Works for confluence and single-venue coins.
    Note: at a fresh 72h extreme there is no mapped level on the fade side, so
    `at_level` is False → suppressed (this is what stops the parabola-topping
    false alerts). Confirmation (a pullback off the extreme) is optional."""
    sr, mark = ev["sr"], ev["mark"]
    bias_state = (bias or {}).get("bias")

    if direction == "top":                       # fade = short into resistance
        strong = next((z for z in sr["resistance"] if z["touches"] >= STRONG_TOUCHES), None)
        at = strong is not None and (strong["price"] / mark - 1) * 100 <= NEAR_LEVEL_PCT
        opposing = bias_state == "bullish"
        confirmed = (not REQUIRE_CONFIRMATION) or ev["from_high_pct"] <= -0.5
        entry = strong["price"] if strong else mark
        stop = sr["resistance"][1]["price"] if len(sr["resistance"]) > 1 else entry * 1.03
        target = sr["support"][0]["price"] if sr["support"] else mark * 0.95
        rr = (entry - target) / (stop - entry) if stop > entry else 0.0
    else:                                        # bottom → long off support
        strong = next((z for z in sr["support"] if z["touches"] >= STRONG_TOUCHES), None)
        at = strong is not None and (1 - strong["price"] / mark) * 100 <= NEAR_LEVEL_PCT
        opposing = bias_state == "bearish"
        confirmed = (not REQUIRE_CONFIRMATION) or ev["from_low_pct"] >= 0.5
        entry = strong["price"] if strong else mark
        stop = sr["support"][1]["price"] if len(sr["support"]) > 1 else entry * 0.97
        target = sr["resistance"][0]["price"] if sr["resistance"] else mark * 1.05
        rr = (target - entry) / (entry - stop) if entry > stop else 0.0

    # TRAMA trend filter: don't fade a strongly-sloping trend (a top into a steep
    # uptrend, or a bottom into a steep downtrend) — that's the parabola trap.
    slope = ev.get("trama_slope") or 0.0
    steep_against = ((direction == "top" and slope > TRAMA_STEEP)
                     or (direction == "bottom" and slope < -TRAMA_STEEP))

    trama_val = ev.get("trama")
    trama_dist = ((mark / trama_val - 1) * 100) if trama_val else None

    rr = round(rr, 1)
    ok = bool(at and not opposing and confirmed and rr >= MIN_RR and not steep_against)
    conviction = "high" if (ok and rr >= 2.0) else ("medium" if ok else "low")
    plan = {"entry": round(entry, 6), "stop": round(stop, 6), "target": round(target, 6),
            "rr": rr, "at_level": bool(at), "opposing": bool(opposing),
            "confirmed": bool(confirmed), "trend": ev.get("trend", "?"),
            "trama_slope": round(slope, 2), "steep_against": bool(steep_against),
            "trama": trama_val, "trama_dist": trama_dist}
    return ok, conviction, plan


def read_pattern_signals(coin: str, user_id: str, max_age_h: float = 48.0) -> list:
    """Recent bot pattern/breakout signals for a coin from the shared Firebase,
    across timeframes — the same `users/{uid}/signals/{SYM}` node the multi-frontend
    reads. Newest first, capped. [] if none / Firebase down / no user_id. Never throws."""
    if not (user_id and coin and _firebase_ready()):
        return []
    sym = coin.upper() + "USDT"
    try:
        node = _fb_db.reference(f"users/{user_id}/signals/{sym}").get() or {}
    except Exception:
        return []
    now_ms = time.time() * 1000
    out = []
    for tf, sigs in (node or {}).items():
        if not isinstance(sigs, dict):
            continue
        for _ts, s in sigs.items():
            if not isinstance(s, dict):
                continue
            det = s.get("detectedAtMs") or 0
            age_h = (now_ms - det) / 3_600_000 if det else None
            if age_h is not None and age_h > max_age_h:
                continue
            out.append({
                "tf": tf, "strategy": s.get("strategy"), "direction": s.get("direction"),
                "strong": bool(s.get("isStrongSignal")), "win_rate": s.get("historicalWinRate"),
                "entry": s.get("entryPriceUsdt"), "stop": s.get("stopPriceUsdt"),
                "targets": s.get("targetPricesUsdt"), "placed": bool(s.get("orderWasPlaced")),
                "age_h": round(age_h, 1) if age_h is not None else None,
            })
    out.sort(key=lambda x: (x["age_h"] is None, x["age_h"] or 0))
    return out[:8]


def read_chart_patterns(coin: str, tfs=("1h", "4h", "1d", "1w")) -> dict:
    """Chart patterns (triangles/wedges/H&S…) + accumulation/distribution zones the
    frontend's detector publishes to `patterns/{coin}/{tf}` in the shared Firebase —
    the same node the multi-frontend chart modal reads. Returns
    {patterns: [...], zones: [...]}, breakout/confirmed patterns first, newest first.
    coin.id is the lowercased ticker (e.g. avax). Never throws."""
    empty = {"patterns": [], "zones": []}
    if not (coin and _firebase_ready()):
        return empty
    cid = coin.lower()
    pats_out, zones_out = [], []
    for tf in tfs:
        try:
            node = _fb_db.reference(f"patterns/{cid}/{tf}").get()
        except Exception:
            continue
        if not isinstance(node, dict):
            continue
        pats = node.get("patterns")
        pats = list(pats.values()) if isinstance(pats, dict) else (pats or [])
        for p in pats:
            if not isinstance(p, dict):
                continue
            mm = p.get("measured_move") or {}
            pats_out.append({
                "tf": tf, "type": p.get("type"), "direction": p.get("direction"),
                "status": p.get("status"), "confidence": p.get("confidence"),
                "target": mm.get("target"), "invalidation": mm.get("invalidation"),
                "magnitude_pct": mm.get("magnitude_pct"),
                "end_time": p.get("end_time"), "summary": p.get("summary"),
            })
        zs = node.get("accumulation_zones")
        zs = list(zs.values()) if isinstance(zs, dict) else (zs or [])
        for z in zs:
            if not isinstance(z, dict):
                continue
            zones_out.append({"tf": tf, "type": z.get("type"), "label": z.get("label"),
                              "low": z.get("price_low"), "high": z.get("price_high"),
                              "strength": z.get("strength")})
    rank = {"breakout": 0, "confirmed": 0}
    pats_out.sort(key=lambda p: (rank.get(p.get("status"), 1), -(p.get("end_time") or 0)))
    return {"patterns": pats_out[:6], "zones": zones_out[:6]}


def read_intelligence() -> tuple:
    """Batch-read the bot's trend_bias map + market_regime once per run.
    Returns ({} , None) if Firebase is unavailable. Never throws."""
    if not _firebase_ready():
        return {}, None
    try:
        bias = _fb_db.reference("intelligence/trend_bias").get() or {}
        regime = _fb_db.reference("intelligence/market_regime").get()
        return bias, regime
    except Exception:
        return {}, None


def read_macro() -> dict | None:
    """The latest macro-conditions read (10Y + DXY → risk regime), from the cache
    macro_conditions.py writes. None if missing or stale (>36h). Never throws."""
    f = ROOT / "reports" / "macro_conditions.json"
    try:
        m = json.loads(f.read_text(encoding="utf-8"))
        return None if (time.time() - m.get("ts", 0) > 36 * 3600) else m
    except (OSError, json.JSONDecodeError):
        return None


def read_usdt_dominance() -> dict | None:
    """The latest USDT.D read (state + crypto rally implication), recomputed from the
    series usdt_dominance.py maintains. None if too little history. Never throws."""
    f = ROOT / "reports" / "usdt_dominance.json"
    try:
        pts = (json.loads(f.read_text(encoding="utf-8")).get("points") or [])
        if len(pts) < 12:
            return None
        import usdt_dominance as ud
        return ud.usdtd_signal([p["usdt_d"] for p in pts])
    except Exception:
        return None


def _macro_tag(m: dict | None) -> str | None:
    """Compact one-line macro-regime tag for alerts/digest, or None."""
    if not m:
        return None
    emoji = {"risk_off": "🔴", "risk_on": "🟢", "neutral": "⚪"}.get(m["regime"], "")
    return (f"macro: {emoji} {m['regime'].replace('_', ' ')} ({m['strength']}) — "
            f"10Y {m['yield_10y']:.2f}% {m['yield_dir']}, DXY {m['dxy']:.1f} {m['dxy_dir']}")


def _macro_align(lean: str, m: dict | None) -> str | None:
    """Is a long/short lean with or against the macro wind? aligned | counter | None.
    (long favours risk_on, short favours risk_off; neutral macro → None.)"""
    if not m or m.get("regime") not in ("risk_on", "risk_off"):
        return None
    favours_long = m["regime"] == "risk_on"
    if lean == "long":
        return "aligned" if favours_long else "counter"
    if lean == "short":
        return "counter" if favours_long else "aligned"
    return None


def read_cross_signals() -> dict:
    """Read the macro + discovery engines' latest per-coin verdicts from Firebase
    so the digest can flag coins a 2nd engine also likes. Returns
    {COIN: {"macro": zone, "discovery": klass}}. Never throws; {} if unavailable."""
    if not _firebase_ready():
        return {}
    out: dict = {}
    try:
        for coin, node in (_fb_db.reference("macro").get() or {}).items():
            if isinstance(node, dict) and node.get("zone"):
                out.setdefault(coin.upper(), {})["macro"] = node["zone"]
    except Exception:
        pass
    try:
        for coin, node in (_fb_db.reference("scan").get() or {}).items():
            if isinstance(node, dict) and node.get("klass"):
                out.setdefault(coin.upper(), {})["discovery"] = node["klass"]
    except Exception:
        pass
    return out


def _rr(entry: float, stop: float, target: float) -> float:
    risk = abs(entry - stop)
    return round(abs(target - entry) / risk, 2) if risk else 0.0


def build_decision(evs: dict, on_hl: bool, on_bn: bool, bias: dict | None) -> dict:
    """Combine exhaustion + S/R + the bot's trend bias into a per-coin lean.

    Conviction:
      high   — confluence exhaustion AT a strong (>=3 touch) level, bias not
               opposing (reversal setups worth acting on).
      medium — price at a strong level with regime agreement, no exhaustion.
      low    — mid-range or no aligned edge (stand aside)."""
    gs = gate_state(evs, on_hl, on_bn)
    ev = evs.get("HL") or evs.get("Binance")
    mark, sr = ev["mark"], ev["sr"]
    strong_sup = next((z for z in sr["support"] if z["touches"] >= STRONG_TOUCHES), None)
    strong_res = next((z for z in sr["resistance"] if z["touches"] >= STRONG_TOUCHES), None)
    near_sup = sr["support"][0] if sr["support"] else None
    near_res = sr["resistance"][0] if sr["resistance"] else None

    slope = ev.get("trama_slope") or 0.0
    trend = ev.get("trend", "?")
    trama_val = ev.get("trama")
    trama_dist = ((mark / trama_val - 1) * 100) if trama_val else None

    bias_state = (bias or {}).get("bias")            # bullish | bearish | neutral | None
    long_ok = (bias or {}).get("long_allowed", True)
    short_ok = (bias or {}).get("short_allowed", True)

    at_res = strong_res is not None and (strong_res["price"] / mark - 1) * 100 <= NEAR_LEVEL_PCT
    at_sup = strong_sup is not None and (1 - strong_sup["price"] / mark) * 100 <= NEAR_LEVEL_PCT

    lean, conviction, entry, stop, target, why = "neutral", "low", None, None, None, "mid-range / no aligned edge"

    if gs["direction"] == "top" and gs["confluence"] and at_res and short_ok and bias_state != "bullish":
        lean, conviction, why = "short", "high", (
            f"confluence TOP exhaustion at strong resistance ${strong_res['price']:.4f} "
            f"({strong_res['touches']}t)")
        entry = strong_res["price"]
        stop = sr["resistance"][1]["price"] if len(sr["resistance"]) > 1 else round(entry * 1.03, 6)
        target = (near_sup or strong_sup or {"price": round(entry * 0.97, 6)})["price"]
    elif gs["direction"] == "bottom" and gs["confluence"] and at_sup and long_ok and bias_state != "bearish":
        lean, conviction, why = "long", "high", (
            f"confluence BOTTOM exhaustion at strong support ${strong_sup['price']:.4f} "
            f"({strong_sup['touches']}t)")
        entry = strong_sup["price"]
        stop = sr["support"][1]["price"] if len(sr["support"]) > 1 else round(entry * 0.97, 6)
        target = (near_res or strong_res or {"price": round(entry * 1.03, 6)})["price"]
    elif at_sup and bias_state == "bullish" and long_ok and ev["rsi_now"] <= RSI_LONG_MAX:
        lean, conviction, why = "long", "medium", (
            f"at strong support ${strong_sup['price']:.4f}, RSI {ev['rsi_now']:.0f} "
            f"pulled back, bullish regime")
        entry = strong_sup["price"]
        stop = sr["support"][1]["price"] if len(sr["support"]) > 1 else round(entry * 0.97, 6)
        target = (near_res or strong_res or {"price": round(entry * 1.03, 6)})["price"]
    elif at_res and bias_state == "bearish" and short_ok and ev["rsi_now"] >= RSI_SHORT_MIN:
        lean, conviction, why = "short", "medium", (
            f"at strong resistance ${strong_res['price']:.4f}, RSI {ev['rsi_now']:.0f} "
            f"elevated, bearish regime")
        entry = strong_res["price"]
        stop = sr["resistance"][1]["price"] if len(sr["resistance"]) > 1 else round(entry * 1.03, 6)
        target = (near_sup or strong_sup or {"price": round(entry * 0.97, 6)})["price"]

    # TRAMA filter: never surface a lean that fades a strongly-sloping trend
    # (a long into a steep downtrend, or a short into a steep uptrend). Keeps the
    # digest consistent with the alert path's steep_against suppression.
    steep_against = ((lean == "short" and slope > TRAMA_STEEP)
                     or (lean == "long" and slope < -TRAMA_STEEP))
    if steep_against:
        lean, conviction, entry, stop, target = "neutral", "low", None, None, None
        why = f"suppressed — would fade a steep TRAMA {trend} (slope {slope:+.1f}%)"

    dec = {
        "lean": lean,
        "conviction": conviction,
        "price": round(mark, 6),
        "rsi": round(ev["rsi_now"]),
        "exhaustion": gs["direction"],
        "confluence": gs["confluence"],
        "bias": bias_state or "unknown",
        "support": (strong_sup or near_sup or {}).get("price"),
        "resistance": (strong_res or near_res or {}).get("price"),
        "trend": trend,
        "trama": trama_val,
        "trama_dist": round(trama_dist, 2) if trama_dist is not None else None,
        "funding": round(ev["funding_ann"]) if ev.get("funding_ann") is not None else None,
        "doji": _doji_desc(evs.values()),
        "rationale": why,
        "ts": int(time.time()),
    }
    if entry and stop and target:
        dec.update({"entry": round(entry, 6), "stop": round(stop, 6),
                    "target": round(target, 6), "rr": _rr(entry, stop, target)})
    return dec


def format_setup_alert(coin: str, dec: dict) -> str:
    arrow = "🟢 LONG" if dec["lean"] == "long" else "🔴 SHORT"
    now_utc = datetime.now(timezone.utc).strftime("%H:%M UTC")
    lines = [
        f"*{arrow} SETUP — {coin}*  (high conviction)  ({now_utc})",
        f"${dec['price']:.4f}  |  RSI {dec['rsi']}  |  bias: {dec['bias']}",
        f"_{dec['rationale']}_",
    ]
    if dec.get("entry"):
        lines += [
            "",
            f"Entry ${dec['entry']:.4f}  |  Stop ${dec['stop']:.4f}  |  "
            f"Target ${dec['target']:.4f}  |  R:R {dec['rr']}",
        ]
    lines += ["", "_Decision support, not advice — odds, not certainty._"]
    return "\n".join(lines)


def should_alert_setup(coin: str, lean: str, state: dict) -> bool:
    """One setup alert per coin per COOLDOWN_HOURS, re-firing if the lean flips."""
    rec = state.get("_setups", {}).get(coin)
    if not rec:
        return True
    if rec.get("lean") != lean:
        return True
    return (time.time() - rec.get("ts", 0)) / 3600.0 >= COOLDOWN_HOURS


# ── Hourly digest (private chat only) ─────────────────────────────────────
_TREND_GLYPH = {"up": "📈", "down": "📉", "flat": "➡️"}


def _trend_tag(v: dict) -> str:
    t = v.get("trend", "?")
    return f"{_TREND_GLYPH.get(t, '•')} {t}"


def _fund_glyph(f) -> str:
    """Funding zone as a compact glyph+value, or '' if unknown."""
    if f is None:
        return ""
    if f >= FUNDING_ANN_HOT:
        return f"🔥 +{f}%"
    if f <= FUNDING_ANN_COLD:
        return f"🧊 {f}%"
    return f"fund {f:+d}%"


def _cross_tag(coin: str, cross: dict | None) -> str:
    """⭐ tag when a 2nd engine (macro zone / discovery class) agrees on this coin."""
    if not cross:
        return ""
    c = cross.get(coin, {})
    parts = []
    z = c.get("macro")
    if z in ("accumulate", "deep_value"):
        parts.append("macro " + ("DEEP-VALUE" if z == "deep_value" else "ACCUMULATION"))
    k = c.get("discovery")
    if k in ("buy", "fade"):
        parts.append(f"discovery {k.upper()}")
    return ("  ⭐ " + " · ".join(parts)) if parts else ""


def build_digest(decisions: dict, cross: dict | None = None,
                 ratio_map: dict | None = None) -> str:
    """Ranked, capped list of the current medium+high leans across all coins.

    Ranking: high conviction first, then RSI extremity toward the trade
    (longs = most washed-out RSI first, shorts = most stretched first).
    Each side is capped at DIGEST_MAX_PER_SIDE so the message stays scannable.
    `cross` (optional) maps coin -> {macro: zone, discovery: klass} to flag
    coins a second engine also likes. `ratio_map` (optional) maps coin -> a
    compact 'vs BTC' relative-strength tag."""
    rank = {"high": 0, "medium": 1}
    longs = [(c, v) for c, v in decisions.items()
             if v.get("lean") == "long" and v.get("conviction") in rank]
    shorts = [(c, v) for c, v in decisions.items()
              if v.get("lean") == "short" and v.get("conviction") in rank]
    longs.sort(key=lambda kv: (rank[kv[1]["conviction"]], kv[1]["rsi"]))
    shorts.sort(key=lambda kv: (rank[kv[1]["conviction"]], -kv[1]["rsi"]))
    longs, shorts = longs[:DIGEST_MAX_PER_SIDE], shorts[:DIGEST_MAX_PER_SIDE]

    def dot(v):
        return "🔺" if v["conviction"] == "high" else "•"

    def rows(items, side):
        out = []
        for c, v in items:
            level = v.get("support") if side == "long" else v.get("resistance")
            detail = []
            if level:
                dist = abs(v["price"] / level - 1) * 100
                detail.append(f"@ {'sup' if side == 'long' else 'res'} ${level:.4f} · {dist:.1f}% away")
            tval, td = v.get("trama"), v.get("trama_dist")
            if tval is not None and td is not None:
                detail.append(f"TRAMA ${tval:.4f} (price {abs(td):.1f}% "
                              f"{'above 🟢' if td >= 0 else 'below 🔴'})")
            if v.get("rr"):
                detail.append(f"R:R {v['rr']}")
            fg = _fund_glyph(v.get("funding"))
            if fg:
                detail.append(fg)
            out.append(f"  {dot(v)} *{c}*  ${v['price']:.4f}  RSI {v['rsi']}  "
                       f"{_trend_tag(v)}  ({v['bias']})")
            tail = "       " + " · ".join(detail) + _cross_tag(c, cross)
            if tail.strip():
                out.append(tail.rstrip())
            if v.get("doji"):
                out.append(f"       🕯️ {v['doji']}")
            if ratio_map and ratio_map.get(c):
                out.append(f"       {ratio_map[c]}")
        if not items:
            out.append("  _(none)_")
        return out

    now = datetime.now(timezone.utc).strftime("%H:%M UTC")
    lines = [f"*📋 SETUP DIGEST*  ({now})",
             f"_{len(longs)} long · {len(shorts)} short · scanned {len(decisions)} coins_"]
    mtag = _macro_tag(read_macro())
    if mtag:
        lines.append(f"_{mtag}_")
    lines.append("")
    lines.append("*🟢 LONGS* _(at support, RSI + trend aligned)_")
    lines += rows(longs, "long")
    lines += ["", "*🔴 SHORTS* _(at resistance, RSI + trend aligned)_"]
    lines += rows(shorts, "short")
    lines += ["", "🔺 high conviction (exhaustion at level) · • medium",
              "📈/➡️/📉 TRAMA trend · ⭐ 2nd engine agrees · 🕯️ doji reversal",
              "vs BTC 🟢 above / 🔴 below the coin's BTC-ratio TRAMA (relative strength)",
              "_Decision support, not advice._"]
    return "\n".join(lines)


def send_telegram_private(message: str) -> bool:
    """Deliver the digest to EXHAUSTION_DIGEST_CHAT_ID if set, else to all private
    chats (positive IDs) — never the group (negative)."""
    from morning_briefing import (TELEGRAM_TOKEN, TELEGRAM_CHAT_IDS,
                                  _send_to_chat, _split_for_telegram)
    if not TELEGRAM_TOKEN:
        return False
    target = os.getenv("EXHAUSTION_DIGEST_CHAT_ID")
    chats = [target] if target else [c for c in TELEGRAM_CHAT_IDS if not str(c).startswith("-")]
    if not chats:
        print("  [WARN] no private chat configured — digest not sent")
        return False
    ok = False
    for chat_id in chats:
        for chunk in _split_for_telegram(message, max_len=4000):
            if _send_to_chat(chat_id, chunk):
                ok = True
    return ok


# Mute ONLY the setup digest + discovery scan (their pushes are noise while we
# gather a performance record). Per-coin exhaustion alerts, auto-reviews, ratio
# watch and macro still push normally. Outcome-tracking always runs regardless.
SIGNALS_MUTED = os.getenv("SIGNALS_MUTED", "false").lower() in ("1", "true", "yes", "on")


def send_signal(message: str) -> bool:
    """Send a digest/discovery push unless SIGNALS_MUTED. Returns True if delivered.
    Never gate logging/tracking on this — log first, then notify via this."""
    if SIGNALS_MUTED:
        return False
    return send_telegram_private(message)


# ── Setup outcome tracker (measure which digest setups actually win) ───────
SETUP_LOG = ROOT / "reports" / "setup_log.json"
SETUP_EXPIRY_H = float(os.getenv("EXHAUSTION_SETUP_EXPIRY_H", "72"))   # unresolved setup expires after


def _load_setup_log() -> list:
    if not SETUP_LOG.exists():
        return []
    try:
        return json.loads(SETUP_LOG.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _save_setup_log(log: list) -> None:
    SETUP_LOG.parent.mkdir(parents=True, exist_ok=True)
    SETUP_LOG.write_text(json.dumps(log, indent=2), encoding="utf-8")


def _with_trend(d: dict) -> bool:
    """Is the lean aligned with the TRAMA trend (long+up / short+down)?"""
    t = d.get("trend")
    return (d["lean"] == "long" and t == "up") or (d["lean"] == "short" and t == "down")


def log_setups(decisions: dict) -> None:
    """Open a tracked record for each high/medium lean that carries a full plan,
    unless one is already open for that coin+lean. Runs every scan, mute or not."""
    leans = [(c, d) for c, d in decisions.items()
             if d.get("lean") in ("long", "short") and d.get("conviction") in ("high", "medium")
             and d.get("entry") and d.get("stop") and d.get("target")]
    if not leans:
        return
    log = _load_setup_log()
    open_keys = {(e["coin"], e["lean"]) for e in log if e.get("status") == "open"}
    macro = read_macro()                          # record the macro regime at entry
    now = int(time.time())
    added = False
    for c, d in leans:
        if (c, d["lean"]) in open_keys:
            continue
        log.append({
            "coin": c, "lean": d["lean"], "conviction": d["conviction"],
            "mode": "confluence" if d.get("confluence") else "single",
            "entry_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "trigger_ts": now,
            "trigger_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "entry": d["entry"], "stop": d["stop"], "target": d["target"], "rr": d.get("rr"),
            "ctx": {"rsi": d.get("rsi"), "trend": d.get("trend"), "funding": d.get("funding"),
                    "bias": d.get("bias"), "doji": bool(d.get("doji")),
                    "with_trend": _with_trend(d),
                    "macro": (macro or {}).get("regime"),
                    "macro_align": _macro_align(d["lean"], macro)},
            "mfe_r": 0.0, "mae_r": 0.0, "status": "open",
            "outcome": None, "exit_price": None, "exit_ts": None, "hours": 0.0,
        })
        added = True
    if added:
        _save_setup_log(log)


def update_setups(price_map: dict) -> list:
    """Advance every open setup from the current bar. Resolves loss (stop hit —
    checked first, conservative) / win (target hit) / expired, tracking MFE/MAE in
    R-multiples. price_map: coin -> {mark, hi, lo}."""
    log = _load_setup_log()
    now = time.time()
    changed = False
    for e in log:
        if e.get("status") != "open":
            continue
        pm = price_map.get(e["coin"])
        entry, stop, target, lean = e["entry"], e["stop"], e["target"], e["lean"]
        risk = abs(entry - stop) or 1e-12
        if pm:
            hi, lo = pm["hi"], pm["lo"]
            if lean == "long":
                e["mfe_r"] = round(max(e["mfe_r"], (hi - entry) / risk), 2)
                e["mae_r"] = round(max(e["mae_r"], (entry - lo) / risk), 2)
                if lo <= stop:
                    e.update(status="loss", outcome="loss", exit_price=stop)
                elif hi >= target:
                    e.update(status="win", outcome="win", exit_price=target)
            else:  # short
                e["mfe_r"] = round(max(e["mfe_r"], (entry - lo) / risk), 2)
                e["mae_r"] = round(max(e["mae_r"], (hi - entry) / risk), 2)
                if hi >= stop:
                    e.update(status="loss", outcome="loss", exit_price=stop)
                elif lo <= target:
                    e.update(status="win", outcome="win", exit_price=target)
        e["hours"] = round((now - e["trigger_ts"]) / 3600, 1)
        if e["status"] == "open" and e["hours"] >= SETUP_EXPIRY_H:
            e.update(status="expired", outcome="expired", exit_price=(pm or {}).get("mark"))
        if e["status"] != "open":
            e["exit_ts"] = int(now)
        changed = True
    if changed:
        _save_setup_log(log)
    return log


def format_setup_report() -> str:
    """Win-rate + average excursion, overall and by segment — for `--setups`."""
    log = _load_setup_log()
    if not log:
        return "No setups tracked yet."
    n_open = sum(1 for e in log if e.get("status") == "open")
    n_exp = sum(1 for e in log if e.get("outcome") == "expired")
    lines = [f"SETUP PERFORMANCE — {len(log)} logged · {n_open} open · "
             f"{len(log) - n_open - n_exp} decided · {n_exp} expired\n"]

    def seg(label, rows):
        decided = [e for e in rows if e.get("outcome") in ("win", "loss")]
        if not decided:
            op = sum(1 for e in rows if e.get("status") == "open")
            lines.append(f"  {label:22} — no decided yet ({op} open)")
            return
        w, n = sum(1 for e in decided if e["outcome"] == "win"), len(decided)
        amfe = sum(e["mfe_r"] for e in decided) / n
        amae = sum(e["mae_r"] for e in decided) / n
        lines.append(f"  {label:22} {w}/{n} wins ({100 * w / n:.0f}%)  "
                     f"avgMFE {amfe:.2f}R · avgMAE {amae:.2f}R")

    seg("ALL", log)
    seg("longs", [e for e in log if e["lean"] == "long"])
    seg("shorts", [e for e in log if e["lean"] == "short"])
    seg("high conviction", [e for e in log if e["conviction"] == "high"])
    seg("medium conviction", [e for e in log if e["conviction"] == "medium"])
    seg("confluence", [e for e in log if e["mode"] == "confluence"])
    seg("with-trend (TRAMA)", [e for e in log if e["ctx"].get("with_trend")])
    seg("counter-trend", [e for e in log if not e["ctx"].get("with_trend")])
    seg("with doji", [e for e in log if e["ctx"].get("doji")])
    seg("R:R >= 2", [e for e in log if (e.get("rr") or 0) >= 2])
    seg("macro-aligned", [e for e in log if e["ctx"].get("macro_align") == "aligned"])
    seg("counter-macro", [e for e in log if e["ctx"].get("macro_align") == "counter"])
    lines.append("\n(win = target hit before stop · loss = stop first · "
                 "MFE/MAE = best/worst excursion in R-multiples · expired = neither in "
                 f"{SETUP_EXPIRY_H:.0f}h)")
    return "\n".join(lines)


# ── Event-driven Claude review (subscription only; runs on an exhaustion signal) ─
# Generates a written review via headless Claude Code. API credentials are
# STRIPPED from the call, so it can only use the interactive subscription login
# and can never bill the metered API. Capped per-run and per-day (env-tunable).
REVIEW_ENABLED = os.getenv("EXHAUSTION_REVIEW_ENABLED", "true").lower() != "false"
REVIEW_MAX_PER_RUN = int(os.getenv("EXHAUSTION_REVIEW_MAX_PER_RUN", "3"))
REVIEW_DAILY_CAP = int(os.getenv("EXHAUSTION_REVIEW_DAILY_CAP", "10"))
# Per-coin cooldown: don't re-review the same coin within this many hours, so a
# coin that flip-flops top↔bottom can't burn multiple reviews from the budget.
REVIEW_COOLDOWN_HOURS = float(os.getenv("EXHAUSTION_REVIEW_COOLDOWN_HOURS", "4"))
# Resolve the claude binary by absolute path — pm2's PATH may omit ~/.local/bin.
import shutil
CLAUDE_BIN = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")


def _claude_env() -> dict:
    """Env with API credentials removed — forces the subscription login, so a
    review can never fall through to a billed API key."""
    env = dict(os.environ)
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(k, None)
    return env


def build_review_prompt(coin: str, direction: str, mode: str, ev: dict, dec: dict) -> str:
    """Market-data-only prompt — contains NO keys, tokens, or credentials."""
    sr = ev.get("sr", {"support": [], "resistance": []})
    res = " · ".join(f"${z['price']:.4f} ({z['touches']}t)" for z in sr["resistance"][:3]) or "—"
    sup = " · ".join(f"${z['price']:.4f} ({z['touches']}t)" for z in sr["support"][:3]) or "—"
    tag = "confluence" if mode == "confluence" else f"{ev['source']} only"
    emoji = "⚠️" if direction == "top" else "🩸"
    return (
        "You are a crypto trading assistant. Write a SHORT Telegram review (Markdown) of the "
        "exhaustion setup below. Keep it tight and use exactly this structure:\n"
        f"🔍 *EXHAUSTION REVIEW — {coin}*   ({emoji} {tag} {direction.upper()})\n"
        "*Verdict:* <1-2 sentences — the honest bottom line>\n"
        "<one line: price | RSI | funding | bias>\n"
        "*Levels* — bullet nearest resistance and support\n"
        "*How to play it* — 2-3 bullets: entry/stop/target with R:R, plus what to avoid\n"
        "End with one italic caveat line. Use ONLY the numbers below — do not invent any. "
        "Be direct and honest; this is decision support, not financial advice.\n\n"
        "DATA:\n"
        f"- direction: {direction} ({tag})\n"
        f"- price: ${ev['mark']:.4f}\n"
        f"- 24h change: {ev['chg_24h']:+.1f}%; from 72h high {ev['from_high_pct']:+.1f}%, "
        f"from 72h low {ev['from_low_pct']:+.1f}%\n"
        f"- RSI(14) 1h: {ev['rsi_now']:.0f}\n"
        f"- funding: {ev['funding_ann']:+.0f}%/yr ({_funding_zone(ev['funding_ann'])})\n"
        f"- resistance: {res}\n"
        f"- support: {sup}\n"
        f"- TRAMA trend: {ev.get('trend', '?')} (slope {ev.get('trama_slope', 0):+.1f}% — "
        f"flat=range/fade-friendly, steep=trend/don't-fight)\n"
        f"- bot trend bias: {dec.get('bias', 'unknown')}\n"
    )


def _run_claude(prompt: str) -> str | None:
    """Run headless Claude Code on the subscription with API credentials stripped
    (can never bill the API). Returns text, or None on any failure. Logs only
    exception type names — never secrets."""
    try:
        r = subprocess.run([CLAUDE_BIN, "-p", prompt], env=_claude_env(),
                           capture_output=True, text=True, timeout=150)
    except Exception as e:
        print(f"  [review] claude invocation failed: {type(e).__name__}")
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return r.stdout.strip()


def claude_review(prompt: str) -> str | None:
    """Auto (event-driven) review — gated by the REVIEW_ENABLED toggle."""
    if not REVIEW_ENABLED:
        return None
    review = _run_claude(prompt)
    if review is None:
        print("  [review] no review produced (subscription/CLI unavailable) — skipped")
    return review


def review_budget_ok(state: dict, run_count: int) -> bool:
    """True if another review is allowed — within the per-run and per-day caps.
    Preserves the per-coin `last` review timestamps across the daily reset."""
    if run_count >= REVIEW_MAX_PER_RUN:
        return False
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rec = state.get("_reviews")
    if not rec or rec.get("date") != today:
        rec = {"date": today, "count": 0, "last": (rec or {}).get("last", {})}
        state["_reviews"] = rec
    return rec["count"] < REVIEW_DAILY_CAP


def review_cooldown_ok(coin: str, state: dict) -> bool:
    """False if `coin` was reviewed within REVIEW_COOLDOWN_HOURS (dedup flip-flops)."""
    last = state.get("_reviews", {}).get("last", {}).get(coin, 0)
    return (time.time() - last) / 3600.0 >= REVIEW_COOLDOWN_HOURS


# ── On-demand review (admin /review command) ──────────────────────────────
def _btc_ratio_block(coin: str) -> str | None:
    """Formatted {coin}/BTC relative-strength block to fold into a review, so every
    review also weighs whether the coin is gaining or losing ground against BTC.
    None for BTC itself or when the ratio has no data. Lazy-imports ratio_watch
    to avoid a circular import."""
    if coin.upper() == "BTC":
        return None
    try:
        import ratio_watch as rw
        e1 = rw.evaluate_ratio(f"{coin}/BTC", "1h")
        ed = rw.evaluate_ratio(f"{coin}/BTC", "1d")
    except Exception:
        return None
    if not e1 and not ed:
        return None

    def line(tag, ev):
        if not ev:
            return None
        return (f"- {tag} {coin}/BTC: {rw.disp(ev['price'], ev['unit'])} · RSI {ev['rsi']:.0f} · "
                f"TRAMA trend {ev['trend']} ({ev['trama_slope']:+.1f}%), price "
                f"{'ABOVE' if ev['above_trama'] else 'BELOW'} its TRAMA")

    prim = ed or e1
    out = [f"RELATIVE TO BTC — is {coin} gaining or losing ground vs BTC? "
           f"({coin} priced in BTC, small alts quoted in sats = ratio × 1e8):"]
    out += [ln for ln in (line("1H", e1), line("1D", ed)) if ln]
    if ed:
        out.append(f"- {coin}/BTC change: 7d {ed['chg7']:+.1f}%")
    r = rw.disp(prim["res"]["price"], prim["unit"]) if prim.get("res") else "—"
    s = rw.disp(prim["sup"]["price"], prim["unit"]) if prim.get("sup") else "—"
    out.append(f"- {coin}/BTC nearest shelf: res {r} · sup {s}")
    return "\n".join(out)


def _ratio_synthesis_instr(coin: str, ratio_block: str | None) -> str:
    """Instruction appended when a RELATIVE TO BTC block is present, asking the
    model to synthesize the USD read and the vs-BTC read into one opinion."""
    if not ratio_block:
        return ""
    return (
        f"\nA RELATIVE TO BTC section is included. ALSO weigh whether {coin} is out- or "
        f"under-performing BTC: add one *Why* bullet on it, and let it shape the bottom-line "
        f"call — a coin strong in USD but weak vs BTC is a poorer hold than one strong in both. "
        f"If the {coin}/BTC ratio is falling (below its TRAMA / trend down), say plainly that "
        f"simply holding BTC may be the better expression, and only favour {coin} if BOTH the USD "
        f"chart and the {coin}/BTC ratio support it.\n"
    )


def build_full_review_prompt(coin: str, evs: dict, bias: dict | None, regime: dict | None,
                             ratio_block: str | None = None) -> str:
    """Richer narrative prompt for the on-demand /review command. Market data only."""
    prim = evs.get("HL") or next(iter(evs.values()))
    sr = prim["sr"]

    def zones(zs):
        return " · ".join(f"${z['price']:.4f} ({z['touches']}t)" for z in zs[:4]) or "none"

    def venue_line(tag, ev):
        if not ev:
            return None
        return (f"{tag} 24h {ev['chg_24h']:+.1f}%, RSI {ev['rsi_now']:.0f}, "
                f"funding {ev['funding_ann']:+.0f}%/yr, dir={ev['direction'] or 'chop'}, "
                f"signals={[k for k, _ in ev['signals']]}")

    b = bias or {}
    lines = [venue_line("HL:", evs.get("HL")), venue_line("Binance:", evs.get("Binance"))]
    data = "\n".join([
        "DATA (use ONLY these numbers, never invent prices):",
        f"- price: ${prim['mark']:.4f}",
        *[f"- {ln}" for ln in lines if ln],
        f"- from 72h high {prim['from_high_pct']:+.1f}%, from 72h low {prim['from_low_pct']:+.1f}%",
        f"- resistance: {zones(sr['resistance'])}",
        f"- support: {zones(sr['support'])}",
        f"- TRAMA trend: {prim.get('trend', '?')} (slope {prim.get('trama_slope', 0):+.1f}% — "
        f"flat = range/mean-revert, steep = strong trend/don't fade)",
        f"- bot trend bias: {b.get('bias', 'unknown')} (state {b.get('state', '?')}, "
        f"long_allowed={b.get('long_allowed')}, short_allowed={b.get('short_allowed')})",
        f"- market regime: {(regime or {}).get('regime', '?')}",
    ])
    if ratio_block:
        data += "\n\n" + ratio_block
    return (
        f"You are a crypto trading assistant replying to an admin's /review command on "
        f"Telegram. Write a detailed but tight review of {coin} in Markdown, in this style:\n\n"
        f"*{coin} @ $PRICE — <one-line characterization + the bottom-line call>*\n\n"
        "*Why* — 2-4 bullets: the honest case, including the main risk.\n\n"
        "*How to play it*\n"
        "• If already in: how to manage (trail stop to which levels / take partials)\n"
        "• Fresh entry: entry zone / stop / target, all as concrete levels\n"
        "• What to avoid\n\n"
        f"{_ratio_synthesis_instr(coin, ratio_block)}"
        "End with one italic caveat line. Use ONLY the numbers in DATA. Be direct and honest; "
        "decision support, not financial advice. Keep under ~1600 characters.\n\n"
        f"{data}"
    )


def review_coin_cli(coin_raw: str) -> tuple:
    """On-demand review for `--review <COIN>`. Returns (exit_code, message).
    Anti-spam: strict input validation + a single-flight lock so it cannot be
    made to spawn concurrent reviews. Runs on the subscription (never billed)."""
    coin = (coin_raw or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{1,15}", coin):
        return 2, "⚠️ Invalid coin. Send a ticker, e.g. `/review ETH` or `/review VIRTUAL`."

    lock = STATE_FILE.parent / "review.lock"
    if lock.exists() and (time.time() - lock.stat().st_mtime) < 120:
        return 3, "⏳ A review is already running — try again in a moment."

    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(str(time.time()))
        HL.prefetch()
        BINANCE.prefetch()
        evs = {}
        for src in (HL, BINANCE):
            try:
                if coin in src.universe():
                    ev = evaluate(coin, src)
                    if ev:
                        evs[src.name] = ev
            except Exception:
                pass
        if not evs:
            return 4, f"❓ *{coin}* isn't tradable on Hyperliquid or Binance."
        bias_map, regime = read_intelligence()
        ratio_block = _btc_ratio_block(coin)      # also weigh the coin's BTC counterpart
        text = _run_claude(build_full_review_prompt(
            coin, evs, bias_map.get(coin.lower()), regime, ratio_block))
        return (0, text) if text else (5, "⚠️ Review unavailable right now (subscription/CLI).")
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


# ── Daily-timeframe review (swing structure — /review <coin> daily) ───────
_BAR_MS = {"1d": 86_400_000, "4h": 14_400_000}


def _daily_ohlcv(coin: str, days: int = 250, interval: str = "1d") -> tuple:
    """Closes/highs/lows/vols from Binance perp, else HL. (data, venue). `days` is a
    bar count — daily by default; `interval="4h"` gives the last `days` 4h bars."""
    try:
        r = requests.get(BINANCE.FAPI + "/fapi/v1/klines",
                         params={"symbol": coin + "USDT", "interval": interval,
                                 "limit": min(1500, days)}, timeout=15)
        if r.status_code == 200 and r.json():
            k = r.json()
            return ([float(x[4]) for x in k], [float(x[2]) for x in k],
                    [float(x[3]) for x in k], [float(x[5]) for x in k]), "Binance"
    except Exception:
        pass
    try:
        now = int(time.time() * 1000)
        raw = _post({"type": "candleSnapshot",
                     "req": {"coin": coin, "interval": interval,
                             "startTime": now - days * _BAR_MS[interval], "endTime": now}})
        if raw:
            return ([float(x["c"]) for x in raw], [float(x["h"]) for x in raw],
                    [float(x["l"]) for x in raw], [float(x["v"]) for x in raw]), "HL"
    except Exception:
        pass
    return None, None


def evaluate_daily(coin: str) -> dict | None:
    ohlcv, venue = _daily_ohlcv(coin)
    if not ohlcv or len(ohlcv[0]) < 60:      # need a couple months of daily bars
        return None
    closes, highs, lows, vols = ohlcv
    price = closes[-1]
    r = rsi(closes)
    rsi_d = r[-1] if r else 50.0

    def sma(vals, n):
        w = vals[-n:] if len(vals) >= n else vals
        return sum(w) / len(w)

    ma50, ma200 = sma(closes, 50), sma(closes, 200)
    hi = max(highs[-90:]) if len(highs) >= 90 else max(highs)
    lo = min(lows[-90:]) if len(lows) >= 90 else min(lows)
    chg7 = (price / closes[-8] - 1) * 100 if len(closes) > 8 else 0.0
    chg30 = (price / closes[-31] - 1) * 100 if len(closes) > 31 else 0.0
    base_vol = sum(vols[-30:-3]) / max(1, len(vols[-30:-3]))
    vol_rvol = (sum(vols[-3:]) / 3) / base_vol if base_vol else 0.0

    # funding (current) from the live ctx if available
    funding_ann = None
    src = BINANCE if coin in BINANCE.universe() else (HL if coin in HL.universe() else None)
    if src:
        try:
            ctx = src.ctx(coin)
            price = ctx["mark"]
            funding_ann = ctx["funding_rate"] * src.fund_per_year * 100.0
        except Exception:
            pass

    trama_val, trama_slope, trend = trama_trend(
        price, trama(closes, highs, lows, length=TRAMA_LEN_D))

    return {
        "coin": coin, "venue": venue, "price": price, "rsi": rsi_d,
        "ma50": ma50, "ma200": ma200,
        "trama": trama_val, "trama_slope": trama_slope, "trend": trend,
        "vs_ma50": (price / ma50 - 1) * 100 if ma50 else 0.0,
        "vs_ma200": (price / ma200 - 1) * 100 if ma200 else 0.0,
        "hi90": hi, "lo90": lo,
        "from_hi90": (price / hi - 1) * 100, "from_lo90": (price / lo - 1) * 100,
        "chg7": chg7, "chg30": chg30, "rvol": round(vol_rvol, 2),
        "funding_ann": funding_ann, "days": len(closes),
        "sr": compute_sr(highs, lows, price, tol=0.008),   # slightly wider clustering on daily
        "accdist": accdist_read(closes, highs, lows, vols, k=min(ACCDIST_K, 20)),
        "breakout": sr_breakout(price, highs, lows,
                                compute_sr(highs, lows, price, tol=0.008)),
    }


def build_daily_review_prompt(coin: str, d: dict, ratio_block: str | None = None) -> str:
    sr = d["sr"]

    def zones(zs):
        return " · ".join(f"${z['price']:.4f} ({z['touches']}t)" for z in zs[:4]) or "none"

    fund = f"{d['funding_ann']:+.0f}%/yr" if d["funding_ann"] is not None else "n/a"
    data = "\n".join([
        "DATA (daily timeframe — use ONLY these numbers, never invent):",
        f"- price: ${d['price']:.4f}  ({d['venue']})",
        f"- daily RSI(14): {d['rsi']:.0f}",
        f"- 50-day MA: ${d['ma50']:.4f}  (price {d['vs_ma50']:+.1f}% vs 50D)",
        f"- 200-day MA: ${d['ma200']:.4f}  (price {d['vs_ma200']:+.1f}% vs 200D)",
        f"- TRAMA trend (daily): {d.get('trend', '?')} (slope {d.get('trama_slope', 0) or 0:+.1f}% — "
        f"flat = range/mean-revert, steep = strong trend/don't fade)",
        f"- 90-day range: ${d['lo90']:.4f} ({d['from_lo90']:+.0f}%) .. ${d['hi90']:.4f} ({d['from_hi90']:+.0f}%)",
        f"- change: 7d {d['chg7']:+.1f}%, 30d {d['chg30']:+.1f}%",
        f"- daily volume RVOL(3d vs 30d): {d['rvol']}x",
        f"- funding: {fund}",
        f"- daily resistance: {zones(sr['resistance'])}",
        f"- daily support: {zones(sr['support'])}",
    ])
    if ratio_block:
        data += "\n\n" + ratio_block
    return (
        f"You are a crypto trading assistant replying to an admin's /review command on "
        f"Telegram. Write a *daily-timeframe SWING* review of {coin} in Markdown, in this style:\n\n"
        f"*{coin} @ $PRICE — <one-line characterization + the swing bottom-line call>*\n\n"
        "*Why* — 2-4 bullets: the daily trend (price vs 50D/200D MAs), where it sits in the "
        "90-day range, momentum, and the main risk.\n\n"
        "*How to play it (swing — days to weeks)*\n"
        "• If already in: how to manage on the daily (trail to which daily level / MA)\n"
        "• Fresh entry: entry zone / stop / target as DAILY levels, with wider swing stops\n"
        "• Confirmation: express triggers as *daily closes* above/below key levels\n"
        "• What to avoid\n\n"
        f"{_ratio_synthesis_instr(coin, ratio_block)}"
        "End with one italic caveat line. Frame everything for a multi-day hold (not a scalp). "
        "Use ONLY the numbers in DATA. Be direct and honest; decision support, not financial "
        "advice. Keep under ~1600 characters.\n\n"
        f"{data}"
    )


def review_daily_cli(coin_raw: str) -> tuple:
    """On-demand daily/swing review for `--review-daily <COIN>`."""
    coin = (coin_raw or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{1,15}", coin):
        return 2, "⚠️ Invalid coin — e.g. `/review ETH daily`."
    lock = STATE_FILE.parent / "review.lock"
    if lock.exists() and (time.time() - lock.stat().st_mtime) < 120:
        return 3, "⏳ A review is already running — try again in a moment."
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(str(time.time()))
        d = evaluate_daily(coin)
        if not d:
            return 4, (f"❓ *{coin}* — not enough daily history on Binance/HL for a daily "
                       f"review (needs ~60+ days; new/low-cap coins won't have it).")
        ratio_block = _btc_ratio_block(coin)      # also weigh the coin's BTC counterpart
        text = _run_claude(build_daily_review_prompt(coin, d, ratio_block))
        return (0, text) if text else (5, "⚠️ Review unavailable right now (subscription/CLI).")
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


# ── Ratio-chart review (alt/BTC relative strength — --review-ratio XRP/BTC) ─
def build_ratio_review_prompt(pair: str, e1: dict | None, ed: dict | None) -> str:
    import ratio_watch as rw
    prim = ed or e1
    num, den, unit = prim["num"], prim["den"], prim["unit"]

    def fp(p):
        return rw.disp(p, unit)

    def zones(zs):
        return " · ".join(f"{fp(z['price'])} ({z['touches']}t)" for z in zs[:4]) or "none"

    def tf(tag, ev):
        if not ev:
            return [f"- {tag}: no data on this timeframe"]
        return [
            f"- {tag}: {fp(ev['price'])} · RSI {ev['rsi']:.0f} · TRAMA trend {ev['trend']} "
            f"(slope {ev['trama_slope']:+.1f}%), price "
            f"{'ABOVE the TRAMA line' if ev['above_trama'] else 'BELOW the TRAMA line'}",
            f"    {tag} resistance: {zones(ev['sr']['resistance'])}",
            f"    {tag} support: {zones(ev['sr']['support'])}",
        ]

    data = "\n".join([
        f"DATA — {pair} ratio ({num} priced in {den}"
        + ("; small alts quoted in sats = ratio×1e8, larger in BTC" if unit == "sats" else "")
        + "). Use ONLY these numbers, never invent:",
        *tf("1H", e1),
        *tf("1D", ed),
        (f"- daily change: 1d {ed['chg1']:+.1f}% · 7d {ed['chg7']:+.1f}%" if ed
         else (f"- 24h change: {e1['chg7']:+.1f}%" if e1 else "")),
        f"- data source: {prim['mode']} pair",
    ])
    return (
        f"You are a crypto trading assistant replying to an admin's /review command on "
        f"Telegram. Write a review of the *{pair} ratio* — {num} strength versus {den} (is {num} "
        f"gaining or losing ground against {den}?) — in Markdown, in this style:\n\n"
        f"*{pair} @ {fp(prim['price'])} — <one line: is {num} out- or under-performing {den}, "
        f"plus the bottom line>*\n\n"
        "*Why* — 2-4 bullets: the ratio trend on 1D vs 1H (price vs TRAMA), the key shelf it's "
        "capped under / holding above, and what would confirm rotation either way.\n\n"
        "*What it means for positioning*\n"
        f"• Rising ratio = favour {num} over {den}; falling = favour {den}\n"
        "• The level / close that flips the read (reclaim = rotate in, lose = rotate out)\n"
        "• What to avoid\n\n"
        "This is a RELATIVE-strength read, NOT a USD price call — frame it as which of the two to "
        "favour. End with one italic caveat. Use ONLY the numbers in DATA; decision support, not "
        "advice. Keep under ~1400 characters.\n\n"
        f"{data}"
    )


def review_ratio_cli(pair_raw: str) -> tuple:
    """On-demand ratio-chart review for `--review-ratio NUM/DEN` (denominator
    defaults to BTC). Lazy-imports ratio_watch to avoid a circular import."""
    pair = (pair_raw or "").strip().upper().replace("-", "/").replace(":", "/")
    if "/" not in pair:
        pair += "/BTC"
    if not re.fullmatch(r"[A-Z0-9]{1,15}/[A-Z0-9]{1,15}", pair):
        return 2, "⚠️ Invalid pair — e.g. `/review XRP/BTC`."
    lock = STATE_FILE.parent / "review.lock"
    if lock.exists() and (time.time() - lock.stat().st_mtime) < 120:
        return 3, "⏳ A review is already running — try again in a moment."
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(str(time.time()))
        import ratio_watch as rw
        e1, ed = rw.evaluate_ratio(pair, "1h"), rw.evaluate_ratio(pair, "1d")
        if not e1 and not ed:
            return 4, (f"❓ Couldn't load the *{pair}* ratio — no native Binance pair, and no "
                       f"USDT market for both sides.")
        text = _run_claude(build_ratio_review_prompt(pair, e1, ed))
        return (0, text) if text else (5, "⚠️ Review unavailable right now (subscription/CLI).")
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


# ── Board-wide market review (--review-market) ─────────────────────────────
# The per-coin reviews answer "what about X?"; this answers "what is the market
# doing?" — the regime (bull trend vs bull breakout vs bearish rally vs bear
# trend), where the rotation money is parked (USDT.D), and whether alts are
# genuinely breaking their ranges board-wide or only a couple of names are.
# Breadth is computed LIVE from daily candles across the configured universe
# rather than read from a cache, so the answer is never quietly stale.
MARKET_WORKERS = int(os.getenv("MARKET_BREADTH_WORKERS", "6"))
MARKET_RANGE_N = 20        # daily bars — the near-term range
MARKET_STRUCT_N = 55       # daily bars — the structural range alts "break out" of
MARKET_MAJORS = ("BTC", "ETH", "SOL")


def _breadth_one(coin: str) -> dict | None:
    """Daily structure for one coin — candles only (no ctx/funding call), so the
    board-wide sweep costs one request per coin. None when history is too thin."""
    try:
        ohlcv, _ = _daily_ohlcv(coin, days=250)
    except Exception:
        return None
    if not ohlcv or len(ohlcv[0]) < 60:
        return None
    closes, highs, lows, vols = ohlcv
    price = closes[-1]

    def sma(vals, n):
        w = vals[-n:] if len(vals) >= n else vals
        return sum(w) / len(w)

    ma50 = sma(closes, 50)
    ma200 = sma(closes, 200) if len(closes) >= 200 else None

    # Range windows exclude the live bar, so "breaking out" means clearing the
    # range as it stood *before* today — not comparing price against itself.
    def brk(n: int) -> str:
        if len(highs) < n + 2:
            return "in"
        hh, ll = max(highs[-n - 1:-1]), min(lows[-n - 1:-1])
        return "up" if price >= hh else "down" if price <= ll else "in"

    r = rsi(closes)
    _, tslope, trend = trama_trend(price, trama(closes, highs, lows, length=TRAMA_LEN_D))
    base_vol = sum(vols[-30:-3]) / max(1, len(vols[-30:-3]))
    return {
        "coin": coin,
        "price": price,
        "rsi": r[-1] if r else 50.0,
        "trend": trend,
        "trama_slope": tslope or 0.0,
        "above_ma50": price > ma50,
        "above_ma200": (price > ma200) if ma200 else None,
        "chg1": (price / closes[-2] - 1) * 100 if len(closes) > 1 else 0.0,
        "chg7": (price / closes[-8] - 1) * 100 if len(closes) > 8 else 0.0,
        "chg30": (price / closes[-31] - 1) * 100 if len(closes) > 31 else 0.0,
        "range20": brk(MARKET_RANGE_N),
        "range55": brk(MARKET_STRUCT_N),
        "rvol": round((sum(vols[-3:]) / 3) / base_vol, 2) if base_vol else 0.0,
    }


def market_breadth(coins: list) -> dict:
    """Board-wide daily-structure sweep + the aggregates that separate a real
    regime change from a two-coin move. One candle request per coin, run
    concurrently (bounded) so the whole board lands in a few seconds."""
    rows = []
    with ThreadPoolExecutor(max_workers=MARKET_WORKERS) as pool:
        for row in pool.map(_breadth_one, coins):
            if row:
                rows.append(row)
    if not rows:
        return {"n": 0, "rows": []}

    n = len(rows)
    btc = next((r for r in rows if r["coin"] == "BTC"), None)
    alts = [r for r in rows if r["coin"] != "BTC"]
    ma200_rows = [r for r in rows if r["above_ma200"] is not None]

    def share(sub, total=None):
        total = n if total is None else total
        return round(100 * len(sub) / total) if total else 0

    def median(vals):
        s = sorted(vals)
        return s[len(s) // 2] if s else 0.0

    beat7 = [r for r in alts if btc and r["chg7"] > btc["chg7"]]
    beat30 = [r for r in alts if btc and r["chg30"] > btc["chg30"]]
    return {
        "n": n,
        "rows": rows,
        "btc": btc,
        "up": share([r for r in rows if r["trend"] == "up"]),
        "down": share([r for r in rows if r["trend"] == "down"]),
        "above_ma50": share([r for r in rows if r["above_ma50"]]),
        "above_ma200": share([r for r in ma200_rows if r["above_ma200"]], len(ma200_rows)),
        "ma200_n": len(ma200_rows),
        "brk20_up": [r["coin"] for r in rows if r["range20"] == "up"],
        "brk20_dn": [r["coin"] for r in rows if r["range20"] == "down"],
        "brk55_up": [r["coin"] for r in rows if r["range55"] == "up"],
        "brk55_dn": [r["coin"] for r in rows if r["range55"] == "down"],
        "green1": share([r for r in rows if r["chg1"] > 0]),
        "med7": median([r["chg7"] for r in rows]),
        "med30": median([r["chg30"] for r in rows]),
        "beat_btc7": share(beat7, len(alts)),
        "beat_btc30": share(beat30, len(alts)),
        "leaders": sorted(rows, key=lambda r: -r["chg7"])[:6],
        "laggards": sorted(rows, key=lambda r: r["chg7"])[:4],
    }


# ── TRAMA board (--review-trama [1D|4H]) ────────────────────────────────────
# Which coins are in a CONFIRMED trend against their TRAMA: price on one side AND
# the TRAMA sloping the same way (trama_trend's up/down). Everything else — price
# across a flat TRAMA, or price and slope disagreeing — is "ranging". Candles only,
# no model call, so it answers in seconds. Uses the live (unclosed) bar's price.
TRAMA_BOARD_TFS = {"1D": "1d", "4H": "4h"}
TRAMA_BOARD_BARS = 250     # warm-up: the adaptive series is seeded from the first close


def _trama_one(coin: str, interval: str) -> dict | None:
    try:
        ohlcv, _ = _daily_ohlcv(coin, days=TRAMA_BOARD_BARS, interval=interval)
    except Exception:
        return None
    if not ohlcv:
        return None
    closes, highs, lows, _ = ohlcv
    price = closes[-1]
    val, slope, trend = trama_trend(price, trama(closes, highs, lows, length=TRAMA_LEN_D))
    if val is None:
        return None
    return {"coin": coin, "trend": trend, "slope": slope,
            "dist": (price / val - 1) * 100}


def trama_board_cli(tf_raw: str = "") -> tuple:
    """(exit_code, message) — plain text (the Telegram reply path has no parse mode)."""
    tf = (tf_raw or "1D").upper()
    if tf not in TRAMA_BOARD_TFS:
        return 2, f"Usage: /review trama [{'|'.join(TRAMA_BOARD_TFS)}]"
    coins = sorted({*DEFAULT_COINS, *MARKET_MAJORS})
    with ThreadPoolExecutor(max_workers=MARKET_WORKERS) as pool:
        rows = [r for r in pool.map(lambda c: _trama_one(c, TRAMA_BOARD_TFS[tf]), coins) if r]
    if not rows:
        return 4, "⚠️ Couldn't load candles for a TRAMA read — try again shortly."

    up = sorted((r for r in rows if r["trend"] == "up"), key=lambda r: -r["dist"])
    down = sorted((r for r in rows if r["trend"] == "down"), key=lambda r: r["dist"])
    flat = sorted(r["coin"] for r in rows if r["trend"] == "flat")

    def line(r):
        return f"{r['coin']:<8} {r['dist']:+6.1f}%  slope {r['slope']:+.1f}%"

    out = [f"📐 TRAMA board — {tf} (length {TRAMA_LEN_D}) · {len(rows)} coins",
           f"Confirmed = price on that side of TRAMA and TRAMA sloping the same way "
           f"(>{TRAMA_FLAT_PCT}% over {TRAMA_SLOPE_K} bars). % = price vs TRAMA.",
           "", f"🟢 ABOVE TRAMA, RISING ({len(up)})"]
    out += [line(r) for r in up] or ["none"]
    out += ["", f"🔴 BELOW TRAMA, FALLING ({len(down)})"]
    out += [line(r) for r in down] or ["none"]
    out += ["", f"➡️ RANGING / UNCONFIRMED ({len(flat)})", ", ".join(flat) or "none"]
    if len(rows) < len(coins):
        out.append(f"\n({len(coins) - len(rows)} coins skipped — not enough history)")
    return 0, "\n".join(out)


def _usdtd_block() -> str:
    """USDT.D — the risk-on/off rotation gauge. Its own S/R read plus the 7d/30d
    drift, since direction of travel matters more than the level. USDT.D moves
    INVERSELY to crypto, so the prompt spells that out to stop it being read as
    a plain bullish/bearish number."""
    sig = read_usdt_dominance()
    if not sig:
        return "- USDT.D: no read cached (run `python usdt_dominance.py`)"
    lines = [
        f"- USDT.D (stablecoin dominance — INVERSE to crypto: falling USDT.D = money "
        f"leaving stables into coins = bullish; rising = flight to safety = bearish):",
        f"    now {sig['usdt_d']}% · {sig['state']} · trend {sig['trend']} → {sig['crypto']}",
    ]
    lv = []
    if sig.get("support"):
        lv.append(f"support {sig['support']}%")
    if sig.get("resistance"):
        lv.append(f"resistance {sig['resistance']}%")
    if lv:
        lines.append(f"    USDT.D levels: {' / '.join(lv)} "
                     f"(USDT.D losing support = risk-on; reclaiming resistance = risk-off)")
    try:
        pts = (json.loads((ROOT / "reports" / "usdt_dominance.json")
                          .read_text(encoding="utf-8")).get("points") or [])
        cur = pts[-1]["usdt_d"]
        if len(pts) > 7:
            lines.append(f"    USDT.D change: 7d {cur - pts[-8]['usdt_d']:+.2f}pp"
                         + (f" · 30d {cur - pts[-31]['usdt_d']:+.2f}pp" if len(pts) > 30 else "")
                         + f"  (as of {pts[-1]['date']})")
    except Exception:
        pass
    return "\n".join(lines)


def _majors_block() -> str:
    """BTC/ETH/SOL daily structure — the market's spine. Plus ETH/BTC, the
    cleanest single tell for whether money is rotating out of BTC into alts."""
    out = []
    for coin in MARKET_MAJORS:
        d = evaluate_daily(coin)
        if not d:
            out.append(f"- {coin} 1D: no data")
            continue
        out.append(
            f"- {coin} 1D: ${d['price']:.4f} · RSI {d['rsi']:.0f} · TRAMA trend {d['trend']} "
            f"(slope {d['trama_slope'] or 0:+.1f}%) · vs 50MA {d['vs_ma50']:+.1f}% · "
            f"vs 200MA {d['vs_ma200']:+.1f}% · from 90d high {d['from_hi90']:+.1f}% · "
            f"7d {d['chg7']:+.1f}% · 30d {d['chg30']:+.1f}% · rvol {d['rvol']}x")
        out.append(f"    {coin} structure: {d['breakout']}")
        out.append(f"    {coin} money flow: {d['accdist']['state']}")
    try:
        import ratio_watch as rw
        ed = rw.evaluate_ratio("ETH/BTC", "1d")
        if ed:
            out.append(
                f"- ETH/BTC 1D (alt-rotation proxy — rising = money moving out of BTC into "
                f"alts): {rw.disp(ed['price'], ed['unit'])} · trend {ed['trend']} · "
                f"{'above' if ed['above_trama'] else 'below'} its TRAMA · "
                f"1d {ed['chg1']:+.1f}% · 7d {ed['chg7']:+.1f}%")
    except Exception:
        pass
    return "\n".join(out)


def build_market_review_prompt(br: dict, macro: dict | None, regime: dict | None) -> str:
    """Board-wide regime prompt. Market data only — no keys, tokens or credentials."""
    def names(lst, k=10):
        return ", ".join(lst[:k]) + (f" +{len(lst) - k} more" if len(lst) > k else "") if lst else "none"

    def movers(rows):
        return " · ".join(f"{r['coin']} {r['chg7']:+.0f}%" for r in rows) or "none"

    btc = br.get("btc")
    data = "\n".join([
        f"DATA — board-wide read across {br['n']} liquid coins, daily candles "
        f"({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}). "
        f"Use ONLY these numbers, never invent any:",
        "",
        "MAJORS:",
        _majors_block(),
        "",
        "ROTATION:",
        _usdtd_block(),
        f"- alts outperforming BTC: {br['beat_btc7']}% over 7d · {br['beat_btc30']}% over 30d",
        "",
        "BREADTH (the check on whether a move is board-wide or just a few names):",
        f"- trend: {br['up']}% of coins in a daily TRAMA uptrend, {br['down']}% in a downtrend",
        f"- {br['above_ma50']}% above their 50D MA · {br['above_ma200']}% above their 200D MA "
        f"({br['ma200_n']} coins have 200d of history)",
        f"- {br['green1']}% green on the day · median 7d {br['med7']:+.1f}% · "
        f"median 30d {br['med30']:+.1f}%",
        "",
        "RANGE BREAKS (last close vs the range as it stood before today):",
        f"- clearing their {MARKET_STRUCT_N}-day range HIGH ({len(br['brk55_up'])} coins): "
        f"{names(br['brk55_up'])}",
        f"- losing their {MARKET_STRUCT_N}-day range LOW ({len(br['brk55_dn'])} coins): "
        f"{names(br['brk55_dn'])}",
        f"- clearing their {MARKET_RANGE_N}-day range high ({len(br['brk20_up'])} coins): "
        f"{names(br['brk20_up'])}",
        f"- losing their {MARKET_RANGE_N}-day range low ({len(br['brk20_dn'])} coins): "
        f"{names(br['brk20_dn'])}",
        "",
        f"LEADERS (7d): {movers(br['leaders'])}",
        f"LAGGARDS (7d): {movers(br['laggards'])}",
        "",
        "CONTEXT:",
        f"- {_macro_tag(macro) or 'macro: no read cached'}",
        f"- bot market regime: {(regime or {}).get('regime', 'unknown')}",
        f"- BTC 7d {btc['chg7']:+.1f}% / 30d {btc['chg30']:+.1f}%" if btc else "- BTC: no data",
    ])
    return (
        "You are a crypto trading assistant replying to an admin's `/review market` command on "
        "Telegram. Give a board-wide read of the market — NOT a single coin. Markdown, in this "
        "structure:\n\n"
        "*MARKET — <REGIME> · <one line: the honest bottom line>*\n\n"
        "Pick <REGIME> as EXACTLY one of these labels (no others, no hedging between two):\n"
        "🚀 BULL BREAKOUT (uptrend expanding out of a range, breadth confirming)\n"
        "🟢 BULL TREND (uptrend intact, no fresh expansion)\n"
        "🟡 BULL PULLBACK (uptrend, but currently correcting)\n"
        "⚪ RANGE / CHOP (no directional edge)\n"
        "🟠 BEARISH RALLY (a bounce inside a downtrend — the trap case: price up, breadth and "
        "structure still broken)\n"
        "🔴 BEAR TREND (downtrend intact)\n"
        "🩸 BEAR BREAKDOWN (downtrend expanding, breadth confirming)\n\n"
        "*Why* — 3-5 bullets: BTC's own structure, breadth (does it confirm or contradict the "
        "price move?), USDT.D rotation, ETH/BTC, and macro. Say plainly where they disagree.\n\n"
        "*Alt rotation* — 2-3 bullets: is this a genuine board-wide range break or a narrow "
        "few-name move? Cite the breadth and range-break counts. Name the leaders actually "
        "clearing structure.\n\n"
        "*How to plan it*\n"
        "• Positioning now: add risk / hold / trim / stand aside — and roughly what size\n"
        "• What CONFIRMS the regime (a concrete level or breadth threshold to see next)\n"
        "• What INVALIDATES it (a concrete BTC level and a concrete USDT.D level)\n"
        "• What to avoid right now\n\n"
        "Rules: the regime label must follow the DATA, not the loudest number — a rally on weak "
        "breadth with USDT.D rising is a BEARISH RALLY, and a small number of coins breaking "
        "range is NOT a breakout. Remember USDT.D is INVERSE to crypto. Use ONLY the numbers in "
        "DATA. Be direct and honest; decision support, not financial advice. End with one italic "
        "caveat line. Keep under ~2200 characters.\n\n"
        f"{data}"
    )


def review_market_cli() -> tuple:
    """On-demand board-wide market review for `--review-market`. Returns
    (exit_code, message). Shares the single-flight review lock so it cannot be
    stacked on top of another review; runs on the subscription (never billed)."""
    lock = STATE_FILE.parent / "review.lock"
    if lock.exists() and (time.time() - lock.stat().st_mtime) < 240:
        return 3, "⏳ A review is already running — try again in a moment."
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(str(time.time()))
        coins = sorted({*DEFAULT_COINS, *MARKET_MAJORS})
        br = market_breadth(coins)
        if not br["n"] or not br.get("btc"):
            return 4, "⚠️ Couldn't load enough daily candles for a market read — try again shortly."
        macro = read_macro()
        _, regime = read_intelligence()
        text = _run_claude(build_market_review_prompt(br, macro, regime))
        return (0, text) if text else (5, "⚠️ Market review unavailable right now (subscription/CLI).")
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


# ── Buy / Sell decision support (--decide <coin> <buy|sell> <userId>) ──────
def build_decide_prompt(coin: str, side: str, ev: dict, daily: dict | None,
                        signals: list, macro: dict | None, ratio_block: str | None,
                        chart: dict | None = None) -> str:
    """Assemble every read into one DATA block, then have Claude weigh it into a
    buy/sell/wait call framed by the requested `side` (buy or sell)."""
    ad1 = ev.get("accdist") or {}
    doji = _doji_desc([ev]) or "none"
    chart = chart or {"patterns": [], "zones": []}

    def sig_line(s):
        wr = s.get("win_rate")
        wr_s = f"{wr:.0f}% win-rate" if isinstance(wr, (int, float)) and wr >= 0 else "win-rate n/a"
        tgt = (s.get("targets") or [None])[0]
        return (f"{s['tf']} {s.get('strategy', '?')} {str(s.get('direction', '?')).upper()}"
                f"{' STRONG' if s.get('strong') else ''} — {wr_s}, "
                f"entry ${s.get('entry')} stop ${s.get('stop')} t1 ${tgt}, {s.get('age_h', '?')}h ago"
                f"{' [order placed]' if s.get('placed') else ''}")

    def pat_line(p):
        mag = f", {p['magnitude_pct']:+.0f}% move" if p.get("magnitude_pct") is not None else ""
        tgt = f" → target ${p['target']:g}" if p.get("target") is not None else ""
        inval = f", invalidation ${p['invalidation']:g}" if p.get("invalidation") is not None else ""
        return (f"{p['tf']} {p.get('type', '?')} ({p.get('direction', '?')}, "
                f"{str(p.get('status', '?')).upper()}){tgt}{inval}{mag}"
                + (f" — {p['summary']}" if p.get("summary") else ""))

    sig_block = "\n".join(f"  - {sig_line(s)}" for s in signals) if signals else "  - none in the last 48h"
    pat_block = ("\n".join(f"  - {pat_line(p)}" for p in chart["patterns"])
                 if chart["patterns"] else "  - none detected")
    zone_block = ""
    if chart["zones"]:
        zone_block = "\n- accumulation/distribution zones (frontend detector):\n" + "\n".join(
            f"  - {z['tf']} {z.get('type', '?')} {z.get('label', '')} "
            f"${z.get('low')}–${z.get('high')} (strength {z.get('strength')})" for z in chart["zones"])

    data = ["DATA (use ONLY these numbers, never invent):",
            f"- price: ${ev['mark']:.4f}  ·  24h {ev['chg_24h']:+.1f}%  ·  "
            f"from 72h high {ev['from_high_pct']:+.1f}% / low {ev['from_low_pct']:+.1f}%",
            f"- chart patterns (triangles/wedges/etc., frontend detector):\n{pat_block}"
            + zone_block,
            f"- bot strategy signals (breakout200/S-R/etc.):\n{sig_block}",
            f"- candle: doji = {doji}",
            f"- S/R breakout (1h): {ev.get('breakout', '?')}",
            f"- accumulation/distribution (1h): {ad1.get('state', '?')} "
            f"(A/D slope {ad1.get('ad_slope')}, {ad1.get('divergence') or 'no divergence'})",
            f"- RSI(1h): {ev['rsi_now']:.0f}  ·  funding: {ev['funding_ann']:+.0f}%/yr "
            f"({_funding_zone(ev['funding_ann'])})",
            f"- TRAMA trend (1h): {ev.get('trend', '?')} (slope {ev.get('trama_slope', 0):+.1f}%)",
            f"- exhaustion direction (1h): {ev.get('direction') or 'none'} "
            f"[signals: {[k for k, _ in ev['signals']] or 'none'}]"]
    if daily:
        adD = daily.get("accdist") or {}
        data += [
            f"- DAILY: RSI {daily['rsi']:.0f} · trend {daily.get('trend', '?')} · "
            f"vs50D {daily['vs_ma50']:+.1f}% · vs200D {daily['vs_ma200']:+.1f}% · "
            f"7d {daily['chg7']:+.1f}% · 30d {daily['chg30']:+.1f}%",
            f"- DAILY S/R breakout: {daily.get('breakout', '?')}",
            f"- DAILY accumulation/distribution: {adD.get('state', '?')} "
            f"({adD.get('divergence') or 'no divergence'})",
        ]
    if ratio_block:
        data.append(ratio_block)
    if macro:
        data.append(f"- macro regime: {macro['regime']} ({macro['strength']}) — "
                    f"10Y {macro['yield_10y']}% {macro['yield_dir']}, DXY {macro['dxy']} {macro['dxy_dir']}")
    usdtd = read_usdt_dominance()
    if usdtd:
        data.append(f"- USDT dominance: {usdtd['usdt_d']}% {usdtd['state']} ({usdtd['trend']}) "
                    f"— {usdtd['crypto']} [USDT.D is INVERSE to crypto: falling/rejecting-resistance "
                    f"= bullish for crypto, rising/breaking-resistance = bearish]")
    data_block = "\n".join(data)

    verb = "BUY (go long)" if side == "buy" else "SELL / SHORT (go short)"
    return (
        f"You are a crypto trading assistant answering an admin's /{side} {coin} command on "
        f"Telegram — decision support for whether to {verb} {coin} right now. Weigh ALL the "
        f"evidence below (chart patterns + their measured-move targets, bot strategy signals, "
        f"candle/doji, S/R breakout, accumulation vs distribution across timeframes, RSI, "
        f"funding, TRAMA trend, exhaustion, vs-BTC, macro) and give an HONEST call — if the "
        f"evidence contradicts the {side}, say so plainly.\n\n"
        f"Reply in Markdown, tight, this shape:\n"
        f"*{'🟢' if side == 'buy' else '🔴'} {side.upper()}? {coin} @ $PRICE — LEAN <BUY|SELL|WAIT> "
        f"(<one-line why>)*\n"
        f"*Signals in play* — list any active chart patterns (type, status, and their TARGET) "
        f"and bot strategy signals; write 'none' only if truly empty.\n"
        f"*Evidence* — 3-5 bullets: what agrees with the {side}, what argues against it "
        f"(patterns, doji, S/R breakout, accum/distribution incl. any divergence, "
        f"momentum/funding, higher-timeframe + macro).\n"
        f"*How to play it* — concrete: entry/trigger, stop, first target (use pattern targets / "
        f"levels from DATA); and what would flip the call.\n\n"
        f"End with one italic caveat. Decision support, not advice — odds, not certainty. "
        f"Keep under ~1500 characters.\n\n"
        f"{data_block}"
    )


def decide_cli(coin_raw: str, side_raw: str, user_id: str = "") -> tuple:
    """On-demand buy/sell decision support for `--decide <COIN> <buy|sell> [userId]`."""
    coin = (coin_raw or "").strip().upper()
    side = (side_raw or "buy").strip().lower()
    if side not in ("buy", "sell"):
        side = "buy"
    if not re.fullmatch(r"[A-Z0-9]{1,15}", coin):
        return 2, "⚠️ Invalid coin — e.g. `/buy ETH` or `/sell SOL`."
    lock = STATE_FILE.parent / "review.lock"
    if lock.exists() and (time.time() - lock.stat().st_mtime) < 120:
        return 3, "⏳ A review is already running — try again in a moment."
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(str(time.time()))
        HL.prefetch()
        BINANCE.prefetch()
        evs = {}
        for src in (HL, BINANCE):
            try:
                if coin in src.universe():
                    ev = evaluate(coin, src)
                    if ev:
                        evs[src.name] = ev
            except Exception:
                pass
        if not evs:
            return 4, f"❓ *{coin}* isn't tradable on Hyperliquid or Binance."
        ev0 = evs.get("HL") or next(iter(evs.values()))
        daily = evaluate_daily(coin)
        signals = read_pattern_signals(coin, user_id)
        chart = read_chart_patterns(coin)
        ratio_block = _btc_ratio_block(coin)
        macro = read_macro()
        text = _run_claude(build_decide_prompt(coin, side, ev0, daily, signals, macro,
                                               ratio_block, chart))
        return (0, text) if text else (5, "⚠️ Decision support unavailable right now (subscription/CLI).")
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


# ── Main ────────────────────────────────────────────────────────────────
def parse_coins() -> list:
    if len(sys.argv) > 1:
        raw = sys.argv[1]
    else:
        raw = os.getenv("COIN", ",".join(DEFAULT_COINS))
    return [c.strip().upper() for c in raw.split(",") if c.strip()]


def _log_venue(coin: str, ev: dict) -> None:
    flags = ", ".join(k for k, _ in ev["signals"]) or "none"
    print(f"    {ev['source']:8}: 24h {ev['chg_24h']:+.1f}%  RSI {ev['rsi_now']:.0f}  "
          f"fund {ev['funding_ann']:+.0f}%/yr  dir={ev['direction'] or 'chop'}  "
          f"signals={len(ev['signals'])} [{flags}]")


def main() -> None:
    print("\n" + "=" * 60)
    print("  EXHAUSTION WATCH — reversal early warning (top + bottom)")
    print("  " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 60)

    coins = parse_coins()
    state = _load_state()
    changed = False
    fb_payload = {}                    # per-coin gate state, published in one write
    decisions = {}                     # per-coin decision (S/R + exhaustion + bias)
    price_map = {}                     # coin -> {mark, hi, lo} for the setup tracker
    reviews_this_run = 0               # cap event-driven Claude reviews per run

    # The bot's trend bias / market regime, read once for all coins.
    bias_map, _regime = read_intelligence()
    macro_tag = _macro_tag(read_macro())          # macro regime, same for every coin this run

    # Reset per-run rate-limit tally.
    for v in _RL.values():
        v["retries"] = v["failures"] = 0

    # Load each venue's whole-market snapshot ONCE (mark/funding/prevDay for all
    # coins) so per-coin work is just candle calls. This is what lets the list
    # scale to dozens of coins without hammering the market-wide endpoints.
    avail = {}
    for src in (HL, BINANCE):
        try:
            src.prefetch()
            avail[src.name] = src.universe()
        except Exception as e:
            print(f"  [WARN] {src.name} snapshot fetch failed: {e}")
            avail[src.name] = set()

    for coin in coins:
        on_hl = coin in avail.get("HL", set())
        on_bn = coin in avail.get("Binance", set())
        if not on_hl and not on_bn:
            print(f"  {coin}: not listed on HL or Binance — skip")
            continue

        # Evaluate every venue the coin trades on.
        evs = {}
        for src in (HL, BINANCE):
            if coin not in avail.get(src.name, set()):
                continue
            try:
                ev = evaluate(coin, src)
            except Exception as e:
                print(f"  {coin}: {src.name} evaluation failed: {e}")
                continue
            if ev is not None:
                evs[src.name] = ev

        print(f"  {coin}:")
        for ev in evs.values():
            _log_venue(coin, ev)
        if not evs:
            # Listed on a venue but no venue returned usable data (e.g. a coin
            # mid-delisting/migration with an empty candle history).
            print("    (no usable data from any venue — skip)")
            continue

        # Record the gate state for the bot BEFORE the alert-cooldown branches
        # below can `continue` — the gate needs current state every run, incl.
        # "none", so a stale exhaustion never lingers.
        fb_payload[coin] = gate_state(evs, on_hl, on_bn)

        # Full decision (S/R + exhaustion + bias) — published every run for the
        # bot gate / digest. The tradeable-setup alert below is derived from the
        # same inputs, so the ping and the review stay consistent.
        decisions[coin] = build_decision(evs, on_hl, on_bn, bias_map.get(coin.lower()))

        # Current price + recent bar range — for the setup outcome tracker.
        prim_ev = evs.get("HL") or evs.get("Binance")
        price_map[coin] = {"mark": prim_ev["mark"],
                           "hi": prim_ev["recent_hi"], "lo": prim_ev["recent_lo"]}

        # ── Confluence gate ──────────────────────────────────────────────
        if on_hl and on_bn:
            # Strict AND: both venues must have evaluated, agree on direction,
            # and each independently clear MIN_SIGNALS.
            if "HL" not in evs or "Binance" not in evs:
                print("    (a venue was unavailable this run — no confluence, skip)")
                continue
            a, b = evs["HL"], evs["Binance"]
            if not (a["direction"] and a["direction"] == b["direction"]
                    and len(a["signals"]) >= MIN_SIGNALS
                    and len(b["signals"]) >= MIN_SIGNALS):
                print("    (no 2-venue confluence — skip)")
                continue
            direction = a["direction"]
            n = len(a["signals"]) + len(b["signals"])
            mode, alert_venues = "confluence", {"HL": a, "Binance": b}
        else:
            # Single-venue coin (e.g. TLM/VET on Binance only) — no partner to
            # confirm with, so it stands on its own evaluation.
            ev = next(iter(evs.values()))
            if not ev["direction"] or len(ev["signals"]) < MIN_SIGNALS:
                continue
            direction = ev["direction"]
            n = len(ev["signals"])
            mode, alert_venues = "single", {ev["source"]: ev}

        # ── Tradeable-setup gate ─────────────────────────────────────────
        # An exhaustion SIGNAL only becomes an ALERT if it's a tradeable SETUP —
        # at a strong level, not fighting a strong opposing bias, acceptable R:R.
        # This is what makes the alert agree with the review (both check the same
        # things), and it suppresses the mid-air / parabola-topping false pings.
        ev0 = alert_venues.get("HL") or next(iter(alert_venues.values()))
        bias_state = (bias_map.get(coin.lower()) or {}).get("bias")
        tradeable, conviction, plan = assess_exhaustion_trade(
            direction, ev0, bias_map.get(coin.lower()))
        if not tradeable:
            print(f"    (exhaustion but not a tradeable setup — suppressed: "
                  f"at_level={plan['at_level']} opposing_bias={plan['opposing']} "
                  f"R:R={plan['rr']}<{MIN_RR} trend={plan['trend']} "
                  f"steep_against={plan['steep_against']}"
                  f"{'' if plan['confirmed'] else ' unconfirmed'})")
            continue

        if not should_alert(coin, direction, n, state):
            print(f"    (within {COOLDOWN_HOURS}h cooldown, no escalation — skip)")
            continue

        ratio_tag = _btc_ratio_tag(coin)          # relative strength vs BTC
        if send_telegram_private(format_alert(coin, direction, mode, alert_venues,
                                              plan, conviction, bias_state, ratio_tag, macro_tag)):
            state[coin] = {"ts": time.time(), "direction": direction, "n_signals": n,
                           "mode": mode, "conviction": conviction,
                           "at": datetime.now(timezone.utc).isoformat()}
            changed = True
            print(f"    ✅ {direction.upper()} {mode} alert sent "
                  f"({conviction} conviction, R:R {plan['rr']}).")

            # Auto-review ONLY high-conviction setups — so a review never
            # contradicts a low-quality ping (that was the whole problem).
            if conviction != "high":
                print(f"    🔍 review skipped (conviction {conviction}, not high).")
            elif not REVIEW_ENABLED:
                pass
            elif not review_cooldown_ok(coin, state):
                print(f"    🔍 review skipped ({coin} in {REVIEW_COOLDOWN_HOURS:.0f}h cooldown).")
            elif not review_budget_ok(state, reviews_this_run):
                print("    🔍 review skipped (per-run or daily cap reached).")
            else:
                review = claude_review(
                    build_review_prompt(coin, direction, mode, ev0, decisions.get(coin, {})))
                if review and send_telegram_private(review):
                    reviews_this_run += 1
                    state["_reviews"]["count"] += 1
                    state["_reviews"].setdefault("last", {})[coin] = time.time()
                    changed = True
                    print(f"    🔍 review sent ({reviews_this_run}/{REVIEW_MAX_PER_RUN} run, "
                          f"{state['_reviews']['count']}/{REVIEW_DAILY_CAP} today).")
        else:
            print("    ⚠️  Telegram send failed — will retry next run.")

    if changed:
        _save_state(state)

    publish_to_firebase(fb_payload)
    publish_decisions(decisions)

    # Setup outcome tracking — ALWAYS runs; muting only silences Telegram, so we
    # keep building a performance record while the pushes are off.
    log_setups(decisions)
    tracked = update_setups(price_map)
    if tracked:
        n_open = sum(1 for e in tracked if e.get("status") == "open")
        print(f"  📓 Setup tracker: {n_open} open / {len(tracked)} logged "
              f"({'MUTED' if SIGNALS_MUTED else 'live'}).  `--setups` for win-rate.")

    # Hourly setup digest to the private chat(s) only. The cron runs at :00 and
    # :30, so gating on the top-of-hour run sends it once per hour. Stays quiet
    # when there are no medium+high leans to report (no heartbeat spam).
    if decisions and datetime.now().minute < 15:
        n_setups = sum(1 for d in decisions.values() if d.get("conviction") in ("high", "medium"))
        if not n_setups:
            print("  📋 No medium+high setups this hour — digest skipped.")
        elif SIGNALS_MUTED:
            print(f"  🔇 {n_setups} setups this hour — digest MUTED (tracking only).")
        else:
            # vs-BTC tag only for the coins the digest will show (hourly, small set).
            lean_coins = [c for c, d in decisions.items()
                          if d.get("conviction") in ("high", "medium")]
            ratio_map = {c: _btc_ratio_tag(c) for c in lean_coins}
            if send_signal(build_digest(decisions, read_cross_signals(), ratio_map)):
                print(f"  📋 Hourly setup digest sent to private chat ({n_setups} setups).")

    _report_rate_limits()


def _report_rate_limits() -> None:
    """Log a per-run rate-limit summary, and ping Telegram if it's excessive."""
    hl, bn = _RL["HL"], _RL["Binance"]
    retries = hl["retries"] + bn["retries"]
    failures = hl["failures"] + bn["failures"]
    print(f"\n  Rate-limit summary — retries: HL={hl['retries']} Binance={bn['retries']}"
          f"  |  hard failures (data dropped): {failures}")

    # Alert if we backed off a lot (approaching the ceiling) or actually dropped
    # data. A handful of retries that self-heal is normal and stays quiet.
    if retries >= RATE_LIMIT_ALERT_THRESHOLD or failures > 0:
        msg = "\n".join([
            "⏱️ *Exhaustion Watch — rate limits elevated*",
            f"HL 429 retries: {hl['retries']}  (dropped: {hl['failures']})",
            f"Binance 429/418 retries: {bn['retries']}  (dropped: {bn['failures']})",
            "",
            "_A run is being throttled. If this persists: trim coins, widen the "
            "cron interval, or raise HL_PACING_S in exhaustion_watch.py._",
        ])
        if send_telegram_private(msg):
            print("    ⚠️  rate-limit notification sent to Telegram.")


if __name__ == "__main__":
    # On-demand reviews (used by the admin /review Telegram command):
    #   --review <COIN>        → 1h exhaustion / S-R review
    #   --review-daily <COIN>  → daily-timeframe swing review
    #   --review-ratio NUM/DEN → alt/BTC ratio (relative-strength) review
    #   --review-market        → board-wide regime read (breadth + USDT.D + macro)
    #   --review-trama [1D|4H] → coins in a confirmed trend above / below TRAMA
    if len(sys.argv) > 1 and sys.argv[1] == "--review-trama":
        code, out = trama_board_cli(sys.argv[2] if len(sys.argv) > 2 else "")
        print(out)
        sys.exit(code)
    if len(sys.argv) > 1 and sys.argv[1] in ("--setups", "--performance"):
        print(format_setup_report())
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == "--macro":
        m = read_macro()
        print(_macro_tag(m) or "No macro read cached — run `python macro_conditions.py`.")
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == "--review-market":
        code, out = review_market_cli()
        print(out)
        sys.exit(code)
    if len(sys.argv) > 1 and sys.argv[1] in ("--review", "--review-daily", "--review-ratio"):
        arg = sys.argv[2] if len(sys.argv) > 2 else ""
        if sys.argv[1] == "--review-ratio":
            code, out = review_ratio_cli(arg)
        elif sys.argv[1] == "--review-daily":
            code, out = review_daily_cli(arg)
        else:
            code, out = review_coin_cli(arg)
        print(out)
        sys.exit(code)
    # --decide <COIN> <buy|sell> [userId]  → buy/sell decision support
    if len(sys.argv) > 1 and sys.argv[1] == "--decide":
        coin = sys.argv[2] if len(sys.argv) > 2 else ""
        side = sys.argv[3] if len(sys.argv) > 3 else "buy"
        uid = sys.argv[4] if len(sys.argv) > 4 else os.getenv("DECIDE_SIGNALS_USER", "")
        code, out = decide_cli(coin, side, uid)
        print(out)
        sys.exit(code)
    main()
