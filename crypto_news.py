#!/usr/bin/env python3
"""
Crypto News Scan — Twice-daily web-searched catalyst report
═══════════════════════════════════════════════════════════

Asks the Claude Code CLI (headless `claude -p`, using your existing
subscription — no API key) to web-search for price-moving news on the
coins in coins.js, then sends a single Telegram message.

Flow:
  1. Parse the watchlist from coins.js (site root)
  2. Run `claude -p "<prompt>" --allowedTools WebSearch`
  3. Save the report to reports/ and broadcast via Telegram

Usage:
  python crypto_news.py                 # run once
  pm2 start ecosystem.config.js         # run on schedule (07:30 / 16:30 UTC)
"""

import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Reuse Telegram delivery + reports dir from the morning briefing
# (importing it only loads helpers; it does not run main()).
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from morning_briefing import send_telegram, REPORTS_DIR  # noqa: E402

COINS_JS = ROOT / "coins.js"

# Web search can take a couple of minutes across several queries.
CLAUDE_TIMEOUT = 300  # seconds


# ── Watchlist ─────────────────────────────────────────────────────────────────

def load_watchlist() -> list:
    """
    Parse coin IDs out of coins.js → uppercased ticker symbols.
    Returns e.g. ["BTC", "ETH", ...]. Empty list if the file is unreadable.
    """
    try:
        text = COINS_JS.read_text(encoding="utf-8")
    except OSError as e:
        print(f"  [WARN] Could not read {COINS_JS}: {e}")
        return []
    ids = re.findall(r'coinId:\s*"([^"]+)"', text)
    return [c.upper() for c in ids]


# ── Build the search prompt ────────────────────────────────────────────────────

def build_prompt(symbols: list) -> str:
    coin_list = ", ".join(symbols)
    return (
        "You are a crypto market analyst. Use web search to produce a two-part "
        "report. Search for the latest data before writing.\n\n"
        "PART 1 — MARKET SUMMARY (write this first):\n"
        "- Read the overall crypto market state right now: is it selling off, "
        "rallying, or chop/ranging? Check total market cap 24h change, BTC and "
        "ETH price action, and Bitcoin's Fear & Greed Index.\n"
        "- Then explain WHY in 2-4 short sentences. Do NOT guess the cause from "
        "price action — run a web search for the LATEST market news/headlines "
        "(e.g. 'why is crypto market down/up today', macro events, Fed/rates/CPI, "
        "ETF flows, regulation, major liquidations) and base the explanation on "
        "what the news actually reports. Name the specific catalyst(s).\n"
        "- End the summary with one source URL for the main driver, on its own "
        "line prefixed 'Source: '.\n"
        "- Start this section with the line 'MARKET: <one-word read>' where the "
        "read is e.g. SELLING OFF, BULLISH, RISK-OFF, RANGING, RECOVERING.\n\n"
        "PART 2 — COIN CATALYSTS:\n"
        "Find notable, price-moving news or catalysts from the LAST 12-24 HOURS "
        "for any of these coins:\n\n"
        f"{coin_list}\n\n"
        "Rules:\n"
        "- Only list coins that actually have a real catalyst in this window "
        "(partnership/integration, listing, protocol/feature announcement, "
        "regulatory news, major whale flow, hack, etc.). Skip routine price chatter.\n"
        "- For each coin output ONE line in this exact format:\n"
        "  SYMBOL — the catalyst in plain language (include % move or $ size if known) — URL\n"
        "- Plain text only. NO markdown, NO asterisks, NO [text](url) links — "
        "just the bare URL (Telegram auto-links it).\n"
        "- Output ONLY the coins that have a catalyst. Do NOT list the coins "
        "with no news, and do not add any summary of what you skipped.\n\n"
        "FORMAT:\n"
        "- Put the market summary first, then a blank line and the divider line "
        "'CATALYSTS', then the coin lines.\n"
        "- Plain text throughout. Be tight and factual. No preamble before the "
        "MARKET line, no disclaimers, no closing remarks after the coin list."
    )


# ── Run the headless Claude web search ─────────────────────────────────────────

def fetch_news(symbols: list) -> str:
    """Shell out to the claude CLI in headless mode with WebSearch enabled."""
    claude_bin = shutil.which("claude") or str(Path.home() / ".local/bin/claude")

    print("\n" + "=" * 65)
    print(f"  Crypto News Scan — searching {len(symbols)} coins")
    print("=" * 65)

    result = subprocess.run(
        [claude_bin, "-p", build_prompt(symbols), "--allowedTools", "WebSearch"],
        capture_output=True,
        text=True,
        timeout=CLAUDE_TIMEOUT,
        cwd=str(ROOT),
    )

    if result.returncode != 0:
        print(f"  [ERROR] claude CLI failed (rc={result.returncode}): "
              f"{result.stderr[-500:]}")
        return ""

    body = (result.stdout or "").strip()

    # Strip any model preamble before the MARKET line (the report's true start).
    idx = body.find("MARKET:")
    if idx > 0:
        body = body[idx:]
    return body


# ── Assemble + persist ──────────────────────────────────────────────────────────

def build_report(news_body: str) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"CRYPTO NEWS — {now}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{news_body}"
    )


def save_report(report: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = REPORTS_DIR / f"news_{ts}.txt"
    filepath.write_text(report, encoding="utf-8")
    (REPORTS_DIR / "news_latest.txt").write_text(report, encoding="utf-8")
    return str(filepath)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("\n" + "█" * 65)
    print("  CRYPTO NEWS SCAN")
    print("  " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("█" * 65)

    symbols = load_watchlist()
    if not symbols:
        print("  [ERROR] No coins parsed from coins.js — aborting.")
        sys.exit(1)

    news_body = fetch_news(symbols)
    if not news_body:
        print("  [ERROR] No news returned — aborting (nothing sent).")
        sys.exit(1)

    report = build_report(news_body)
    saved = save_report(report)
    print(f"\n  Report saved → {saved}")

    print("\n" + "─" * 65)
    print(report)
    print("─" * 65)

    if send_telegram(report, parse_mode=None):
        print("\n  ✅ Telegram news report sent!")
    else:
        print("\n  ⚠️  Telegram send failed — check credentials")

    print("\n  Done.\n")


if __name__ == "__main__":
    main()
