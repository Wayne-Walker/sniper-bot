#!/usr/bin/env python3
"""
sol-scanner weekly review — is the survivor scanner calibrated?
═════════════════════════════════════════════════════════════════════════════

The scanner's gates (SCAN_* in .env) were opening guesses, never validated
against outcomes. This reads its journal and answers the only question that
matters early on: *which gate is doing the rejecting, and is it the right one?*

Reports, for the last REVIEW_DAYS (default 7):
  • volume      — verdicts, pass rate, split by venue
  • gate autopsy— how many candidates each gate killed (a gate that kills
                  ~everything is either miscalibrated or masking the others)
  • near-misses — failed EXACTLY ONE gate, and by how much. This is the
                  actionable list: it names the threshold to move and the
                  size of the move.
  • passes      — what actually got through, with metrics
  • health      — rate limits / errors / stale journal. A scanner that has
                  silently stopped scanning looks identical to a quiet market
                  unless you check, so we check.

Then hands the numbers to Claude (subscription, API credentials stripped —
same path as exhaustion_watch's reviews) for a calibration read, and pushes to
the private Telegram chat. If Claude is unavailable the raw stats still go out,
so a weekly review never silently produces nothing.

Usage:
  python sol_scanner_review.py            # review + Telegram push
  python sol_scanner_review.py --dry      # print only, no push
  python sol_scanner_review.py --days 14  # widen the window
"""

import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

import exhaustion_watch as ew  # send_telegram_private / _run_claude / CLAUDE_BIN

ROOT     = Path(__file__).resolve().parent
JOURNAL  = ROOT / "reports" / "sol_scanner_journal.jsonl"
STATE    = ROOT / "reports" / "sol_scanner_state.json"
DAYS     = float(os.getenv("SOL_REVIEW_DAYS", "7"))

# Gate label ← substring of the reason string emitted by token.scorer.ts
GATES = [
    ("liquidity",        "liquidity $"),
    ("LP draining",      "draining"),
    ("holder count",     "holders <"),
    ("top holders",      "top holders"),
    ("organic score",    "organic score"),
    ("serial dev",       "dev has minted"),
    ("too young",        "too young"),
    ("too old",          "too old"),
    ("mint authority",   "mint authority"),
    ("freeze authority", "freeze authority"),
    ("LP locked",        "LP locked"),
    # legacy labels — Helius/RugCheck era, kept so the archived journal
    # (reports/sol_scanner_journal.helius.jsonl) still reads correctly
    ("risk score",       "risk score"),
    ("top-10 concentr.", "top 10 hold"),
    ("insiders",         "insiders hold"),
    ("rugged flag",      "flagged as RUGGED"),
]


def classify(reason: str) -> str:
    for label, needle in GATES:
        if needle in reason:
            return label
    return "other"


def load(days: float) -> list[dict]:
    if not JOURNAL.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for line in JOURNAL.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
            if datetime.fromisoformat(e["ts"].replace("Z", "+00:00")) >= cutoff:
                out.append(e)
        except Exception:
            continue
    return out


def health() -> list[str]:
    """Distinguish 'quiet market' from 'scanner is dead'."""
    lines = []
    if not JOURNAL.exists():
        lines.append("⚠️ journal file does not exist — has the scanner ever run?")
        return lines
    age_h = (datetime.now(timezone.utc).timestamp() - JOURNAL.stat().st_mtime) / 3600
    if age_h > 48:
        lines.append(f"⚠️ journal last written {age_h:.0f}h ago — scanner may be down "
                     f"(`pm2 status sol-scanner`)")
    if STATE.exists():
        try:
            st = json.loads(STATE.read_text())
            pend = len(st.get("pending", []))
            lines.append(f"watchlist: {pend} pending · {len(st.get('seen', []))} seen")
            if pend >= 4500:
                lines.append("⚠️ watchlist near cap — candidates are being dropped")
        except Exception:
            pass
    return lines


def build(entries: list[dict], days: float) -> tuple[str, str]:
    """Returns (telegram_stats_block, claude_prompt)."""
    now = datetime.now(timezone.utc)
    hdr = f"🔎 *SOL-SCANNER REVIEW* ({now.strftime('%Y-%m-%d')})"

    if not entries:
        body = [hdr, f"_no verdicts in the last {days:.0f}d_", ""]
        body += health()
        body += ["", "_Nothing has matured through the gates yet. Expected early — "
                 "the gates are strict and unvalidated._"]
        return "\n".join(body), ""

    n       = len(entries)
    passes  = [e for e in entries if e.get("status") == "pass"]
    fails   = [e for e in entries if e.get("status") == "fail"]
    limited = [e for e in entries if e.get("status") == "rate_limited"]
    errored = [e for e in entries if e.get("status") == "error"]

    venues = Counter(e.get("source", "?") for e in entries)

    # Gate autopsy — every gate that fired, across all failures.
    gate_hits = Counter()
    for e in fails:
        for r in e.get("reasons", []):
            gate_hits[classify(r)] += 1

    # Near-misses: failed exactly one gate.
    near = [e for e in fails if len(e.get("reasons", [])) == 1]
    near_by_gate = Counter(classify(e["reasons"][0]) for e in near)

    L = [hdr,
         f"_{n} verdicts · {days:.0f}d · {len(passes)} passed ({100*len(passes)/n:.1f}%)_",
         "",
         "*Venues*  " + " · ".join(f"{k} {v}" for k, v in venues.most_common()),
         ""]

    if gate_hits:
        L.append("*Gate autopsy* — kills per gate")
        for g, c in gate_hits.most_common(6):
            L.append(f"  {g}: {c} ({100*c/max(len(fails),1):.0f}% of fails)")
        L.append("")

    if near:
        L.append(f"*Near-misses* — failed ONE gate only ({len(near)})")
        for g, c in near_by_gate.most_common(5):
            L.append(f"  {g}: {c}")
        L.append("  _these name the threshold worth moving_")
        L.append("")

    if passes:
        L.append("*Passed*")
        for e in passes[:8]:
            m = e.get("metrics", {})
            L.append(f"  ${e.get('symbol','?')} · liq ${m.get('liquidityUsd',0):,.0f} · "
                     f"{m.get('holders',0)} hldrs · LP {m.get('lpLockedPct',0):.0f}%")
        L.append("")

    h = health()
    if limited or errored:
        h.append(f"⚠️ {len(limited)} rate-limited · {len(errored)} errored "
                 f"(these are NOT rejections — candidates were deferred/lost)")
    if h:
        L += ["*Health*"] + [f"  {x}" for x in h] + [""]

    stats = "\n".join(L)

    prompt = (
        "You are reviewing a Solana token scanner's weekly journal. It watches new "
        "pools and alerts only on tokens that SURVIVED a 15-minute maturity window "
        "and then passed safety gates. The gate thresholds were opening guesses and "
        "have never been validated — your job is to say whether they look calibrated.\n\n"
        "Write a SHORT Telegram review (Markdown), exactly this structure:\n"
        "*Verdict:* <1-2 sentences — is this calibrated, too strict, or too loose?>\n"
        "*Read* — 2-3 bullets on what the gate autopsy and near-misses imply\n"
        "*Suggested change* — at most 2 concrete SCAN_* threshold changes with old→new "
        "values, or say explicitly that there is not enough data yet to change anything\n"
        "End with one italic caveat line.\n\n"
        "Be honest and conservative: with a small sample the correct answer is usually "
        "'not enough data — leave it alone'. Do NOT invent numbers beyond those given. "
        "A 0% pass rate early on is expected, not necessarily a fault.\n\n"
        f"Window: {days:.0f} days\n"
        f"Verdicts: {n} (pass {len(passes)}, fail {len(fails)}, "
        f"rate_limited {len(limited)}, error {len(errored)})\n"
        f"Venues: {dict(venues)}\n"
        f"Gate kills: {dict(gate_hits)}\n"
        f"Near-misses (failed one gate only): {dict(near_by_gate)}\n"
        f"Current thresholds: age {os.getenv('SCAN_MIN_AGE_MINUTES','15')}-{os.getenv('SCAN_MAX_AGE_MINUTES','180')}m, "
        f"liq>=${os.getenv('SCAN_MIN_LIQUIDITY_USD','15000')}, "
        f"holders>={os.getenv('SCAN_MIN_HOLDERS','75')}, "
        f"topHolders<={os.getenv('SCAN_MAX_TOP_HOLDERS_PCT','25')}%, "
        f"organicScore>={os.getenv('SCAN_MIN_ORGANIC_SCORE','40')}, "
        f"liqChange1h>={os.getenv('SCAN_MIN_LIQ_CHANGE_PCT','-25')}%, "
        f"devMints<={os.getenv('SCAN_MAX_DEV_MINTS','50')}\n"
    )
    return stats, prompt


def main() -> int:
    days = DAYS
    if "--days" in sys.argv:
        days = float(sys.argv[sys.argv.index("--days") + 1])
    dry = "--dry" in sys.argv

    entries = load(days)
    stats, prompt = build(entries, days)

    review = ew._run_claude(prompt) if prompt else None
    msg = stats + ("\n" + review if review else
                   "\n_(calibration read unavailable — raw stats only)_")

    print(msg)
    if dry:
        return 0
    ok = ew.send_telegram_private(msg)
    print(f"\n[review] telegram: {'sent' if ok else 'NOT sent'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
