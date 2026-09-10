#!/usr/bin/env python3
"""
Flow Alert — Intraday macro-shift watcher.
═══════════════════════════════════════════

Runs the macro pulse + ETF + regime fetch, diffs today against the prior day,
and fires a Telegram alert the *moment* a market shift is detected — so a turn
(stablecoin/ETF outflows flipping to inflows, outflows slowing, regime crossing
into accumulation) doesn't wait until the morning briefing.

Designed to run frequently (e.g. hourly via pm2 cron). To avoid spamming the
same turn every hour, each shift category alerts at most once per calendar day
(tracked in reports/flow_alerts_sent.json).

Usage:
  python flow_alert.py                 # run once
  pm2 start ecosystem.config.js        # run on schedule
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.data.market_pulse import fetch_market_pulse, derive_market_regime
from src.data.etf_flows    import fetch_btc_etf_flows
from src.data.flow_history import (
    build_snapshot, load_history, save_snapshot,
    detect_shifts, format_shifts, _latest_prior,
)
from morning_briefing import send_telegram

SENT_FILE = ROOT / "reports" / "flow_alerts_sent.json"


def _load_sent(today: str) -> set:
    """Keys already alerted today (reset when the date rolls over)."""
    if not SENT_FILE.exists():
        return set()
    try:
        data = json.loads(SENT_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    if data.get("date") != today:
        return set()
    return set(data.get("keys", []))


def _save_sent(today: str, keys: set) -> None:
    SENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    SENT_FILE.write_text(
        json.dumps({"date": today, "keys": sorted(keys)}),
        encoding="utf-8",
    )


def main() -> None:
    print("\n" + "=" * 60)
    print("  FLOW ALERT — intraday macro-shift watcher")
    print("  " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 60)

    pulse, flows, regime = None, None, None
    try:
        pulse = fetch_market_pulse()
    except Exception as e:
        print(f"  [WARN] Market pulse failed: {e}")
    try:
        flows = fetch_btc_etf_flows()
    except Exception as e:
        print(f"  [WARN] BTC ETF flows failed: {e}")
    if pulse is not None:
        try:
            regime = derive_market_regime(pulse, etf_flows=flows)
        except Exception as e:
            print(f"  [WARN] Regime synthesis failed: {e}")

    snap    = build_snapshot(pulse, flows, regime)
    history = load_history()
    prev    = _latest_prior(history, snap["date"])
    shifts  = detect_shifts(snap, history)
    save_snapshot(snap)  # persist regardless, so tomorrow has a baseline

    if not shifts:
        print("  No shifts vs prior day — nothing to alert.")
        return

    today     = snap["date"]
    sent_keys = _load_sent(today)
    new       = [s for s in shifts if s.key not in sent_keys]

    if not new:
        print(f"  {len(shifts)} shift(s) detected, all already alerted today — skipping.")
        return

    now_utc = datetime.now(timezone.utc).strftime("%H:%M UTC")
    header  = f"*⚡ FLOW ALERT* — {now_utc}"
    body    = format_shifts(new, prev_date=prev.get("date", "") if prev else "")
    # format_shifts emits its own "MARKET SHIFT" header; replace with the alert one
    body    = "\n".join(body.split("\n")[1:])  # drop the first header line
    message = f"{header}\n{body}"

    print(f"  Sending {len(new)} new shift(s):")
    for s in new:
        print(f"    {s.emoji} [{s.key}] {s.text}")

    if send_telegram(message):
        _save_sent(today, sent_keys | {s.key for s in new})
        print("  ✅ Alert sent.")
    else:
        print("  ⚠️  Telegram send failed — not marking as sent (will retry next run).")


if __name__ == "__main__":
    main()
