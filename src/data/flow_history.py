"""
Flow History — Persist daily macro snapshots and detect *transitions*.

The market pulse / ETF / regime modules each read the market's *current state*.
This module gives the briefing a memory: it appends one snapshot per run to
`reports/flow_history.jsonl`, then diffs today against the most recent prior
day to surface the things a stateless snapshot can't see —

  • Sign flips ........ outflow → inflow (the turn itself)
  • Deceleration ...... still bleeding, but less than before (selling exhausting)
  • Streak breaks ..... a multi-day outflow run ending
  • Regime momentum ... the accumulation/distribution score trending

These "shift" signals are the highest-value, most time-sensitive reads in the
briefing, so they render above the regime block.

Units note: stablecoin deltas are stored in raw USD; ETF flows in US$ millions
(matching their source modules). Formatting helpers handle both.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

from src.utils.logger import get_logger

log = get_logger(__name__)

# src/data/flow_history.py → parents[2] == project root
HISTORY_FILE = Path(__file__).resolve().parents[2] / "reports" / "flow_history.jsonl"

# ── Detection thresholds ──────────────────────────────────────────────────────
SC_FLIP_USD   = 200_000_000   # min |7d stablecoin flow| each side to call a flip
SC_SLOW_USD   = 250_000_000   # min improvement to call outflows "slowing"/cooling
USDT_FLIP_USD = 200_000_000   # min |1d USDT flow| each side for a USDT-specific flip
ETF_FLIP_M    = 50.0          # min |7d ETF flow| ($M) each side to call a flip
ETF_DECEL_M   = 300.0         # min ETF 7d improvement ($M) to call outflows slowing
REGIME_JUMP   = 3             # regime score swing to call macro momentum


# ── Snapshot ──────────────────────────────────────────────────────────────────

def build_snapshot(pulse: Any, flows: Any, regime: Any) -> dict:
    """
    Extract the comparable metrics from this run's macro objects.
    Any input may be None (a source can fail independently) — missing
    fields are stored as None and skipped during detection.
    """
    now = datetime.now(timezone.utc)
    snap: dict = {
        "date": now.strftime("%Y-%m-%d"),
        "ts":   now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sc_7d":        None,
        "sc_1d":        None,
        "usdt_1d":      None,
        "etf_latest":   None,
        "etf_7d":       None,
        "etf_streak":   None,
        "regime_score": None,
        "fg":           None,
    }

    if pulse is not None:
        sc = getattr(pulse, "stablecoins", None)
        if sc is not None:
            snap["sc_7d"]   = float(getattr(sc, "mcap_7d_change", 0) or 0)
            snap["sc_1d"]   = float(getattr(sc, "mcap_1d_change", 0) or 0)
            snap["usdt_1d"] = float(getattr(sc, "usdt_1d_change", 0) or 0)
        fg = getattr(pulse, "fear_greed", None)
        if fg is not None and getattr(fg, "value", 0):
            snap["fg"] = int(fg.value)

    if flows is not None and getattr(flows, "days", None):
        snap["etf_latest"] = float(getattr(flows, "latest_total", 0) or 0)
        snap["etf_7d"]     = float(getattr(flows, "sum_7d", 0) or 0)
        snap["etf_streak"] = int(getattr(flows, "consecutive", 0) or 0)

    if regime is not None:
        snap["regime_score"] = int(getattr(regime, "score", 0) or 0)

    return snap


def load_history(path: Path = HISTORY_FILE) -> List[dict]:
    """Read all snapshots (oldest → newest). Skips malformed lines."""
    if not path.exists():
        return []
    out: List[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            log.warning("Skipping malformed flow_history line")
    return out


def save_snapshot(snap: dict, path: Path = HISTORY_FILE) -> None:
    """
    Append today's snapshot, keeping at most one entry per date (the latest
    run of the day wins). Rewrites the file so re-runs don't pollute history.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    history = [s for s in load_history(path) if s.get("date") != snap["date"]]
    history.append(snap)
    with path.open("w", encoding="utf-8") as f:
        for s in history:
            f.write(json.dumps(s) + "\n")


def _latest_prior(history: List[dict], today_date: str) -> Optional[dict]:
    """Most recent snapshot from a *different* date than today."""
    for s in reversed(history):
        if s.get("date") != today_date:
            return s
    return None


# ── Shift detection ───────────────────────────────────────────────────────────

@dataclass
class Shift:
    emoji:    str
    text:     str
    severity: int = 1   # 3 = a turn (flip), 2 = decel / regime, 1 = cooling
    key:      str = ""  # stable category id, for once-per-day alert dedup


def _fmt_usd(v: float) -> str:
    if abs(v) >= 1e9:
        return f"${v/1e9:+.1f}B"
    return f"${v/1e6:+,.0f}M"


def _fmt_m(v: float) -> str:
    """Format a value already in US$ millions."""
    if abs(v) >= 1000:
        return f"${v/1000:+.1f}B"
    return f"${v:+,.0f}M"


def detect_shifts(today: dict, history: List[dict]) -> List[Shift]:
    """
    Diff today's snapshot against the most recent prior day. Returns an
    ordered list of Shift signals (empty until at least one prior day exists).
    """
    prev = _latest_prior(history, today.get("date", ""))
    if prev is None:
        return []

    shifts: List[Shift] = []

    # ── Stablecoins — total 7d flow (smooth) ─────────────────────────────
    s_now, s_old = today.get("sc_7d"), prev.get("sc_7d")
    if s_now is not None and s_old is not None:
        if s_old < -SC_FLIP_USD and s_now > SC_FLIP_USD:
            shifts.append(Shift("🟢", f"Stablecoin 7d flow flipped positive "
                                      f"({_fmt_usd(s_old)} → {_fmt_usd(s_now)}) — capital returning to crypto", 3, "sc_flip_pos"))
        elif s_old > SC_FLIP_USD and s_now < -SC_FLIP_USD:
            shifts.append(Shift("🔴", f"Stablecoin 7d flow flipped negative "
                                      f"({_fmt_usd(s_old)} → {_fmt_usd(s_now)}) — capital leaving crypto", 3, "sc_flip_neg"))
        elif s_old < 0 and s_now < 0 and (s_now - s_old) >= SC_SLOW_USD:
            shifts.append(Shift("🟡", f"Stablecoin outflows slowing "
                                      f"(7d {_fmt_usd(s_old)} → {_fmt_usd(s_now)}) — selling pressure easing", 2, "sc_decel"))
        elif s_old > 0 and s_now > 0 and (s_old - s_now) >= SC_SLOW_USD:
            shifts.append(Shift("🟡", f"Stablecoin inflows cooling "
                                      f"(7d {_fmt_usd(s_old)} → {_fmt_usd(s_now)}) — dry powder building slower", 1, "sc_cool"))

    # ── USDT-specific 1d flip ────────────────────────────────────────────
    u_now, u_old = today.get("usdt_1d"), prev.get("usdt_1d")
    if u_now is not None and u_old is not None:
        if u_old < -USDT_FLIP_USD and u_now > USDT_FLIP_USD:
            shifts.append(Shift("🟢", f"USDT printed positive 1d "
                                      f"({_fmt_usd(u_old)} → {_fmt_usd(u_now)}) — fresh USDT minting", 3, "usdt_flip_pos"))
        elif u_old > USDT_FLIP_USD and u_now < -USDT_FLIP_USD:
            shifts.append(Shift("🔴", f"USDT redeemed 1d "
                                      f"({_fmt_usd(u_old)} → {_fmt_usd(u_now)}) — USDT burning", 3, "usdt_flip_neg"))

    # ── BTC ETF — streak flip (the turn) ─────────────────────────────────
    c_now, c_old = today.get("etf_streak"), prev.get("etf_streak")
    if c_now is not None and c_old is not None:
        if c_old < 0 and c_now > 0:
            shifts.append(Shift("🟢", f"BTC ETF flipped to inflows "
                                      f"— ended a {abs(c_old)}-day outflow streak", 3, "etf_flip_pos"))
        elif c_old > 0 and c_now < 0:
            shifts.append(Shift("🔴", f"BTC ETF flipped to outflows "
                                      f"— ended a {c_old}-day inflow streak", 3, "etf_flip_neg"))
        elif c_old <= -2 and c_now == 0:
            shifts.append(Shift("🟡", f"BTC ETF {abs(c_old)}-day outflow streak broke — selling paused", 2, "etf_streak_break"))

    # ── BTC ETF — 7d deceleration ────────────────────────────────────────
    e_now, e_old = today.get("etf_7d"), prev.get("etf_7d")
    if e_now is not None and e_old is not None:
        if e_old < -ETF_FLIP_M and e_now > ETF_FLIP_M:
            shifts.append(Shift("🟢", f"BTC ETF 7d cumulative flipped positive "
                                      f"({_fmt_m(e_old)} → {_fmt_m(e_now)})", 3, "etf_7d_flip_pos"))
        elif e_old < 0 and e_now < 0 and (e_now - e_old) >= ETF_DECEL_M:
            shifts.append(Shift("🟡", f"BTC ETF outflows slowing "
                                      f"(7d {_fmt_m(e_old)} → {_fmt_m(e_now)}) — institutional selling easing", 2, "etf_decel"))

    # ── Regime score momentum ────────────────────────────────────────────
    r_now, r_old = today.get("regime_score"), prev.get("regime_score")
    if r_now is not None and r_old is not None:
        diff = r_now - r_old
        if r_old <= 0 < r_now:
            shifts.append(Shift("🟢", f"Regime crossed into accumulation "
                                      f"(score `{r_old:+d}` → `{r_now:+d}`)", 3, "regime_cross_pos"))
        elif r_old >= 0 > r_now:
            shifts.append(Shift("🔴", f"Regime crossed into distribution "
                                      f"(score `{r_old:+d}` → `{r_now:+d}`)", 3, "regime_cross_neg"))
        elif diff >= REGIME_JUMP:
            shifts.append(Shift("🟢", f"Regime score rising fast "
                                      f"(`{r_old:+d}` → `{r_now:+d}`, {diff:+d}) — macro turning bullish", 2, "regime_up"))
        elif diff <= -REGIME_JUMP:
            shifts.append(Shift("🔴", f"Regime score falling fast "
                                      f"(`{r_old:+d}` → `{r_now:+d}`, {diff:+d}) — macro turning bearish", 2, "regime_down"))

    shifts.sort(key=lambda s: s.severity, reverse=True)
    return shifts


def format_shifts(shifts: List[Shift], prev_date: str = "") -> str:
    """Render the shift block, or "" when nothing changed."""
    if not shifts:
        return ""
    since = f" _(vs {prev_date})_" if prev_date else ""
    lines = [
        f"*⚡ MARKET SHIFT*{since}",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    for s in shifts:
        lines.append(f"  {s.emoji} {s.text}")
    return "\n".join(lines)


# ── One-call helper for the briefing ──────────────────────────────────────────

def process_run(pulse: Any, flows: Any, regime: Any,
                path: Path = HISTORY_FILE) -> str:
    """
    Build today's snapshot, detect shifts vs the prior day, persist, and
    return the formatted shift block (empty string if nothing to report).
    """
    snap = build_snapshot(pulse, flows, regime)
    history = load_history(path)
    prev = _latest_prior(history, snap["date"])
    shifts = detect_shifts(snap, history)
    save_snapshot(snap, path)

    if shifts:
        log.info(f"  Detected {len(shifts)} market shift(s) vs prior day")
    else:
        log.info("  No market shifts vs prior day (or no history yet)")

    return format_shifts(shifts, prev_date=prev.get("date", "") if prev else "")


# ── CLI test ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    hist = load_history()
    print(f"History entries: {len(hist)}")
    if hist:
        latest = hist[-1]
        shifts = detect_shifts(latest, hist[:-1])
        print(format_shifts(shifts) or "No shifts detected.")
