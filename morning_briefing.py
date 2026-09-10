#!/usr/bin/env python3
"""
Morning Briefing — Consolidated daily trading intelligence
═══════════════════════════════════════════════════════════

Runs the full smart money → wallet tracker pipeline and sends
a single consolidated Telegram message with today's trade signals.

Flow:
  1. Run hl-smartmoney edge scan → get divergence signals
  2. For each signal, run hl-wallet-tracker coin report → validate
  3. Consolidate into one Telegram briefing
  4. Save briefing to reports/

Usage:
  python morning_briefing.py          # run once
  pm2 start ecosystem.config.js       # run on schedule
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests


# ── Paths ─────────────────────────────────────────────────────────────────────

ROOT         = Path(__file__).resolve().parent
SMARTMONEY   = ROOT / "hl-smartmoney"
TRACKER      = ROOT / "hl-wallet-tracker"
VENV_PYTHON  = ROOT / "venv" / "bin" / "python"
REPORTS_DIR  = ROOT / "reports"
REPORTS_DIR.mkdir(exist_ok=True)

# Make src/ importable when running this script directly
sys.path.insert(0, str(ROOT))
from src.data.market_pulse import (
    fetch_market_pulse, format_market_pulse,
    derive_market_regime, format_market_regime,
)
from src.data.etf_flows  import fetch_btc_etf_flows, format_etf_flows
from src.data.flow_history import process_run as process_flow_history

# ── Telegram (read from hl-momentum .env which has the creds) ─────────────────

def _load_telegram_creds() -> tuple:
    """
    Pull Telegram creds from the momentum .env (already configured).
    TELEGRAM_CHAT_ID can be a single ID or a comma-separated list to broadcast
    the briefing to multiple chats (e.g. "12345,67890,11111").
    Returns (token, [chat_id, ...]).
    """
    token, chat_id_raw = "", ""
    env_files = [
        ROOT / "hl-momentum" / ".env",
        ROOT / "hl-smartmoney" / ".env",
    ]
    for env_file in env_files:
        if not env_file.exists():
            continue
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key, val = key.strip(), val.strip()
            if key == "TELEGRAM_BOT_TOKEN" and val:
                token = val
            elif key == "TELEGRAM_CHAT_ID" and val:
                chat_id_raw = val
        if token and chat_id_raw:
            break

    chat_ids = [cid.strip() for cid in chat_id_raw.split(",") if cid.strip()]
    return token, chat_ids


TELEGRAM_TOKEN, TELEGRAM_CHAT_IDS = _load_telegram_creds()


def _split_for_telegram(message: str, max_len: int = 4000) -> list:
    """
    Split on line boundaries so chunks never break a Markdown entity
    (e.g. mid-way through *bold* or `code`). Falls back to a hard split
    only if a single line itself exceeds max_len.
    """
    chunks, buf = [], ""
    for line in message.splitlines(keepends=True):
        if len(line) > max_len:
            # Pathological case — flush buf, then hard-split the long line
            if buf:
                chunks.append(buf)
                buf = ""
            for i in range(0, len(line), max_len):
                chunks.append(line[i:i + max_len])
            continue
        if len(buf) + len(line) > max_len:
            chunks.append(buf)
            buf = line
        else:
            buf += line
    if buf:
        chunks.append(buf)
    return chunks


def _send_to_chat(chat_id: str, chunk: str, parse_mode: str = "Markdown") -> bool:
    """
    Send one chunk to one chat. With a parse_mode set, falls back to plain
    text on parser errors. Pass parse_mode=None to send plain text directly.
    """
    try:
        payload = {"chat_id": chat_id, "text": chunk}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json=payload,
            timeout=10,
        )
        if resp.ok:
            return True
        print(f"  [WARN] Telegram API error (chat {chat_id}): "
              f"{resp.status_code} {resp.text[:200]}")
        # Markdown probably broke — retry plain text so user still gets the info
        resp2 = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": chunk},
            timeout=10,
        )
        if resp2.ok:
            return True
        print(f"  [WARN] Plain-text retry also failed (chat {chat_id}): "
              f"{resp2.status_code}")
        return False
    except Exception as e:
        print(f"  [WARN] Telegram send failed (chat {chat_id}): {e}")
        return False


def send_telegram(message: str, parse_mode: str = "Markdown") -> bool:
    """
    Broadcast message to all configured chat IDs.
    Returns True if at least one chat received the message successfully.
    Each chat is attempted independently — one failure doesn't abort others.
    Pass parse_mode=None to send as plain text (no Markdown parsing).
    """
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_IDS:
        print("  [WARN] Telegram not configured — skipping alert")
        return False

    chunks = _split_for_telegram(message, max_len=4000)
    delivered_to = []
    failed_for = []

    for chat_id in TELEGRAM_CHAT_IDS:
        all_chunks_ok = True
        for chunk in chunks:
            if not _send_to_chat(chat_id, chunk, parse_mode=parse_mode):
                all_chunks_ok = False
                break  # stop sending to this chat, move to next
        if all_chunks_ok:
            delivered_to.append(chat_id)
        else:
            failed_for.append(chat_id)

    if delivered_to:
        print(f"  Telegram delivered to {len(delivered_to)}/"
              f"{len(TELEGRAM_CHAT_IDS)} chat(s): {', '.join(delivered_to)}")
    if failed_for:
        print(f"  Telegram failed for: {', '.join(failed_for)}")

    return bool(delivered_to)


# ── Step 1: Run smart money edge scan ─────────────────────────────────────────

def run_edge_scan() -> list:
    """
    Run hl-smartmoney edge scan and return list of TradeSignal dicts.
    Reads from the saved signals/latest.json for structured data.
    """
    print("\n" + "=" * 65)
    print("  STEP 1 — Smart Money Edge Scan")
    print("=" * 65)

    result = subprocess.run(
        [str(VENV_PYTHON), "main.py", "edge"],
        cwd=str(SMARTMONEY),
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "SUPPRESS_EDGE_ALERT": "1"},
    )

    # Print the output so we can see it in logs
    if result.stdout:
        print(result.stdout[-2000:])  # last 2000 chars
    if result.returncode != 0 and result.stderr:
        print(f"  [ERROR] Edge scan failed:\n{result.stderr[-500:]}")
        return []

    # Read structured signals from JSON
    signals_file = SMARTMONEY / "signals" / "latest.json"
    if not signals_file.exists():
        print("  No signals file found — likely no signals above threshold")
        return []

    try:
        data = json.loads(signals_file.read_text())
        signals = data.get("signals", [])
        print(f"\n  Found {len(signals)} signal(s) above threshold")
        return signals
    except Exception as e:
        print(f"  [ERROR] Failed to read signals JSON: {e}")
        return []


# ── Step 2: Validate signals with wallet tracker ─────────────────────────────

def validate_signal(coin: str) -> dict:
    """
    Run hl-wallet-tracker coin report for a specific coin.
    Returns parsed stats or empty dict.
    """
    print(f"\n  Validating ${coin} with wallet tracker...")

    result = subprocess.run(
        [str(VENV_PYTHON), "main.py", "coin", coin],
        cwd=str(TRACKER),
        capture_output=True,
        text=True,
        timeout=300,  # wallet tracker can be slow with many wallets
    )

    output = result.stdout or ""

    # Parse key stats from the output
    stats = {
        "coin": coin,
        "raw_output": output,
        "trade_count": 0,
        "wallets_trading": 0,
    }

    # Count trades from output lines (each line with wallet address is a trade)
    trade_lines = [
        l for l in output.splitlines()
        if l.strip() and "0x" in l and ("LONG" in l.upper() or "SHORT" in l.upper() or "▲" in l or "▼" in l)
    ]
    stats["trade_count"] = len(trade_lines)

    # Extract unique wallets
    wallets_seen = set()
    for line in trade_lines:
        parts = line.strip().split()
        if parts and parts[0].startswith("0x"):
            wallets_seen.add(parts[0][:10])
    stats["wallets_trading"] = len(wallets_seen)

    if result.returncode != 0 and result.stderr:
        print(f"    [WARN] Tracker error: {result.stderr[-200:]}")

    print(f"    Found {stats['trade_count']} trades from {stats['wallets_trading']} wallets")
    return stats


# ── Step 3: Get entry clusters ────────────────────────────────────────────────

def get_entry_clusters(coin: str) -> dict:
    """
    Run hl-smartmoney coin report to get average entry prices.
    Returns dict with smart/dumb money avg entry prices.
    """
    print(f"  Getting entry data for ${coin}...")

    result = subprocess.run(
        [str(VENV_PYTHON), "main.py", coin],
        cwd=str(SMARTMONEY),
        capture_output=True,
        text=True,
        timeout=120,
    )

    output = result.stdout or ""
    lines = output.splitlines()

    entry_data = {
        "smart_long_entry": None,
        "smart_short_entry": None,
        "dumb_long_entry": None,
        "dumb_short_entry": None,
    }

    # Parse avg entry prices from the coin report
    # Format: "  Long  →  N wallets  $X  avg lev: Xx  avg entry: $Y"
    in_smart = False
    in_dumb = False
    for line in lines:
        upper = line.upper()
        if "SMART MONEY" in upper or "WINNER" in upper:
            in_smart = True
            in_dumb = False
        elif "DUMB MONEY" in upper or "LOSER" in upper:
            in_dumb = True
            in_smart = False

        if "avg entry:" in line.lower():
            try:
                entry_str = line.lower().split("avg entry:")[1].strip()
                entry_price = float(entry_str.replace("$", "").replace(",", ""))
                is_long = "long" in upper or "▲" in line

                if in_smart and is_long:
                    entry_data["smart_long_entry"] = entry_price
                elif in_smart and not is_long:
                    entry_data["smart_short_entry"] = entry_price
                elif in_dumb and is_long:
                    entry_data["dumb_long_entry"] = entry_price
                elif in_dumb and not is_long:
                    entry_data["dumb_short_entry"] = entry_price
            except (ValueError, IndexError):
                pass

    return entry_data


# ── Build the briefing ────────────────────────────────────────────────────────

def build_briefing(
    signals: list,
    validations: dict,
    clusters: dict,
    regime_text: str = "",
    market_pulse_text: str = "",
    etf_flows_text: str = "",
    shift_text: str = "",
) -> str:
    """Build the consolidated Telegram message."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        f"*MORNING BRIEFING* — {now}",
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
    ]

    # Market shift first — the most time-sensitive read (the turn)
    if shift_text:
        lines.append(shift_text)
        lines.append("")

    # Regime verdict next — the headline read on the market
    if regime_text:
        lines.append(regime_text)
        lines.append("")
    if market_pulse_text:
        lines.append(market_pulse_text)
        lines.append("")
    if etf_flows_text:
        lines.append(etf_flows_text)
        lines.append("")

    if not signals:
        lines.append("No signals above threshold today. Market is neutral or aligned.")
        lines.append("")
        lines.append("_Check back tomorrow or lower_ `EDGE_THRESHOLD` _in .env_")
        return "\n".join(lines)

    lines.append(f"*{len(signals)} SIGNAL(S) DETECTED*")
    lines.append("")

    for i, sig in enumerate(signals, 1):
        coin      = sig["coin"]
        direction = sig["direction"].upper()
        gap       = sig["gap_pct"]
        mark      = sig["mark_price"]
        sentiment = sig["net_sentiment"]
        arrow     = "🟢" if direction == "LONG" else "🔴"

        smart_long = sig.get("winner_long_pct", 0)
        dumb_long  = sig.get("loser_long_pct", 0)
        smart_lev  = sig.get("winner_avg_leverage", 0)
        dumb_lev   = sig.get("loser_avg_leverage", 0)
        smart_net  = sig.get("winner_net_notional", 0)
        dumb_net   = sig.get("loser_net_notional", 0)

        lines.append(f"{arrow} *[{i}] ${coin} — {direction}*")
        lines.append(f"  Gap: `{gap:.1f}%`  |  Mark: `${mark:,.4f}`")
        lines.append(f"  {sentiment}")
        lines.append(f"  Smart: `{smart_long:.0f}%` long, `{smart_lev:.1f}x` lev, net `${smart_net:+,.0f}`")
        lines.append(f"  Dumb:  `{dumb_long:.0f}%` long, `{dumb_lev:.1f}x` lev, net `${dumb_net:+,.0f}`")

        # Add validation data
        val = validations.get(coin, {})
        if val.get("trade_count", 0) > 0:
            lines.append(f"  📊 Tracker: `{val['trade_count']}` trades from `{val['wallets_trading']}` smart wallets")
        else:
            lines.append(f"  ⚠️ No historical trades found for this coin")

        # Add average entry prices
        entry = clusters.get(coin, {})
        if entry:
            if direction == "LONG":
                smart_entry = entry.get("smart_long_entry")
                dumb_entry = entry.get("dumb_short_entry")
            else:
                smart_entry = entry.get("smart_short_entry")
                dumb_entry = entry.get("dumb_long_entry")

            entry_parts = []
            if smart_entry:
                entry_parts.append(f"Smart avg entry: `${smart_entry:,.4f}`")
            if dumb_entry:
                opp = "short" if direction == "LONG" else "long"
                entry_parts.append(f"Dumb avg {opp}: `${dumb_entry:,.4f}`")
            if entry_parts:
                lines.append(f"  📍 {' | '.join(entry_parts)}")

        lines.append("")

    # Trading rules reminder
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("*RULES*")
    lines.append("• Only trade gap >60% + wallet win rate >60%")
    lines.append("• Leverage: 2-3x max (match smart money)")
    lines.append("• TP: 4-5% | SL: 1.5-2% | Time stop: 6-8h")
    lines.append("• Validate entry zone before entering")

    return "\n".join(lines)


# ── Save briefing to file ─────────────────────────────────────────────────────

def save_briefing(briefing: str) -> str:
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = REPORTS_DIR / f"briefing_{ts}.txt"
    filepath.write_text(briefing, encoding="utf-8")

    # Also save as latest
    latest = REPORTS_DIR / "briefing_latest.txt"
    latest.write_text(briefing, encoding="utf-8")

    return str(filepath)


# ── Main ──────────────────────────────────────────────────────────────────────

def fetch_macro_context() -> tuple:
    """
    Fetch market pulse + ETF flows and synthesize a regime verdict.
    Returns (shift_text, regime_text, pulse_text, etf_text).
    """
    print("\n" + "=" * 65)
    print("  STEP 0 — Macro Context (stablecoins, market, F&G, ETF flows)")
    print("=" * 65)

    pulse, flows, regime = None, None, None
    pulse_text, etf_text, regime_text, shift_text = "", "", "", ""

    try:
        pulse = fetch_market_pulse()
        pulse_text = format_market_pulse(pulse)
    except Exception as e:
        print(f"  [WARN] Market pulse failed: {e}")

    try:
        flows = fetch_btc_etf_flows()
        etf_text = format_etf_flows(flows)
    except Exception as e:
        print(f"  [WARN] BTC ETF flows failed: {e}")

    # Synthesize accumulation/distribution verdict from whatever we got
    if pulse is not None:
        try:
            regime = derive_market_regime(pulse, etf_flows=flows)
            regime_text = format_market_regime(regime)
            print(f"  Regime: {regime.label} (score {regime.score:+d}) — {regime.narrative}")
        except Exception as e:
            print(f"  [WARN] Regime synthesis failed: {e}")

    # Detect day-over-day shifts (flips, decel, regime momentum) and persist
    try:
        shift_text = process_flow_history(pulse, flows, regime)
        if shift_text:
            print("  ⚡ Market shift(s) detected vs prior day")
    except Exception as e:
        print(f"  [WARN] Shift detection failed: {e}")

    return shift_text, regime_text, pulse_text, etf_text


def main():
    print("\n" + "█" * 65)
    print("  MORNING BRIEFING — Hyperliquid Smart Money Trading System")
    print("  " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("█" * 65)

    # Step 0: Macro context (free data — stablecoin flows, market, F&G, ETF flows)
    shift_text, regime_text, market_pulse_text, etf_flows_text = fetch_macro_context()

    # Step 1: Edge scan
    signals = run_edge_scan()

    # Step 2: Validate each signal with wallet tracker
    print("\n" + "=" * 65)
    print("  STEP 2 — Wallet Tracker Validation")
    print("=" * 65)

    validations = {}
    for sig in signals:
        coin = sig["coin"]
        validations[coin] = validate_signal(coin)

    # Step 3: Get entry clusters for validated signals
    print("\n" + "=" * 65)
    print("  STEP 3 — Entry Cluster Analysis")
    print("=" * 65)

    clusters = {}
    for sig in signals:
        coin = sig["coin"]
        clusters[coin] = get_entry_clusters(coin)

    # Build consolidated briefing
    print("\n" + "=" * 65)
    print("  STEP 4 — Sending Briefing")
    print("=" * 65)

    briefing = build_briefing(
        signals, validations, clusters,
        regime_text=regime_text,
        market_pulse_text=market_pulse_text,
        etf_flows_text=etf_flows_text,
        shift_text=shift_text,
    )

    # Save to file
    saved = save_briefing(briefing)
    print(f"\n  Briefing saved → {saved}")

    # Print to console
    print("\n" + "─" * 65)
    print(briefing)
    print("─" * 65)

    # Send to Telegram
    if send_telegram(briefing):
        print("\n  ✅ Telegram briefing sent!")
    else:
        print("\n  ⚠️  Telegram send failed — check credentials")

    print("\n  Done.\n")


if __name__ == "__main__":
    main()
