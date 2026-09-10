#!/usr/bin/env python3
"""
Discovery Scanner — unified buy / fade / momentum radar (Bitget-wide).
═════════════════════════════════════════════════════════════════════════════

Two lenses over the whole Bitget USDT-perp board, then one classifier:

  price lens  (gainers)  — sort by 24h %      → finds coins that already moved
  volume lens (RVOL)     — recent vol vs base → finds coins getting flow (early)

Classify each surviving coin:
  BUY       — price still flat/early AND volume ramping (coiling / accumulation),
              or a deep dump on a volume spike (capitulation)
  FADE      — big gainer BUT volume fading (the move is exhausting)
  MOMENTUM  — big gainer WITH rising volume (still fueled — don't fade)

Crypto only (base asset must trade on Binance/HL; gold/stables excluded), so the
buy/fade candidates can be CONFIRMED with the exhaustion engine (direction, RSI,
funding, signals). Publishes scan/{COIN} to Firebase and pings the private chat:
  • a NEW buy/fade candidate  → the discovery digest
  • a MOMENTUM BREAKOUT        → a dedicated "get in early" push — a strong gainer
    breaking out on high, rising RVOL with a confirmed TRAMA uptrend (e.g. the TUT
    launch), one ping per coin per cooldown. Dedup via reports/scanner_state.json.
  • an EARLY WARNING           → the same volume signature (high RVOL, volume still
    accelerating) fired BEFORE TRAMA confirms. Earlier than the breakout and less
    certain by construction — the ramp stands in for the confirmation that hasn't
    happened yet. The "catch the low" tier: pushes even while the digests are muted
    (it's the earliest entry signal); journaled under source="early".
  • a MOMENTUM BREAKDOWN (short) → the mirror: a loser breaking down on high, rising
    volume with TRAMA rolling over, NOT yet oversold or crowded-short (short the
    distribution break, not the capitulation). UNPROVEN — journaled under
    direction="short", alerts respect the digest mute until the log earns trust.
    View with `--breakdown` (Telegram /breakdown).

Every gainer bar is CAP-SCALED (see BREAKOUT_MIN_CHG): a breakout is +3% on a
large cap, +5% on a mid, +8% on a microcap. All caps are in scope — a fixed bar
either missed large-cap moves entirely (nothing between 4–8% was even classified)
or drowned in flat-price volume blips. Both tiers additionally require price to
be participating, not just volume: RVOL alone fired on BTC/ETH/SOL/DOGE sitting
at ±1% and every one of those peaked ≤1.03x. Outcomes are judged per tier too.

Usage:
  python discovery_scan.py                 # full scan
  python discovery_scan.py --scan          # (alias) full scan, same output
  python discovery_scan.py --breakouts     # full outcome journal (console)
  python discovery_scan.py --brief         # one line per coin (Telegram /breakout)
  python discovery_scan.py --engulf        # 1D+1W range breakouts (slow; writes cache)
  python discovery_scan.py --engulf 1D     # narrow to one timeframe
  python discovery_scan.py --engulf-brief  # cached sweep, instant (Telegram /engulf)
  python discovery_scan.py --breakdown     # short-side journal (Telegram /breakdown)
  pm2 start ecosystem.config.js --only discovery-scan
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

import exhaustion_watch as ew  # evaluate / HL / BINANCE / firebase / private telegram

BITGET = "https://api.bitget.com"
STATE_FILE = ROOT / "reports" / "scanner_state.json"
ENGULF_CACHE = ROOT / "reports" / "engulf_cache.json"   # cached range-breakout sweep
BREAKOUT_LOG = ROOT / "reports" / "breakout_log.json"   # momentum-breakout outcome journal
TRACK_DAYS = float(os.getenv("DISCOVERY_TRACK_DAYS", "30"))   # how long to follow a breakout's price

# Weekly breakout-performance summary (a report, not a signal — always sends).
PERF_SUMMARY_DAY = int(os.getenv("DISCOVERY_PERF_DAY", "6"))       # 0=Mon .. 6=Sun
PERF_SUMMARY_HOUR = int(os.getenv("DISCOVERY_PERF_HOUR", "12"))    # UTC hour
PERF_MATURE_DAYS = float(os.getenv("DISCOVERY_PERF_MATURE_DAYS", "7"))  # a breakout counts once this old

# ── Tunables ────────────────────────────────────────────────────────────
GAINER_TOP = 30              # candidates from the price lens
VOLUME_TOP = 70              # candidates from the volume lens (by 24h turnover)
GAIN_PCT = 8.0               # fade/dump bar — the gainer bar is cap-scaled, see BREAKOUT_MIN_CHG
COIL_MAX_CHG = 4.0           # a "coil" has price within ±this % (still early)
DUMP_PCT = -10.0             # a capitulation candidate is down at least this much
RVOL_BUY = 2.0               # volume must be this × baseline to count as picking up
RVOL_MOMENTUM = 1.5          # a gainer with vol ≥ this is still fueled (bar: BREAKOUT_MIN_CHG)
RVOL_FADE = 1.0              # a gainer with vol < this is fading (exhausting)
RVOL_DUMP = 2.5              # capitulation needs a real volume spike
CANDLE_LIMIT = 30            # 1h candles for RVOL

# Momentum-breakout alert — pings a strong gainer breaking out on real volume
# with a confirmed (TRAMA) uptrend (the "get in early" case, e.g. TUT on Aug 1).
# Stricter than the momentum CLASS so only genuine launches ping, not every pop.
MOM_ALERT_RVOL = float(os.getenv("DISCOVERY_MOM_RVOL", "3.0"))          # min RVOL for a breakout
MOM_ALERT_SLOPE = float(os.getenv("DISCOVERY_MOM_SLOPE", "1.5"))        # min TRAMA slope % (trend rising)
MOM_ALERT_MIN_TURNOVER = float(os.getenv("DISCOVERY_MOM_TURNOVER", "2000000"))  # min 24h USDT turnover (tradeability floor)
MOM_ALERT_COOLDOWN_H = float(os.getenv("DISCOVERY_MOM_COOLDOWN_H", "24"))       # 1 ping / coin / this many h

# Early-warning tier — fires on the volume signature alone, before TRAMA confirms
# the trend. Lower RVOL bar than the breakout (catch it sooner) but a HIGHER
# acceleration bar: with no trend confirmation to lean on, the volume has to be
# actively ramping, not merely elevated. Shares the breakout's turnover floor.
EARLY_RVOL = float(os.getenv("DISCOVERY_EARLY_RVOL", "2.5"))            # min RVOL for an early warning
EARLY_ACCEL = float(os.getenv("DISCOVERY_EARLY_ACCEL", "1.5"))          # min accel (last 3h vs prior 3h)
EARLY_COOLDOWN_H = float(os.getenv("DISCOVERY_EARLY_COOLDOWN_H", "12")) # 1 ping / coin / this many h

# Spot-vs-perp basis (Bitget) — a perp trading far from spot is a dislocation. A
# perp DISCOUNT (perp < spot) in a dump is a liquidation cascade / mean-reversion
# tell (SKR ran to -16%); a perp PREMIUM (perp > spot) on a rip is leverage/FOMO.
# Attached to every rec (context on the breakout), and fires its own ⚖️ alert past
# the threshold. Liquidity + sanity filters kill stale/delisted-spot false gaps.
BASIS_ALERT_PCT = float(os.getenv("DISCOVERY_BASIS_PCT", "4.0"))          # |basis %| to alert
BASIS_MIN_VOL = float(os.getenv("DISCOVERY_BASIS_MIN_VOL", "2000000"))    # both sides ≥ this 24h vol
BASIS_SANE_MAX = float(os.getenv("DISCOVERY_BASIS_SANE_MAX", "40"))       # above this = symbol/stale artifact
BASIS_COOLDOWN_H = float(os.getenv("DISCOVERY_BASIS_COOLDOWN_H", "6"))    # 1 ping / coin / this many h

# Short breakdown tier — the MIRROR of the breakout (a loser breaking down on high,
# rising volume with TRAMA rolling over), but with anti-capitulation guards: crypto
# has an upward drift + violent squeeze risk, and a volume-backed dump is often a
# capitulation LOW (a bounce/buy), not a short. So a breakdown must NOT already be
# oversold or crowded-short — short the distribution break, not the crash. Deep
# flushes (<= DUMP_PCT) stay 'buy' (capitulation bounce). UNPROVEN by design:
# journaled under direction="short", alerts respect SIGNALS_MUTED until the log
# earns trust; view on demand with `--breakdown`.
LOSER_TOP = int(os.getenv("DISCOVERY_LOSER_TOP", "30"))                       # loser lens size
BREAKDOWN_RSI_FLOOR = float(os.getenv("DISCOVERY_BREAKDOWN_RSI", "35"))       # don't short if already oversold
BREAKDOWN_FUNDING_FLOOR = float(os.getenv("DISCOVERY_BREAKDOWN_FUNDING", "-30"))  # nor if funding crowded-short

# Cap tiers by 24h turnover — the liquidity proxy for "how big is this coin".
# A breakout does not look the same at every size: BTC ripping is +3%, a microcap
# ripping is +15%. One fixed % bar either drowns in large-cap noise or never sees
# a large-cap move at all, so the bar scales with the tier instead.
CAP_LARGE_TURNOVER = float(os.getenv("DISCOVERY_CAP_LARGE", "100000000"))   # ≥$100M 24h
CAP_MID_TURNOVER = float(os.getenv("DISCOVERY_CAP_MID", "20000000"))        # ≥$20M 24h

# Minimum 24h move to count as "breaking out", per tier. Derived from the journal:
# every early-tier firing with a flat price (BTC -0.6%, ETH -0.4%, DOGE +0.1%,
# TAO -3.5% …) peaked at 1.00–1.03x, while every one that ran had already moved.
# Volume without price is churn — this is the filter that says so.
BREAKOUT_MIN_CHG = {
    "large": float(os.getenv("DISCOVERY_MIN_CHG_LARGE", "3.0")),
    "mid":   float(os.getenv("DISCOVERY_MIN_CHG_MID", "5.0")),
    "small": float(os.getenv("DISCOVERY_MIN_CHG_SMALL", "8.0")),
}

# What "it worked" means, per tier — the same scaling problem on the outcome side.
# Judging BTC against a microcap's 1.5x would mark every large cap 🔴 faded no
# matter how well the signal called it, poisoning the hit-rate stats.
WORKED_MULT = {"large": 1.10, "mid": 1.25, "small": 1.50}
MILD_MULT = {"large": 1.05, "mid": 1.12, "small": 1.20}


def cap_tier(turnover: float) -> str:
    """large / mid / small from 24h USDT turnover."""
    if turnover >= CAP_LARGE_TURNOVER:
        return "large"
    return "mid" if turnover >= CAP_MID_TURNOVER else "small"


def breakout_min_chg(turnover: float) -> float:
    """Minimum 24h % move for this coin's tier to count as breaking out."""
    return BREAKOUT_MIN_CHG[cap_tier(turnover)]


# Non-crypto that still lists on Binance — gold tokens, stables, FX.
EXCLUDE = {"PAXG", "XAUT", "XAU", "XAG", "USDC", "FDUSD", "TUSD", "DAI",
           "USDE", "USD1", "AEUR", "EURI", "EUR", "BUSD"}


# ── Bitget data ─────────────────────────────────────────────────────────
def bitget_tickers() -> list:
    r = requests.get(f"{BITGET}/api/v2/mix/market/tickers",
                     params={"productType": "USDT-FUTURES"}, timeout=15)
    r.raise_for_status()
    return r.json()["data"]


def bitget_spot_prices() -> dict:
    """{base: (last_price, usdt_volume)} from the Bitget spot board. {} on failure."""
    try:
        r = requests.get(f"{BITGET}/api/v2/spot/market/tickers", timeout=15)
        r.raise_for_status()
        out = {}
        for d in r.json().get("data", []):
            sym = d.get("symbol", "")
            base = sym[:-4] if sym.endswith("USDT") else sym
            out[base] = (float(d.get("lastPr", 0) or 0), float(d.get("usdtVolume", 0) or 0))
        return out
    except Exception:
        return {}


def compute_basis_map(perp_tickers: list, spot_map: dict) -> dict:
    """coin -> {basis, perp, spot, perp_vol, spot_vol, kind} for every coin on BOTH
    the Bitget perp + spot boards. basis = (perp-spot)/spot %. kind = discount|premium."""
    out = {}
    for d in perp_tickers:
        sym = d.get("symbol", "")
        base = sym[:-4] if sym.endswith("USDT") else sym
        sp = spot_map.get(base)
        if not sp:
            continue
        perp = float(d.get("lastPr", 0) or 0)
        spot, spot_vol = sp
        if perp <= 0 or spot <= 0:
            continue
        basis = (perp - spot) / spot * 100
        out[base] = {"basis": round(basis, 2), "perp": perp, "spot": spot,
                     "perp_vol": float(d.get("usdtVolume", 0) or 0), "spot_vol": spot_vol,
                     "kind": "discount" if basis < 0 else "premium"}
    return out


def _basis_dislocated(b: dict | None) -> bool:
    """Does this basis clear the alert bar — big enough, liquid, and not an artifact?"""
    return bool(b and abs(b["basis"]) >= BASIS_ALERT_PCT and abs(b["basis"]) < BASIS_SANE_MAX
                and b["perp_vol"] >= BASIS_MIN_VOL and b["spot_vol"] >= BASIS_MIN_VOL)


def bitget_rvol(symbol: str) -> tuple | None:
    """(rvol, accel) from 1h candles: last 3h avg vs prior 24h avg / prior 3h."""
    try:
        r = requests.get(f"{BITGET}/api/v2/mix/market/candles",
                         params={"symbol": symbol, "productType": "USDT-FUTURES",
                                 "granularity": "1H", "limit": CANDLE_LIMIT}, timeout=10)
        r.raise_for_status()
        qv = [float(c[6]) for c in r.json()["data"]]   # hourly quote (USDT) volume
        if len(qv) < 28:
            return None
        recent = sum(qv[-3:]) / 3
        rvol = recent / (sum(qv[-27:-3]) / 24 or 1)
        accel = recent / (sum(qv[-6:-3]) / 3 or 1)
        return round(rvol, 2), round(accel, 2)
    except Exception:
        return None


# ── Range-engulfing breakout (structural, higher-timeframe lens) ─────────
# A different question from the RVOL tiers: not "is flow arriving right now"
# but "did a candle just swallow the whole consolidation it was stuck in".
# Runs on CLOSED candles only — the in-progress bar's range and volume are
# still forming, so judging it would fire early and then un-fire.
ENGULF_TF = os.getenv("DISCOVERY_ENGULF_TF", "1W")                        # default single-TF granularity
ENGULF_TFS = [t.strip() for t in os.getenv("DISCOVERY_ENGULF_TFS", "1D,1W").split(",") if t.strip()]
# Lookback per timeframe. 20 daily bars ≈ a month — the classic Donchian range,
# and long enough that a routine 3-day pullback-and-pop doesn't count as breaking
# "the range". 10 weekly bars ≈ a quarter. Both are ranges a trader would draw.
ENGULF_LOOKBACKS = {"1D": int(os.getenv("DISCOVERY_ENGULF_LOOKBACK_1D", "20")),
                    "1W": int(os.getenv("DISCOVERY_ENGULF_LOOKBACK_1W", "10"))}
ENGULF_LOOKBACK = int(os.getenv("DISCOVERY_ENGULF_LOOKBACK", "10"))       # fallback for other TFs
ENGULF_VOL_MULT = float(os.getenv("DISCOVERY_ENGULF_VOL", "1.5"))         # vol vs the lookback baseline
ENGULF_MIN_TURNOVER = float(os.getenv("DISCOVERY_ENGULF_TURNOVER", "200000"))  # 24h tradeability floor
ENGULF_WORKERS = int(os.getenv("DISCOVERY_ENGULF_WORKERS", "8"))          # parallel candle fetches


def bitget_candles(symbol: str, granularity: str, limit: int) -> list | None:
    """Raw Bitget candles, oldest→newest. Element -1 is the IN-PROGRESS bar."""
    try:
        r = requests.get(f"{BITGET}/api/v2/mix/market/candles",
                         params={"symbol": symbol, "productType": "USDT-FUTURES",
                                 "granularity": granularity, "limit": limit}, timeout=10)
        r.raise_for_status()
        return r.json().get("data") or None
    except Exception:
        return None


def engulf_lookback(tf: str) -> int:
    """Bars a candle must clear on this timeframe."""
    return ENGULF_LOOKBACKS.get(tf, ENGULF_LOOKBACK)


def engulfing_breakout(symbol: str, tf: str = ENGULF_TF,
                       lookback: int | None = None) -> dict | None:
    """Did the last CLOSED candle break the whole prior range on rising volume?

    Three things have to line up, in increasing order of strictness:
      broke   — its high cleared every high in the lookback window
      closed_through — it CLOSED above them too (a wick through is a failed
                       breakout; the close is what makes it structural)
      engulf  — its low also undercut the prior bar's low, so the bar's range
                fully contains the one before it (a true outside bar)

    Volume is measured against the MEDIAN of the window, not the mean: one
    historic blow-off bar (POPCAT had an 18.5M week) drags a mean so far up
    that a genuine 2x expansion scores below 1.0 and the signal is missed.
    """
    lookback = engulf_lookback(tf) if lookback is None else lookback
    c = bitget_candles(symbol, tf, lookback + 4)
    if not c or len(c) < lookback + 2:
        return None
    closed, prior = c[-2], c[-2 - lookback:-2]
    if len(prior) < lookback:
        return None
    o, h, l, cl = (float(closed[1]), float(closed[2]), float(closed[3]), float(closed[4]))
    vol = float(closed[6])
    if cl <= o:                                  # red bar — not a breakout
        return None
    prior_high = max(float(x[2]) for x in prior)
    prior_vols = sorted(float(x[6]) for x in prior)
    n = len(prior_vols)
    med_vol = (prior_vols[n // 2] if n % 2 else (prior_vols[n // 2 - 1] + prior_vols[n // 2]) / 2)
    vol_mult = vol / (med_vol or 1)
    if h <= prior_high or vol_mult < ENGULF_VOL_MULT:
        return None
    return {
        "tf": tf, "lookback": lookback,
        "open": o, "high": h, "low": l, "close": cl,
        "chg": round((cl / o - 1) * 100, 1),
        "prior_high": prior_high,
        "closed_through": cl > prior_high,        # close beat the range, not just the wick
        "engulf": l <= float(prior[-1][3]) and cl > float(prior[-1][2]),
        "vol": vol, "med_vol": med_vol, "vol_mult": round(vol_mult, 2),
        "close_ts": int(closed[0]),
    }


def engulfing_breakout_multi(symbol: str, tfs: list | None = None) -> dict:
    """Run the range test on several timeframes. Returns {tf: signal} for the
    ones that fired — a daily break and a weekly break are different claims
    (a month's range vs a quarter's), and a coin doing both is the strongest
    version, so they're kept separate rather than collapsed to one boolean."""
    out = {}
    for tf in (tfs or ENGULF_TFS):
        sig = engulfing_breakout(symbol, tf)
        if sig:
            out[tf] = sig
    return out


def engulf_confirmed_tfs(multi: dict) -> list:
    """Timeframes where the candle CLOSED through the range (not just wicked)."""
    return [tf for tf, s in (multi or {}).items() if s["closed_through"]]


def scan_engulfing(tfs: list | str | None = None, tickers: list | None = None,
                   verbose: bool = False, workers: int = ENGULF_WORKERS) -> list:
    """Board-wide sweep for range-engulfing breakouts on the last closed bar.

    One candle request per symbol PER TIMEFRAME, so scanning 1D+1W over the whole
    board is ~2x the requests of a single-TF pass. Run on demand or once a day,
    never on the 30-minute cron — a daily signal changes once a day and a weekly
    one once a week. A small thread pool keeps it to a few minutes; Bitget's
    public candle endpoint tolerates this level of concurrency comfortably.

    Returns one entry per COIN with the timeframes it fired on, so a coin that
    broke both its monthly and quarterly range appears once, marked as both.
    """
    tfs = [tfs] if isinstance(tfs, str) else (tfs or ENGULF_TFS)
    tickers = tickers if tickers is not None else bitget_tickers()
    crypto = ew.BINANCE.universe() | ew.HL.universe()
    cands = []
    for d in tickers:
        sym = d["symbol"]
        base = sym[:-4] if sym.endswith("USDT") else sym
        if (float(d.get("usdtVolume", 0) or 0) >= ENGULF_MIN_TURNOVER
                and base not in EXCLUDE and base in crypto):
            cands.append((sym, base, d))
    if verbose:
        print(f"  {len(cands)} symbol(s) × {len(tfs)} timeframe(s) = "
              f"{len(cands) * len(tfs)} candle requests, {workers} workers…")

    def probe(item):
        sym, base, d = item
        multi = engulfing_breakout_multi(sym, tfs)
        if not multi:
            return None
        return {"coin": base, "symbol": sym, "tfs": multi,
                "price": float(d.get("lastPr", 0) or 0),
                "turnover": float(d.get("usdtVolume", 0) or 0)}

    hits = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for res in pool.map(probe, cands):
            if res:
                hits.append(res)
                if verbose:
                    marks = "+".join(sorted(res["tfs"]))
                    print(f"    ✓ {res['coin']} [{marks}]")
    # Strongest first: most timeframes confirmed, then most timeframes fired,
    # then the biggest volume expansion across them.
    def rank(h):
        conf = len(engulf_confirmed_tfs(h["tfs"]))
        best_vol = max(s["vol_mult"] for s in h["tfs"].values())
        return (-conf, -len(h["tfs"]), -best_vol)
    hits.sort(key=rank)
    return hits


def save_engulf_cache(hits: list, tfs: list) -> None:
    """Persist a sweep so Telegram can answer instantly. The sweep takes minutes
    (one candle request per symbol per timeframe); a bot command cannot block on
    that, and the underlying signal only changes when a daily/weekly candle
    closes — so the scan is a scheduled job and /engulf is a cache read."""
    ENGULF_CACHE.parent.mkdir(parents=True, exist_ok=True)
    ENGULF_CACHE.write_text(json.dumps(
        {"ts": int(time.time()), "tfs": tfs, "hits": hits}, indent=2), encoding="utf-8")


def load_engulf_cache() -> dict | None:
    if not ENGULF_CACHE.exists():
        return None
    try:
        return json.loads(ENGULF_CACHE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def format_engulfing_brief() -> str:
    """One line per coin from the cached sweep — sized for a Telegram reply.
    Reports the cache age so a stale answer is never mistaken for a live one."""
    cache = load_engulf_cache()
    if not cache:
        return ("No range-breakout scan cached yet.\n"
                "Run `python discovery_scan.py --engulf` to build one.")
    hits, tfs = cache.get("hits") or [], cache.get("tfs") or ENGULF_TFS
    age_h = (time.time() - cache.get("ts", 0)) / 3600
    age = f"{age_h * 60:.0f}m ago" if age_h < 1 else f"{age_h:.0f}h ago"
    stale = "  ⚠️ stale" if age_h > 26 else ""
    head = f"🕯️ RANGE BREAKOUT — {'+'.join(tfs)} · {len(hits)} coin(s)  ({age}{stale})"
    if not hits:
        return head + "\n\nNothing broke its range on the last closed candle."
    both = [h for h in hits if len(engulf_confirmed_tfs(h.get("tfs") or {})) > 1]
    rows = []
    for h in hits:
        marks = []
        for tf in sorted(h["tfs"], key=lambda t: (t != "1W", t)):
            s = h["tfs"][tf]
            marks.append(f"{'✅' if s['closed_through'] else '⚠️'}{tf}"
                         f"{'🫸' if s.get('engulf') else ''}")
        best = max(h["tfs"].values(), key=lambda s: s["vol_mult"])
        rows.append(f"{''.join(marks):<12} {h['coin']:<10} {best['chg']:+4.0f}%  "
                    f"${h['price']:g}  vol {best['vol_mult']}x")
    return "\n".join([
        head,
        f"_{len(both)} on both timeframes · cleared prior range on ≥{ENGULF_VOL_MULT}x median volume_",
        "", *rows, "",
        "✅ closed above the range · ⚠️ wick only · 🫸 engulfed prior bar",
    ])


def format_engulfing(hits: list, tfs: list | None = None) -> str:
    """Telegram/console view of a range-engulfing sweep across timeframes."""
    tfs = tfs or ENGULF_TFS
    label = "+".join(tfs)
    if not hits:
        return f"No {label} range-engulfing breakouts on the last closed candle."
    both = sum(1 for h in hits if len(engulf_confirmed_tfs(h["tfs"])) > 1)
    lines = [f"🕯️ *RANGE BREAKOUT* — {label} · {len(hits)} coin(s)"
             + (f" · {both} on both" if both else ""),
             "_last closed candle cleared its prior range on "
             f"≥{ENGULF_VOL_MULT}x median volume_", ""]
    for h in hits:
        tn = h["turnover"]
        tn_s = f"${tn / 1e6:.0f}M" if tn >= 1e6 else f"${tn / 1e3:.0f}k"
        parts = []
        for tf in sorted(h["tfs"], key=lambda t: (t != "1W", t)):
            s = h["tfs"][tf]
            parts.append(f"{'✅' if s['closed_through'] else '⚠️'}{tf}"
                         f"{'🫸' if s.get('engulf') else ''} {s['vol_mult']}x")
        lines.append(f"*{h['coin']}*  ${h['price']:g}  ({tn_s} 24h)   " + " · ".join(parts))
        for tf in sorted(h["tfs"], key=lambda t: (t != "1W", t)):
            s = h["tfs"][tf]
            lines.append(f"     {tf}: {s['chg']:+.0f}% · broke {s['prior_high']:g} "
                         f"· range {s['low']:g}–{s['high']:g}")
    lines += ["", "✅ closed above the range · ⚠️ wick only · 🫸 engulfed the prior bar"]
    return "\n".join(lines)


# ── Classification ──────────────────────────────────────────────────────
def classify(chg: float, rvol: float, turnover: float) -> str | None:
    # Gainer bar scales with the tier: a large cap moving +3% on volume IS the
    # breakout. A fixed 8% left everything from 4–8% unclassified — the exact
    # band a major breaks out in — so those coins never reached either tier.
    if chg >= breakout_min_chg(turnover) and rvol >= RVOL_MOMENTUM:
        return "momentum"
    if chg >= GAIN_PCT and rvol < RVOL_FADE:
        return "fade"
    if abs(chg) < COIL_MAX_CHG and rvol >= RVOL_BUY:
        return "buy"
    if chg <= DUMP_PCT and rvol >= RVOL_DUMP:
        return "buy"          # capitulation — confirm with exhaustion bottom
    # breakdown: a moderate loser (above the capitulation line) breaking down on
    # real volume — a shortable distribution move. TRAMA-down + anti-capitulation
    # guards in the confirm step separate a real breakdown from a bottom.
    if DUMP_PCT < chg <= -breakout_min_chg(turnover) and rvol >= RVOL_MOMENTUM:
        return "breakdown"
    return None


def _trama_confirmed(ctx: dict | None) -> bool:
    """Has TRAMA confirmed an uptrend? The bar the 🚀 breakout tier requires, and
    the line the ⚡ early tier fires *below*. No context (engine miss) counts as
    unconfirmed — the volume signature is what that tier trades on."""
    return bool(ctx and ctx.get("trend") == "up"
                and (ctx.get("slope") or 0) >= MOM_ALERT_SLOPE)


def momentum_context(coin: str) -> dict | None:
    """TRAMA trend + RSI/funding for a momentum-breakout candidate (an uptrend
    continuation read — no fade plan). Shaped so _exh_tag renders it too."""
    for src in (ew.BINANCE, ew.HL):
        try:
            if coin in src.universe():
                ev = ew.evaluate(coin, src)
                if ev:
                    return {"venue": src.name, "dir": ev["direction"] or "chop",
                            "signals": [k for k, _ in ev["signals"]],
                            "rsi": round(ev["rsi_now"]), "funding": round(ev["funding_ann"]),
                            "trend": ev.get("trend", "?"), "slope": ev.get("trama_slope"),
                            "trama": ev.get("trama"),
                            "from_high": ev.get("from_high_pct"),
                            "doji": ew._doji_desc([ev]),
                            "prev_doji": ev.get("prev_doji")}
        except Exception:
            pass
    return None


def exhaustion_confirm(coin: str, klass: str) -> dict | None:
    """Run the exhaustion engine for context (direction/RSI/funding/signals) plus
    the TRAMA trend and a level-based plan. A `buy` maps to a long-off-support
    setup, a `fade` to a short-into-resistance one, so `assess_exhaustion_trade`
    gives the same entry/stop/target/R:R the exhaustion alert would."""
    direction = "bottom" if klass == "buy" else "top"
    for src in (ew.BINANCE, ew.HL):
        try:
            if coin in src.universe():
                ev = ew.evaluate(coin, src)
                if not ev:
                    continue
                _, _, plan = ew.assess_exhaustion_trade(direction, ev, None)
                return {"venue": src.name, "dir": ev["direction"] or "chop",
                        "signals": [k for k, _ in ev["signals"]],
                        "rsi": round(ev["rsi_now"]),
                        "funding": round(ev["funding_ann"]),
                        "trend": ev.get("trend", "?"),
                        "slope": ev.get("trama_slope"),
                        "trama": plan.get("trama"), "trama_dist": plan.get("trama_dist"),
                        "doji": ew._doji_desc([ev]),
                        "entry": plan["entry"], "stop": plan["stop"],
                        "target": plan["target"], "rr": plan["rr"],
                        "at_level": plan["at_level"]}
        except Exception:
            pass
    return None


# ── Scan ────────────────────────────────────────────────────────────────
def run_scan(tickers: list | None = None) -> list:
    ew.BINANCE.prefetch()
    ew.HL.prefetch()
    crypto = ew.BINANCE.universe() | ew.HL.universe()

    tickers = tickers if tickers is not None else bitget_tickers()
    by_gain = sorted(tickers, key=lambda d: float(d.get("change24h", 0) or 0), reverse=True)[:GAINER_TOP]
    by_vol = sorted(tickers, key=lambda d: float(d.get("usdtVolume", 0) or 0), reverse=True)[:VOLUME_TOP]
    by_loss = sorted(tickers, key=lambda d: float(d.get("change24h", 0) or 0))[:LOSER_TOP]

    seen, candidates = set(), []
    for d in by_gain + by_vol + by_loss:             # gainers + volume + losers (breakdowns)
        sym = d["symbol"]
        base = sym[:-4] if sym.endswith("USDT") else sym
        if sym in seen or base in EXCLUDE or base not in crypto:
            continue
        seen.add(sym)
        candidates.append((sym, base, d))

    results = []
    for sym, base, d in candidates:
        chg = float(d.get("change24h", 0) or 0) * 100
        rv = bitget_rvol(sym)
        if rv is None:
            continue
        rvol, accel = rv
        turnover = float(d.get("usdtVolume", 0) or 0)
        klass = classify(chg, rvol, turnover)
        if not klass:
            continue
        sub = ("flush" if chg <= DUMP_PCT else "coil") if klass == "buy" else None
        rec = {"coin": base, "symbol": sym, "klass": klass, "sub": sub,
               "price": float(d.get("lastPr", 0) or 0),
               "chg24": round(chg, 1), "rvol": rvol, "accel": accel,
               "turnover": turnover,
               "exhaustion": None, "mom_breakout": False, "early_warn": False,
               "mom_breakdown": False, "engulf": None, "ts": int(time.time())}
        results.append(rec)
        time.sleep(0.04)

    # Confirm buy + fade candidates with the exhaustion engine (small set) and
    # attach a level plan. Don't fade a coin whose TRAMA is still sloping up hard
    # (the parabola trap) — reclassify such a fade as momentum so it won't alert.
    for rec in results:
        if rec["klass"] in ("buy", "fade"):
            rec["exhaustion"] = exhaustion_confirm(rec["coin"], rec["klass"])
            exh = rec["exhaustion"]
            if rec["klass"] == "fade" and exh and (exh.get("slope") or 0) > ew.TRAMA_STEEP:
                rec["klass"] = "momentum"
                rec["reclassified"] = "fade→momentum (steep TRAMA uptrend)"
            if rec["klass"] in ("buy", "fade"):        # still actionable → relative strength vs BTC
                rec["btc_ratio"] = ew._btc_ratio_tag(rec["coin"])

    # Two tiers off one read of the trend:
    #   🚀 breakout — a strong gainer on high, still-accelerating volume WITH a
    #      confirmed TRAMA uptrend (liquid enough to trade).
    #   ⚡ early    — the same volume signature BEFORE that confirmation lands.
    # Pre-gate on the cheap fields, then take at most one engine call per coin.
    # buy/fade already carry a context (with trend + slope) from the pass above.
    for rec in results:
        if rec["klass"] not in ("buy", "momentum") or rec["turnover"] < MOM_ALERT_MIN_TURNOVER:
            continue
        full = (rec["klass"] == "momentum" and rec["rvol"] >= MOM_ALERT_RVOL
                and rec["accel"] >= 1.0)
        # Price has to be participating at this coin's own scale. Without this the
        # tier fires on any volume blip in a flat market (it fired on BTC, ETH,
        # SOL, XRP, DOGE, ADA — all peaked ≤1.03x). The positive bar also excludes
        # a capitulation flush outright, so no separate check for it.
        early = (rec["rvol"] >= EARLY_RVOL and rec["accel"] >= EARLY_ACCEL
                 and rec["chg24"] >= breakout_min_chg(rec["turnover"]))
        if not (full or early):
            continue
        if rec["klass"] == "momentum":
            rec["exhaustion"] = momentum_context(rec["coin"])
        # Structural confirmation, as a peer of TRAMA: did the last CLOSED daily
        # OR weekly candle clear the whole range it was stuck in, on expanding
        # volume? Either timeframe confirms — a daily break is faster but noisier,
        # a weekly break is slower but structurally heavier. Only coins past the
        # pre-gate reach here, so it costs ~2 extra candle requests per scan.
        rec["engulf"] = engulfing_breakout_multi(rec["symbol"])
        engulf_confirms = bool(engulf_confirmed_tfs(rec["engulf"]))
        confirmed = _trama_confirmed(rec["exhaustion"]) or engulf_confirms
        if full and confirmed:
            rec["mom_breakout"] = True
        elif early and not confirmed:
            rec["early_warn"] = True
        if rec["mom_breakout"] or rec["early_warn"]:
            rec["btc_ratio"] = ew._btc_ratio_tag(rec["coin"])

    # 🔻 Short breakdown tier — mirror of the breakout, with anti-capitulation
    # guards so it shorts a distribution break, not a capitulation low.
    for rec in results:
        if rec["klass"] != "breakdown" or rec["turnover"] < MOM_ALERT_MIN_TURNOVER:
            continue
        if not (rec["rvol"] >= MOM_ALERT_RVOL and rec["accel"] >= 1.0):
            continue
        ctx = momentum_context(rec["coin"])
        rec["exhaustion"] = ctx
        if not ctx:
            continue
        trama_down = ctx.get("trend") == "down" and (ctx.get("slope") or 0) <= -MOM_ALERT_SLOPE
        # not already oversold, and funding not already crowded-short (squeeze fuel)
        early_enough = (ctx.get("rsi", 50) >= BREAKDOWN_RSI_FLOOR
                        and (ctx.get("funding") or 0) >= BREAKDOWN_FUNDING_FLOOR)
        if trama_down and early_enough:
            rec["mom_breakdown"] = True
            rec["btc_ratio"] = ew._btc_ratio_tag(rec["coin"])

    order = {"buy": 0, "fade": 1, "momentum": 2, "breakdown": 3}
    results.sort(key=lambda r: (order.get(r["klass"], 9), -r["rvol"]))
    return results


# ── State / alerts / output ─────────────────────────────────────────────
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


# ── Momentum-breakout outcome log (did the signal work?) ──────────────────
# ── Momentum-breakout outcome log (did the signal work?) ──────────────────
def _load_breakout_log() -> list:
    if not BREAKOUT_LOG.exists():
        return []
    try:
        return json.loads(BREAKOUT_LOG.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _save_breakout_log(log: list) -> None:
    BREAKOUT_LOG.parent.mkdir(parents=True, exist_ok=True)
    BREAKOUT_LOG.write_text(json.dumps(log, indent=2), encoding="utf-8")


def _price_map(tickers: list) -> dict:
    """base asset -> last price, from a Bitget ticker array."""
    m = {}
    for d in tickers:
        sym = d["symbol"]
        base = sym[:-4] if sym.endswith("USDT") else sym
        m[base] = float(d.get("lastPr", 0) or 0)
    return m


def _new_breakout_record(rec: dict, source: str = "auto", direction: str = "long") -> dict:
    """Snapshot a fresh signal as a tracking record — the start of its journey.
    `source` is 'auto'/'manual'/'early'/'breakdown'; `direction` is 'long' or
    'short' (a breakdown), so the hit-rate stats separate them."""
    exh = rec.get("exhaustion") or {}
    now = int(time.time())
    p = rec["price"]
    return {
        "coin": rec["coin"],
        "source": source,
        "direction": direction,
        "entry_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "trigger_ts": now,
        "trigger_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "trigger_price": p,
        "context": {"chg24": rec["chg24"], "rvol": rec["rvol"], "accel": rec["accel"],
                    "turnover": round(rec["turnover"]), "slope": exh.get("slope"),
                    "rsi": exh.get("rsi"), "funding": exh.get("funding"),
                    "trend": exh.get("trend"), "btc_ratio": rec.get("btc_ratio"),
                    "engulf_tfs": engulf_confirmed_tfs(rec.get("engulf")) or None},
        "peak_price": p, "peak_mult": 1.0, "peak_ts": now,
        "low_price": p, "low_mult": 1.0,
        "last_price": p, "last_mult": 1.0, "last_ts": now,
        "days": 0.0, "status": "tracking",
    }


def log_new_breakouts(alerted: list, source: str = "auto", direction: str = "long") -> None:
    """Start a journey for each freshly-alerted signal (skip if this coin is
    already tracking under the SAME source). Keyed by (coin, source) so a coin
    that warns early and later confirms gets one record per tier — the auto
    hit-rate in format_breakout_performance stays a clean read of the 🚀 gate."""
    if not alerted:
        return
    log = _load_breakout_log()
    active = {(e["coin"], e.get("source", "auto"))
              for e in log if e.get("status") == "tracking"}
    for rec in alerted:
        if (rec["coin"], source) not in active:
            log.append(_new_breakout_record(rec, source, direction))
    _save_breakout_log(log)


def update_breakout_log(price_map: dict) -> list:
    """Advance every tracking record's price journey from the current prices:
    peak (best multiple), low (worst drawdown), last, and days elapsed. Closes a
    record once it's been tracked for TRACK_DAYS. Returns the full log."""
    log = _load_breakout_log()
    now = time.time()
    changed = False
    for e in log:
        if e.get("status") != "tracking":
            continue
        p = price_map.get(e["coin"])
        tp = e["trigger_price"]
        if p and tp:
            e["last_price"], e["last_mult"], e["last_ts"] = p, round(p / tp, 3), int(now)
            if p > e["peak_price"]:
                e["peak_price"], e["peak_mult"], e["peak_ts"] = p, round(p / tp, 3), int(now)
            if p < e["low_price"]:
                e["low_price"], e["low_mult"] = p, round(p / tp, 3)
        e["days"] = round((now - e["trigger_ts"]) / 86400, 1)
        if e["days"] >= TRACK_DAYS:
            e["status"] = "closed"
        changed = True
    if changed:
        _save_breakout_log(log)
    return log


def _tier_of(e: dict) -> str:
    """Cap tier a record was logged at (turnover snapshot at trigger time)."""
    return cap_tier((e.get("context") or {}).get("turnover") or 0)


def _fav_mult(e: dict) -> float:
    """Favorable-excursion multiple in the trade's OWN direction (≥1 = in profit).
    Long: best up-move (peak). Short: best down-move, expressed as 1/low."""
    if e.get("direction") == "short":
        lo = e.get("low_mult") or 1.0
        return (1.0 / lo) if lo else 1.0
    return e.get("peak_mult", 1.0)


def _worked(e: dict) -> bool:
    """Did this record hit its tier's success bar (in its own direction)? Shared by
    the verdict label and the performance stats so the two can never disagree."""
    return _fav_mult(e) >= WORKED_MULT[_tier_of(e)]


def _mild(e: dict) -> bool:
    return _fav_mult(e) >= MILD_MULT[_tier_of(e)]


def _verdict(e: dict) -> str:
    """Compact outcome label for a record, judged against its own tier + direction."""
    pk, lo, days = e.get("peak_mult", 1), e.get("low_mult", 1), e.get("days", 0)
    if _worked(e):
        tag = "🟢 worked"
    elif _mild(e):
        tag = "🟡 mild"
    elif days < 1:
        tag = "⏳ new"
    else:
        tag = "🔴 faded"
    if e.get("direction") == "short":     # for a short, DOWN is favorable
        return f"{tag} · low {lo:.2f}x (fav {_fav_mult(e):.2f}x) · high {pk:.2f}x · now {e.get('last_mult', 1):.2f}x"
    return f"{tag} · peak {pk:.2f}x · low {lo:.2f}x · now {e.get('last_mult', 1):.2f}x"


def format_breakout_report() -> str:
    """Human-readable outcome journal for the `--breakouts` CLI."""
    log = sorted(_load_breakout_log(), key=lambda e: e["trigger_ts"], reverse=True)
    if not log:
        return "No momentum breakouts logged yet."
    live = [e for e in log if e.get("status") == "tracking"]
    hit = sum(1 for e in log if _worked(e))
    early_n = sum(1 for e in log if e.get("source") == "early")
    head = (f"SIGNAL LOG — {len(log)} total, {len(live)} tracking, {hit} hit target")
    if early_n:
        head += f"  ({early_n} early-warning)"
    lines = [head + "\n"]
    for e in log:
        c = e["context"]
        lines.append(
            f"{e['coin']:10} entry {e.get('entry_date','?')}  @ ${e['trigger_price']:g}  "
            f"[{e.get('source','auto')}/{e.get('status','?')} {e.get('days',0):.0f}d]")
        lines.append(f"           trigger: +{c.get('chg24',0):.0f}% RVOL {c.get('rvol','?')}x "
                     f"TRAMA +{c.get('slope') or 0:.1f}% turnover ${(c.get('turnover') or 0)/1e6:.0f}M")
        lines.append(f"           {_verdict(e)}")
    return "\n".join(lines)


def format_breakout_performance() -> str | None:
    """Weekly Telegram performance summary of the AUTO breakout signal (excludes
    hand-added manual entries). None if there's nothing meaningful to report."""
    full = _load_breakout_log()
    log = [e for e in full if e.get("source", "auto") == "auto"]
    if not log and not any(e.get("direction") == "short" for e in full):
        return None
    now = datetime.now(timezone.utc)
    matured = [e for e in log if e.get("status") != "tracking" or e.get("days", 0) >= PERF_MATURE_DAYS]
    running = sorted([e for e in log if e.get("status") == "tracking" and e.get("last_mult", 1) >= 1.2],
                     key=lambda e: -e.get("last_mult", 1))

    def pct(m):
        return f"{(m - 1) * 100:+.0f}%"

    lines = [f"📊 *BREAKOUT PERFORMANCE*  ({now.strftime('%Y-%m-%d')})",
             f"_auto signal · {len(log)} tracked · {len(matured)} matured (≥{PERF_MATURE_DAYS:.0f}d)_", ""]
    if matured:
        n = len(matured)
        # Per-tier bars (large +10% / mid +25% / small +50%) — see WORKED_MULT.
        worked = sum(1 for e in matured if _worked(e))
        mild = sum(1 for e in matured if _mild(e))
        avg_peak = sum(e.get("peak_mult", 1) for e in matured) / n
        avg_dd = sum(e.get("low_mult", 1) for e in matured) / n
        ttp = [(e["peak_ts"] - e["trigger_ts"]) / 86400 for e in matured
               if e.get("peak_ts") and e.get("peak_mult", 1) > 1.05]
        best = max(matured, key=lambda e: e.get("peak_mult", 1))
        worst = min(matured, key=lambda e: e.get("low_mult", 1))
        lines += [
            f"🟢 hit target: {worked}/{n} ({100 * worked / n:.0f}%)  ·  "
            f"🟡 partial: {mild}/{n} ({100 * mild / n:.0f}%)",
            f"avg peak {pct(avg_peak)}  ·  avg max drawdown {pct(avg_dd)}",
        ]
        if ttp:
            lines.append(f"median time-to-peak: {sorted(ttp)[len(ttp) // 2]:.1f}d")
        lines.append(f"best {best['coin']} {pct(best.get('peak_mult', 1))}  ·  "
                     f"worst {worst['coin']} {pct(worst.get('low_mult', 1))}")
    else:
        lines.append("_no matured samples yet — first real read in ~1–2 weeks._")
    if running:
        lines += ["", "🏃 running now: " + " · ".join(f"{e['coin']} {pct(e.get('last_mult', 1))}"
                                                       for e in running[:5])]

    # Short-breakdown tier — reported SEPARATELY (unproven; excluded from the auto
    # stats above via source != "auto").
    shorts = [e for e in _load_breakout_log() if e.get("direction") == "short"]
    if shorts:
        sm = [e for e in shorts if e.get("status") != "tracking" or e.get("days", 0) >= PERF_MATURE_DAYS]
        lines += ["", f"🔻 *breakdown (short, unproven)* — {len(shorts)} tracked · {len(sm)} matured"]
        if sm:
            sw = sum(1 for e in sm if _worked(e))
            afav = sum(_fav_mult(e) for e in sm) / len(sm)
            lines.append(f"   hit: {sw}/{len(sm)} ({100 * sw / len(sm):.0f}%) · avg favorable {pct(afav)}")
        else:
            lines.append("   _no matured shorts yet_")

    lines += ["", "_Auto breakout signal (🔻 short reported separately). `--breakouts` / "
              "`--breakdown` for the logs._"]
    return "\n".join(lines)


def format_breakout_brief() -> str:
    """One line per coin — the whole journal compressed for a Telegram reply.
    `format_breakout_report` is the roomy console view; this is the one that has
    to survive Telegram's 4096-char message cap, so keep it to a single line per
    record. Newest first, tier marked (⚡ early, ✋ manual, unmarked auto)."""
    log = sorted([e for e in _load_breakout_log() if e.get("direction", "long") == "long"],
                 key=lambda e: e["trigger_ts"], reverse=True)
    if not log:
        return "No breakouts logged yet."
    tally = {"🟢": 0, "🟡": 0, "🔴": 0, "⏳": 0}
    rows = []
    for e in log:
        mark = _verdict(e).split(" ", 1)[0]         # reuse the report's thresholds
        tally[mark] = tally.get(mark, 0) + 1
        src = {"early": " ⚡", "manual": " ✋", "dislocation": " ⚖️"}.get(e.get("source", "auto"), "")
        tier = {"large": " Ⓛ", "mid": " Ⓜ", "small": ""}[_tier_of(e)]
        # Entry (trigger) price — the number every multiple on the row is measured
        # against, so the row is readable without cross-referencing the full log.
        entry = f"${e['trigger_price']:g}"
        rows.append(f"{mark} {e['coin']:<10} {e.get('days', 0):>3.0f}d  {entry:<11} "
                    f"pk {e.get('peak_mult', 1):.2f}x  now {e.get('last_mult', 1):.2f}x{src}{tier}")
    tracking = sum(1 for e in log if e.get("status") == "tracking")
    return "\n".join([
        f"📓 BREAKOUT LOG — {len(log)} coins ({tracking} tracking)",
        f"🟢 {tally['🟢']} worked · 🟡 {tally['🟡']} mild · "
        f"🔴 {tally['🔴']} faded · ⏳ {tally['⏳']} new",
        "", *rows, "",
        "$ = entry price · ⚡ early · ✋ manual · Ⓛ large Ⓜ mid cap",
        "🟢/🟡 judged per tier (large +10% · mid +25% · small +50%)",
    ])


def format_breakdown_brief() -> str:
    """One line per SHORT (breakdown) — for `--breakdown` / Telegram /breakdown.
    Mirrors the breakout brief; favorable = price DOWN, so the row shows the low
    multiple and the favorable-in-direction figure."""
    log = sorted([e for e in _load_breakout_log() if e.get("direction") == "short"],
                 key=lambda e: e["trigger_ts"], reverse=True)
    if not log:
        return ("No breakdowns logged yet.\n"
                "(🔻 short tier is UNPROVEN — journaled, not pushed, until the log earns it.)")
    tally = {"🟢": 0, "🟡": 0, "🔴": 0, "⏳": 0}
    rows = []
    for e in log:
        mark = _verdict(e).split(" ", 1)[0]
        tally[mark] = tally.get(mark, 0) + 1
        tier = {"large": " Ⓛ", "mid": " Ⓜ", "small": ""}[_tier_of(e)]
        entry = f"${e['trigger_price']:g}"
        rows.append(f"{mark} {e['coin']:<10} {e.get('days', 0):>3.0f}d  {entry:<11} "
                    f"low {e.get('low_mult', 1):.2f}x  fav {_fav_mult(e):.2f}x  now {e.get('last_mult', 1):.2f}x{tier}")
    tracking = sum(1 for e in log if e.get("status") == "tracking")
    return "\n".join([
        f"🔻 BREAKDOWN LOG (short, UNPROVEN) — {len(log)} coins ({tracking} tracking)",
        f"🟢 {tally['🟢']} worked · 🟡 {tally['🟡']} mild · "
        f"🔴 {tally['🔴']} faded · ⏳ {tally['⏳']} new",
        "", *rows, "",
        "fav = favorable move (price DOWN) · low = lowest / entry · Ⓛ large Ⓜ mid cap",
        "🟢/🟡 judged per tier (large +10% · mid +25% · small +50%) — measure before trusting.",
    ])


def _exh_tag(exh: dict | None) -> str:
    if not exh:
        return ""
    sig = f" {len(exh['signals'])}sig" if exh["signals"] else ""
    tr = f", {exh['trend']}" if exh.get("trend") else ""
    return f"  · exh: {exh['dir']}{sig} (RSI {exh['rsi']}, fund {exh['funding']:+d}%{tr})"


def _doji_line(exh: dict | None) -> str | None:
    """Doji reversal flag for a buy/fade coin, if the engine flagged one."""
    return f"       🕯️ {exh['doji']}" if exh and exh.get("doji") else None


def _trama_line(exh: dict | None) -> str | None:
    """TRAMA level and whether price sits above or below it, for a buy/fade coin."""
    if not exh or exh.get("trama") is None or exh.get("trama_dist") is None:
        return None
    td = exh["trama_dist"]
    return (f"       TRAMA ${exh['trama']:g} · price {abs(td):.1f}% "
            f"{'above 🟢' if td >= 0 else 'below 🔴'}")


def _plan_line(exh: dict | None) -> str | None:
    """Level-based entry/stop/target/R:R for a buy/fade candidate, if mapped."""
    if not exh or not exh.get("entry") or not exh.get("rr"):
        return None
    at = " · at level" if exh.get("at_level") else " · wait for level"
    return (f"       entry ${exh['entry']:g} · stop ${exh['stop']:g} · "
            f"target ${exh['target']:g} · R:R {exh['rr']}{at}")


def format_digest(results: list, highlight: set) -> str:
    now = datetime.now(timezone.utc).strftime("%H:%M UTC")
    buckets = {"buy": [], "fade": [], "momentum": []}
    for r in results:
        buckets[r["klass"]].append(r)
    lines = [f"🔎 *DISCOVERY SCAN*  ({now})",
             f"_{len(buckets['buy'])} buy · {len(buckets['fade'])} fade · "
             f"{len(buckets['momentum'])} momentum_", ""]
    titles = {"buy": "🟢 *BUY* _(early — volume up, price coiling)_",
              "fade": "🔴 *FADE* _(extended — volume fading, exhausting)_",
              "momentum": "🟡 *MOMENTUM* _(running — volume backing)_"}
    for k in ("buy", "fade", "momentum"):
        if not buckets[k]:
            continue
        lines.append(titles[k])
        for r in buckets[k][:6]:
            star = "🆕 " if r["coin"] in highlight else ""
            bo = "🚀 " if r.get("mom_breakout") else ("⚡ " if r.get("early_warn") else "")
            arrow = "↑" if r["accel"] >= 1 else "↓"
            sub = f" _{r['sub']}_" if r.get("sub") else ""
            lines.append(f"  • {star}{bo}*{r['coin']}*{sub}  ${r['price']:g}  {r['chg24']:+.0f}%  "
                         f"RVOL {r['rvol']:g}x{arrow}{_exh_tag(r['exhaustion'])}")
            if r["klass"] in ("buy", "fade"):
                btc = f"       {r['btc_ratio']}" if r.get("btc_ratio") else None
                for extra in (_doji_line(r["exhaustion"]), _trama_line(r["exhaustion"]),
                              _plan_line(r["exhaustion"]), btc):
                    if extra:
                        lines.append(extra)
            elif (r.get("mom_breakout") or r.get("early_warn")) and r.get("btc_ratio"):
                lines.append(f"       {r['btc_ratio']}")
        lines.append("")
    lines.append("_vs BTC 🟢 above / 🔴 below the coin's BTC-ratio TRAMA (relative strength)._")
    lines.append("_vs BTC 🟢 above / 🔴 below the coin's BTC-ratio TRAMA (relative strength)._")
    lines.append("_Discovery only — confirm with /review <coin>. Odds, not certainty._")
    return "\n".join(lines)


def _engulf_line(rec: dict) -> str | None:
    """Range-breakout context across timeframes, strongest (closed-through) first."""
    multi = rec.get("engulf") or {}
    if not multi:
        return None
    rows = sorted(multi.items(), key=lambda kv: (not kv[1]["closed_through"], kv[0]))
    out = []
    for tf, e in rows:
        kind = "closed above" if e["closed_through"] else "wicked through"
        bar = " · engulfed prior bar" if e.get("engulf") else ""
        out.append(f"  🕯️ {tf} candle {kind} its {e['lookback']}-bar high "
                   f"{e['prior_high']:g} ({e['chg']:+.0f}%, vol {e['vol_mult']}x median){bar}")
    return "\n".join(out)


def _basis_line(rec: dict) -> str | None:
    """Spot-vs-perp basis context for a breakout/early message. A perp premium on a
    rip = leverage-led (caution); a perp discount = spot-led / hedged (healthier)."""
    b = rec.get("basis")
    if not b or abs(b["basis"]) < 0.5:
        return None
    if b["kind"] == "premium":
        read = "perp premium — leverage/FOMO-led, squeeze risk" if b["basis"] >= 2 else "perp ≈ spot"
    else:
        read = "perp discount — spot-led / hedged (healthier)" if abs(b["basis"]) >= 2 else "perp ≈ spot"
    return f"       basis {b['basis']:+.1f}% (perp {b['perp']:g} vs spot {b['spot']:g}) — {read}"


def format_dislocation(coin: str, b: dict) -> str:
    """Standalone ⚖️ alert — perp trading substantially away from spot (Bitget)."""
    now = datetime.now(timezone.utc).strftime("%H:%M UTC")
    tn = min(b["perp_vol"], b["spot_vol"])
    tn_s = f"${tn / 1e6:.0f}M" if tn >= 1e6 else f"${tn / 1e3:.0f}k"
    if b["kind"] == "discount":
        read = ("perp is trading BELOW spot — a liquidation-cascade / stress tell. As it "
                "re-converges the perp tends to bounce back toward spot (mean-reversion long); "
                "but a persistent discount means sellers keep hitting the perp. Confirm before "
                "catching it.")
    else:
        read = ("perp is trading ABOVE spot — leverage/FOMO-driven. As it re-converges the perp "
                "tends to fall back toward spot (mean-reversion short); crowded longs paying up.")
    return "\n".join([
        f"⚖️ *PERP DISLOCATION* — *{coin}*  ({now})",
        f"basis *{b['basis']:+.1f}%*  ·  perp {b['perp']:g}  vs  spot {b['spot']:g}  ·  vol {tn_s}",
        f"_{read}_",
        "_Bitget spot vs perp. Odds, not certainty — check funding + liquidity before acting._",
    ])


def format_breakout(rec: dict) -> str:
    """Dedicated 'get in early' push for a momentum-breakout candidate."""
    now = datetime.now(timezone.utc).strftime("%H:%M UTC")
    exh = rec.get("exhaustion") or {}
    arrow = "↑" if rec["accel"] >= 1 else "↓"
    tn = rec["turnover"]
    tn_s = f"${tn / 1e6:.0f}M" if tn >= 1e6 else f"${tn / 1e3:.0f}k"
    lines = [
        f"🚀 *MOMENTUM BREAKOUT* — *{rec['coin']}*  ({now})",
        f"${rec['price']:g}  ·  {rec['chg24']:+.0f}% 24h  ·  RVOL {rec['rvol']:g}x{arrow}  ·  turnover {tn_s}",
    ]
    ctx = []
    if exh.get("slope") is not None:
        # Render the ACTUAL trend — a breakout can now be confirmed by the range
        # break alone, so "up" is no longer safe to assume here.
        ctx.append(f"TRAMA {exh.get('trend', '?')} ({exh['slope']:+.1f}%)")
    if exh.get("rsi") is not None:
        ctx.append(f"RSI {exh['rsi']}")
    if exh.get("funding") is not None:
        ctx.append(f"funding {exh['funding']:+d}%/yr")
    if ctx:
        lines.append("  " + " · ".join(ctx))
    eng = _engulf_line(rec)
    if eng:
        lines.append(eng)
    bl = _basis_line(rec)
    if bl:
        lines.append(bl)
    if rec.get("btc_ratio"):
        lines.append(rec["btc_ratio"])
    lines.append("_Breakout on strong, rising volume, confirmed by the trend (TRAMA) or by a "
                 "higher-timeframe range break — an early-momentum entry. Use a stop and don't "
                 "chase once extended; parabolas mean-revert hard._")
    return "\n".join(lines)


def format_breakdown(rec: dict) -> str:
    """Short-side push — a distribution breakdown on volume (UNPROVEN tier)."""
    now = datetime.now(timezone.utc).strftime("%H:%M UTC")
    exh = rec.get("exhaustion") or {}
    tn = rec["turnover"]
    tn_s = f"${tn / 1e6:.0f}M" if tn >= 1e6 else f"${tn / 1e3:.0f}k"
    lines = [
        f"🔻 *MOMENTUM BREAKDOWN* — *{rec['coin']}*  ({now})",
        f"${rec['price']:g}  ·  {rec['chg24']:+.0f}% 24h  ·  RVOL {rec['rvol']:g}x  ·  turnover {tn_s}",
    ]
    ctx = []
    if exh.get("slope") is not None:
        ctx.append(f"TRAMA down ({exh['slope']:.1f}%)")
    if exh.get("rsi") is not None:
        ctx.append(f"RSI {exh['rsi']}")
    if exh.get("funding") is not None:
        ctx.append(f"funding {exh['funding']:+d}%/yr")
    if ctx:
        lines.append("  " + " · ".join(ctx))
    if rec.get("btc_ratio"):
        lines.append(rec["btc_ratio"])
    lines.append("_Breaking down on volume, TRAMA rolling over, not yet oversold — a short-side "
                 "distribution break. SHORTS ARE UNPROVEN here — journaled, measure before trusting. "
                 "Squeeze risk is real; use a stop._")
    return "\n".join(lines)


def format_early(rec: dict) -> str:
    """Early-warning push — the flow is arriving before the trend confirms."""
    now = datetime.now(timezone.utc).strftime("%H:%M UTC")
    exh = rec.get("exhaustion") or {}
    tn = rec["turnover"]
    tn_s = f"${tn / 1e6:.0f}M" if tn >= 1e6 else f"${tn / 1e3:.0f}k"
    lines = [
        f"⚡ *EARLY WARNING* — *{rec['coin']}*  ({now})",
        f"${rec['price']:g}  ·  {rec['chg24']:+.0f}% 24h  ·  RVOL {rec['rvol']:g}x  ·  "
        f"accel {rec['accel']:g}x↑  ·  turnover {tn_s}",
    ]
    ctx = []
    if exh.get("trend"):
        slope = exh.get("slope")
        ctx.append(f"TRAMA {exh['trend']}"
                   + (f" ({slope:+.1f}%)" if slope is not None else "") + " — not confirmed")
    if exh.get("rsi") is not None:
        ctx.append(f"RSI {exh['rsi']}")
    if exh.get("funding") is not None:
        ctx.append(f"funding {exh['funding']:+d}%/yr")
    if ctx:
        lines.append("  " + " · ".join(ctx))
    eng = _engulf_line(rec)
    if eng:
        lines.append(eng)
    bl = _basis_line(rec)
    if bl:
        lines.append(bl)
    if rec.get("btc_ratio"):
        lines.append(rec["btc_ratio"])
    lines.append(f"_Volume is arriving ({rec['rvol']:g}x baseline, {rec['accel']:g}x the prior 3h) "
                 f"but TRAMA has not confirmed a trend — earlier and less certain than a 🚀 "
                 f"breakout, and it fails more often. Wait for confirmation or size small._")
    return "\n".join(lines)


def publish(results: list) -> None:
    if not results or not ew._firebase_ready():
        return
    payload = {r["coin"]: r for r in results}
    try:
        ew._fb_db.reference("scan").set(payload)   # replace whole node each run
        print(f"  📡 Published {len(payload)} scan results to Firebase (scan/).")
    except Exception as e:
        print(f"  [WARN] Firebase scan publish failed: {type(e).__name__}: {str(e)[:120]}")


def main() -> None:
    print("\n" + "=" * 64)
    print("  DISCOVERY SCAN — buy / fade / momentum radar (Bitget)")
    print("  " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 64)

    tickers = bitget_tickers()
    results = run_scan(tickers)

    # Spot-vs-perp basis (one extra spot call): attach to every rec for breakout
    # context, and scan the whole board for standalone dislocations.
    basis_map = compute_basis_map(tickers, bitget_spot_prices())
    for r in results:
        r["basis"] = basis_map.get(r["coin"])

    for r in results:
        arrow = "↑" if r["accel"] >= 1 else "↓"
        tier = " 🚀" if r.get("mom_breakout") else (" ⚡" if r.get("early_warn") else "")
        print(f"  {r['klass']:9} {r['coin']:8} {r['chg24']:+6.0f}%  "
              f"RVOL {r['rvol']:5g}x{arrow}{tier}{_exh_tag(r['exhaustion'])}")
    if not results:
        print("  (nothing classified this scan)")

    state = _load_state()
    prev = state.get("classes", {})
    now_cls = {r["coin"]: r["klass"] for r in results}
    # New buy/fade candidates since last scan → these trigger the digest alert.
    # First run (no prior state) seeds silently — no alert burst on deploy.
    new = set() if not prev else {c for c, k in now_cls.items()
                                  if k in ("buy", "fade") and prev.get(c) != k}

    # Momentum-breakout pushes — one per coin per MOM_ALERT_COOLDOWN_H (re-arms
    # after the cooldown). Seeded silently on the first run, like the digest.
    now_ts = time.time()
    bo_alerts = state.get("breakout_alerts", {})
    breakouts = [r for r in results if r.get("mom_breakout")]
    fresh_bo = ([] if not prev else
                [r for r in breakouts
                 if now_ts - bo_alerts.get(r["coin"], 0) >= MOM_ALERT_COOLDOWN_H * 3600])

    # Early warnings — same dedup shape, own cooldown. Also suppressed if the coin
    # pushed a 🚀 breakout inside the window: warning *after* confirming is backwards.
    early_alerts = state.get("early_alerts", {})
    early_live = "early_alerts" in state      # False on the first scan after this tier shipped
    earlies = [r for r in results if r.get("early_warn")]
    fresh_early = ([] if not prev or not early_live else
                   [r for r in earlies
                    if now_ts - early_alerts.get(r["coin"], 0) >= EARLY_COOLDOWN_H * 3600
                    and now_ts - bo_alerts.get(r["coin"], 0) >= EARLY_COOLDOWN_H * 3600])
    if not early_live:
        # Seed the tier silently — arm every current coin's cooldown so switching
        # it on doesn't dump a batch of ⚡ pings for coins already mid-ramp.
        for r in earlies:
            early_alerts[r["coin"]] = now_ts
        print(f"  (early-warning tier seeded silently on {len(earlies)} coin(s) "
              f"— pings start next scan)")

    publish(results)
    if new:
        if ew.send_signal(format_digest(results, new)):
            print(f"  📣 Digest sent (new: {', '.join(sorted(new))}).")
        elif ew.SIGNALS_MUTED:
            print(f"  🔇 Digest MUTED (new: {', '.join(sorted(new))}) — tracking only.")
    else:
        print("  (no new buy/fade candidate — no digest)")

    # Momentum breakouts always push (NOT part of the digest mute) and are journaled.
    log_new_breakouts(fresh_bo)
    for r in fresh_bo:
        bo_alerts[r["coin"]] = now_ts        # arm cooldown (dedups push + reprocessing)
        if ew.send_telegram_private(format_breakout(r)):
            print(f"  🚀 Momentum-breakout push sent: {r['coin']} "
                  f"({r['chg24']:+.0f}%, RVOL {r['rvol']:g}x).")
        else:
            print(f"  ⚠️  Momentum-breakout send failed (logged): {r['coin']}.")
    if breakouts and not fresh_bo:
        print(f"  (breakouts on cooldown: {', '.join(r['coin'] for r in breakouts)})")

    # Early warnings — the "catch the low" tier. Pushes even while the digests are
    # muted (send_telegram_private, NOT send_signal): it's the earliest entry
    # signal and worth the noise. Still journaled under source="early".
    log_new_breakouts(fresh_early, source="early")
    for r in fresh_early:
        early_alerts[r["coin"]] = now_ts     # arm cooldown regardless of delivery
        if ew.send_telegram_private(format_early(r)):
            print(f"  ⚡ Early-warning push sent: {r['coin']} "
                  f"({r['chg24']:+.0f}%, RVOL {r['rvol']:g}x, accel {r['accel']:g}x).")
        else:
            print(f"  ⚠️  Early-warning send failed (logged): {r['coin']}.")
    if earlies and not fresh_early:
        print(f"  (early warnings on cooldown: {', '.join(r['coin'] for r in earlies)})")

    # 🔻 Short breakdowns — journaled under direction="short"; alerts respect the
    # digest mute (send_signal) until the log proves the tier. View via --breakdown.
    bd_alerts = state.get("breakdown_alerts", {})
    breakdowns = [r for r in results if r.get("mom_breakdown")]
    fresh_bd = ([] if not prev else
                [r for r in breakdowns
                 if now_ts - bd_alerts.get(r["coin"], 0) >= MOM_ALERT_COOLDOWN_H * 3600])
    log_new_breakouts(fresh_bd, source="breakdown", direction="short")
    for r in fresh_bd:
        bd_alerts[r["coin"]] = now_ts
        if ew.send_signal(format_breakdown(r)):
            print(f"  🔻 Breakdown push sent: {r['coin']} ({r['chg24']:+.0f}%, RVOL {r['rvol']:g}x).")
        else:
            print(f"  🔇 Breakdown logged, push muted (unproven tier): {r['coin']}.")
    if breakdowns and not fresh_bd:
        print(f"  (breakdowns on cooldown: {', '.join(r['coin'] for r in breakdowns)})")

    # ⚖️ Perp dislocations — perp trading far from spot (Bitget), liquidity+sanity
    # filtered. Rare + high-signal so it PUSHES (send_telegram_private); journaled for
    # a measure-first track record as a mean-reversion play (discount → bounce-long,
    # premium → fade-short: the perp tends to re-converge toward spot).
    disl_alerts = state.get("basis_alerts", {})
    dislocated = sorted(((c, b) for c, b in basis_map.items() if _basis_dislocated(b)),
                        key=lambda cb: -abs(cb[1]["basis"]))
    fresh_disl = ([] if not prev else
                  [(c, b) for c, b in dislocated
                   if now_ts - disl_alerts.get(c, 0) >= BASIS_COOLDOWN_H * 3600])
    for c, b in fresh_disl:
        drec = {"coin": c, "price": b["perp"], "chg24": b["basis"], "rvol": 0, "accel": 0,
                "turnover": min(b["perp_vol"], b["spot_vol"]),
                "exhaustion": None, "btc_ratio": None}
        log_new_breakouts([drec], source="dislocation",
                          direction="long" if b["kind"] == "discount" else "short")
        disl_alerts[c] = now_ts
        if ew.send_telegram_private(format_dislocation(c, b)):
            print(f"  ⚖️ Dislocation push sent: {c} basis {b['basis']:+.1f}%.")
    if dislocated and not fresh_disl:
        print(f"  (dislocations on cooldown: {', '.join(c for c, _ in dislocated)})")

    # Advance every tracked coin's price journey (peak / drawdown / current).
    log = update_breakout_log(_price_map(tickers))
    tracking = sum(1 for e in log if e.get("status") == "tracking")
    if tracking:
        print(f"  📓 Breakout log: {tracking} coin(s) tracking ({len(log)} total). "
              f"View with `python discovery_scan.py --breakouts`.")

    # Weekly breakout-performance summary (report, always sends — not muted).
    # Dedup once per ISO week; fires on the first scan at the configured day/hour.
    nowdt = datetime.now(timezone.utc)
    wk = nowdt.strftime("%G-W%V")
    if (nowdt.weekday() == PERF_SUMMARY_DAY and nowdt.hour == PERF_SUMMARY_HOUR
            and state.get("perf_week") != wk):
        msg = format_breakout_performance()
        if msg and ew.send_telegram_private(msg):
            state["perf_week"] = wk
            print(f"  📊 Weekly breakout performance summary sent ({wk}).")

    state["classes"] = now_cls
    # keep only currently-active breakout timestamps so stale coins re-arm cleanly
    state["breakout_alerts"] = {r["coin"]: bo_alerts[r["coin"]]
                                for r in breakouts if r["coin"] in bo_alerts}
    state["early_alerts"] = {r["coin"]: early_alerts[r["coin"]]
                             for r in earlies if r["coin"] in early_alerts}
    state["breakdown_alerts"] = {r["coin"]: bd_alerts[r["coin"]]
                                 for r in breakdowns if r["coin"] in bd_alerts}
    state["basis_alerts"] = {c: disl_alerts[c] for c, _ in dislocated if c in disl_alerts}
    _save_state(state)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("--breakouts", "--log"):
        perf = format_breakout_performance()
        if perf:
            print(perf.replace("*", "") + "\n" + "─" * 60 + "\n")
        print(format_breakout_report())
    elif len(sys.argv) > 1 and sys.argv[1] in ("--breakouts-brief", "--brief"):
        print(format_breakout_brief())          # compact — sized for a Telegram reply
    elif len(sys.argv) > 1 and sys.argv[1] in ("--breakdown", "--breakdowns"):
        print(format_breakdown_brief())         # short-side journal (Telegram /breakdown)
    elif len(sys.argv) > 1 and sys.argv[1] in ("--engulf", "--range"):
        # No 2nd arg → both timeframes. `--engulf 1D` / `--engulf 1W` narrows it.
        _tfs = [sys.argv[2]] if len(sys.argv) > 2 else ENGULF_TFS
        print(f"Scanning the board for {'+'.join(_tfs)} range-engulfing breakouts…")
        _hits = scan_engulfing(_tfs, verbose=True)
        save_engulf_cache(_hits, _tfs)          # so /engulf can answer instantly
        print(format_engulfing(_hits, _tfs).replace("*", ""))
        print(f"\n(cached to {ENGULF_CACHE.name} — Telegram /engulf reads this)")
    elif len(sys.argv) > 1 and sys.argv[1] in ("--engulf-brief", "--engulf-cached"):
        print(format_engulfing_brief())         # cached sweep, instant (no network)
    elif len(sys.argv) > 1 and sys.argv[1] == "--engulf-tg":
        # Telegram /engulf: scan live (~25s with the thread pool), refresh the
        # cache, print the compact view. Live beats cached here — the scan is
        # quick enough that a stale answer would be the worse trade-off.
        _tfs = [sys.argv[2]] if len(sys.argv) > 2 else ENGULF_TFS
        save_engulf_cache(scan_engulfing(_tfs), _tfs)
        print(format_engulfing_brief())
    elif len(sys.argv) > 1 and sys.argv[1] in ("--perf", "--performance"):
        print((format_breakout_performance() or "No auto breakouts logged yet.").replace("*", ""))
    else:
        main()
