# Hyperliquid Smart Money Sentiment Bot

A read-only intelligence bot that scans the top 20 most profitable ("smart money") and top 20 least profitable ("dumb money") wallets on Hyperliquid, compares their open positions coin by coin, and generates divergence-based trade signals and reports.

Also runs a **live liquidation monitor** in the background — fires a Telegram alert whenever a whale gets liquidated above your configured threshold.

**No private key required — this bot never places trades.**

---

## What It Does

```
On startup:
  └── Liquidation monitor starts in background (WebSocket)
          └── Fires Telegram alert when whale liquidated above threshold

On demand (interactive CLI):
  └── global   → fetch leaderboard + all positions → full market report
  └── coin BTC → same but focused on one coin with per-wallet breakdown
  └── refresh  → re-fetch all data
```

---

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set up config
cp env.dev .env
# Open .env and fill in TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID (optional but recommended)

# 3. Run
python main.py
```

No API key, no wallet, no deposit needed.

---

## Interactive CLI

When you run `python main.py` you get a live prompt:

```
smart-money> global          Full market scan across all coins
smart-money> coin BTC        Deep report on BTC only
smart-money> coin SOL        Deep report on SOL only
smart-money> coin HYPE       Deep report on HYPE only
smart-money> refresh         Re-fetch leaderboard + positions
smart-money> help            Show available commands
smart-money> exit            Quit
```

You can also run one-shot from the terminal and exit:

```bash
python main.py global
python main.py BTC
python main.py SOL
```

---

## Report Structure

### Global Report (`global`)

```
═══════════════════════════════════════════════════════════════════
        HYPERLIQUID SMART MONEY SENTIMENT REPORT
        Generated: 2026-04-09 14:22 UTC
        Window: ALLTIME  |  Top 20 Winners & Losers
═══════════════════════════════════════════════════════════════════

  OVERVIEW
  ─────────────────────────────────────────────────────────────────
  Wallets analysed  : 18 winners / 16 losers
  Total positions   : 74
  Coins with data   : 22
  Divergence signals: 7

  TOP 5 WINNERS                               TOP 5 LOSERS
  ─────────────────────────────────────────────────────────────────
  #1  0x4f8a3b..  PnL:   +$4,820,000  ROI:  142.3%
  ...

  SENTIMENT TABLE (by coin)
  ─────────────────────────────────────────────────────────────────
  COIN      SMART MONEY                   DUMB MONEY           SIGNAL
            L%  notional%  lev           L%  notional%  lev
  ·········································································
  SOL     ▲  80% wallets  91% ntl  3.2x  ▼  20% wallets  15% ntl  8.5x  ⚡ STRONGLY BULLISH
  BTC     ▼  30% wallets  22% ntl  2.1x  ▲  70% wallets  78% ntl  5.0x  ⚡ STRONGLY BEARISH
  ETH     ▲  55% wallets  60% ntl  1.8x  ▲  50% wallets  52% ntl  3.2x  ≈ SLIGHTLY BULLISH
  ...

  TOP DIVERGENCE SIGNALS
  ═══════════════════════════════════════════════════════════════════
  [1] $SOL — STRONGLY BULLISH [DIVERGENCE]
  ...
```

---

### Coin Report (`coin BTC`)

Includes everything in the global report for that coin, plus:

**Long/Short % breakdown with visual bars:**
```
  SMART MONEY (Winners)
  Wallets  : ████████████████░░░░  80.0% LONG  |  ░░░░░░░░░░░░░░░░░░░░  20.0% SHORT
  Notional : ██████████████████░░  91.2% LONG  |   8.8% SHORT
  Long  →  8 wallets  $4,820,000  avg lev: 3.2x  avg entry: $138.4200
  Unrealized PnL :  +$142,500.00
```

**Entry price clusters (where positions were opened):**
```
  Smart Money entry concentration:
  ▲ LONG   $    136.2500  ████████████████░░░░░░░░░  62.4%  5 wallets  $3,010,000
  ▲ LONG   $    141.8000  ████████░░░░░░░░░░░░░░░░░  28.8%  2 wallets  $1,390,000
  ▼ SHORT  $    145.5000  ███░░░░░░░░░░░░░░░░░░░░░░   8.8%  1 wallet     $420,000

  Dumb Money entry concentration:
  ▼ SHORT  $    143.2000  ████████████████████░░░░░  78.2%  6 wallets  $1,640,000
```

**Per-wallet position table sorted by ROI %:**
```
  SMART MONEY POSITIONS  (sorted by ROI %)
  ···············································································
  WALLET         DIR    NOTIONAL        ENTRY     LEV   UNREAL PnL     ROI
  ···············································································
  0x4f8a3b2c..  ▲ L  $  1,200,000  $  136.4200  3.0x  $ +84,000.00  +22.1%
  0x7d9e1a4b..  ▲ L  $    800,000  $  138.1000  5.0x  $ +44,800.00  +11.2%
  0x2c8f5e3a..  ▼ S  $    420,000  $  145.5000  2.0x  $ -12,600.00   -3.0%
```

**Trade plan:**
```
  ➤  TRADE PLAN:
     LONG $SOL (mark $142.5000) — 80% of smart money longs
     ($4,820,000 @ 3.2x) vs 80% of losers short ($2,100,000
     @ 8.5x). Winners avg long entry: $138.4200.
```

---

## Liquidation Monitor

Starts automatically when you run `python main.py`. Runs in the background at all times.

Connects to Hyperliquid's WebSocket and sends a Telegram alert whenever a position is liquidated above `MIN_LIQUIDATION_USD`:

```
🔴 WHALE LIQUIDATED
Coin      : $SOL
Side      : LONG
Notional  : $847,000
Liq Price : $138.2400
Mark Price: $138.1900
Wallet    : 0x4f8a3b2c...
```

If Telegram is not configured, liquidations are still logged to `logs/bot.log`.

---

## Setup

### 1. Install Python dependencies

```bash
# Recommended — use a virtual environment
python -m venv venv

# Mac/Linux:
source venv/bin/activate
# Windows:
venv\Scripts\activate

pip install -r requirements.txt
```

### 2. Configure `.env`

```bash
cp env.dev .env
```

The only settings you must fill in for full functionality:

```bash
# Telegram — required for liquidation alerts
TELEGRAM_BOT_TOKEN=123456:ABCdef...
TELEGRAM_CHAT_ID=987654321

# Everything else has sensible defaults
```

### 3. Run

```bash
python main.py
```

---

## Setting Up Telegram Alerts

1. Open Telegram, search for **@BotFather**
2. Send `/newbot`, follow prompts, copy the **bot token**
3. Start a chat with your new bot, then visit:
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
4. Send a message to the bot, refresh the page, find your **chat_id**
5. Add both to `.env`:

```bash
TELEGRAM_BOT_TOKEN=123456:ABCdef...
TELEGRAM_CHAT_ID=987654321
```

---

## Project Structure

```
hl-smartmoney/
├── main.py                              # Entry point + interactive CLI
├── requirements.txt
├── env.dev                              # Sample config — copy to .env
└── src/
    ├── config.py                        # All settings from .env
    ├── bot.py                           # Orchestrator — wires all modules
    ├── data/
    │   ├── leaderboard_fetcher.py       # Fetches + ranks top winners/losers
    │   ├── position_fetcher.py          # Fetches open positions (clearinghouseState)
    │   └── liquidation_monitor.py       # WebSocket liquidation alerts
    ├── analysis/
    │   └── divergence_engine.py         # Gap analysis, clusters, sentiment
    ├── report/
    │   └── report_generator.py          # Formats all output + saves to reports/
    └── utils/
        ├── logger.py                    # Console + file logging
        └── telegram.py                  # Telegram alert helper
```

Reports are saved to `reports/` automatically. Logs go to `logs/bot.log`.

---

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `TOP_N_WALLETS` | 20 | Number of winners and losers to analyse |
| `MIN_ACCOUNT_VALUE` | 10000 | Ignore wallets below this USD value |
| `LEADERBOARD_WINDOW` | allTime | `day`, `week`, `month`, `allTime` |
| `MAX_CONCURRENT_FETCHES` | 8 | Parallel position fetches (keep ≤10) |
| `MIN_LIQUIDATION_USD` | 100000 | Min liquidation size to trigger alert |
| `SAVE_REPORTS` | true | Save reports to `reports/` folder |
| `AUTO_RUN_ON_START` | false | Auto-run global scan on launch |
| `TELEGRAM_BOT_TOKEN` | blank | Required for liquidation alerts |
| `TELEGRAM_CHAT_ID` | blank | Required for liquidation alerts |

### Tuning `MIN_LIQUIDATION_USD`

| Value | What you get |
|---|---|
| `50000` | All liquidations $50k+ — noisy but comprehensive |
| `100000` | Default — catches meaningful whale moves |
| `500000` | Only major liquidations — low noise |
| `1000000` | Only $1M+ liquidation events |

---

## How the Analysis Works

```
1. Fetch public leaderboard
   └── stats-data.hyperliquid.xyz/Mainnet/leaderboard
   └── Filter by MIN_ACCOUNT_VALUE
   └── Sort by PnL for chosen window
   └── Take top N (winners) and bottom N (losers)

2. Fetch open positions for all 40 wallets concurrently
   └── POST api.hyperliquid.xyz/info  { type: clearinghouseState }
   └── Parse: coin, direction, size, entry price, leverage, unrealized PnL

3. Per-coin divergence analysis
   └── Winner net notional  =  long notional − short notional
   └── Loser  net notional  =  long notional − short notional
   └── Divergence score     =  winner net − loser net
   └── Positive score       =  smart money long, dumb money short → BULLISH
   └── Negative score       =  smart money short, dumb money long → BEARISH

4. Price clustering
   └── Bucket entry prices into 5 bands
   └── Show where each group concentrated their entries
   └── Reveals smart money vs dumb money entry zones

5. Report
   └── Sentiment table — all coins at a glance
   └── Top divergence signals with full breakdown
   └── Per-wallet positions sorted by ROI %
   └── Trade plan per coin
```

---

## Troubleshooting

**No positions found / empty report**
The leaderboard top wallets may not have any open positions right now. Try `LEADERBOARD_WINDOW=week` or `LEADERBOARD_WINDOW=month` in `.env` for a broader wallet set.

**Liquidation alerts not arriving**
Check your Telegram bot token and chat ID. Run the bot and verify the startup log shows `Liquidation monitor started`. Liquidations are also logged to `logs/bot.log` regardless of Telegram config.

**Slow to load**
Fetching 40 wallets takes 10–30 seconds depending on network. Data is cached — subsequent `coin <X>` commands are instant until you `refresh`.

**Rate limit errors**
Reduce `MAX_CONCURRENT_FETCHES` to 4 or 5 in `.env`.
