# Hyperliquid Smart Money Trading System

A three-tool trading stack for Hyperliquid perpetual futures that uses smart money analysis to find, validate, and act on high-conviction trade signals.

```
hl-smartmoney     →  finds the edge (divergence signals)
hl-wallet-tracker →  validates the edge (historical performance)
hl-momentum       →  executes the trade (new listing auto-trader)
morning_briefing  →  daily consolidated intelligence to Telegram
```

---

## Architecture

```
08:00 AM (daily cron via PM2)
  └── morning_briefing.py
        ├── Step 0: macro context — stablecoin flows, market cap,
        │           BTC dominance, Fear & Greed, BTC ETF flows
        ├── Step 1: hl-smartmoney edge scan → divergence signals
        ├── Step 2: hl-wallet-tracker coin validation → historical stats
        ├── Step 3: hl-smartmoney coin reports → entry clusters
        └── Step 4: consolidated Telegram briefing

24/7 (PM2 background)
  ├── hl-momentum (paper mode) → listens for new perp listings, auto-trades
  └── hl-liquidation-monitor → WebSocket alerts when whales get liquidated
```

---

## Quick Start

```bash
# 1. Set up virtual environment
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. Configure each bot
cp hl-smartmoney/env.dev hl-smartmoney/.env
cp hl-wallet-tracker/env.dev hl-wallet-tracker/.env
cp hl-momentum/env.dev hl-momentum/.env
# Edit each .env — at minimum set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID

# 3. Install PM2
npm install -g pm2

# 4. Start everything
pm2 start ecosystem.config.js

# 5. Save for auto-restart
pm2 save
```

---

## What Each Tool Does

### hl-smartmoney (Edge Detection)

Analyses the top 100 most profitable ("smart money") vs top 100 least profitable ("dumb money") wallets on Hyperliquid. Compares their open positions coin-by-coin and generates divergence signals.

**No private key required — read-only.**

```bash
cd hl-smartmoney

python main.py              # interactive CLI
python main.py edge         # one-shot: show all signals above threshold
python main.py global       # one-shot: full market scan
python main.py coin SOL     # one-shot: deep report for SOL
python main.py check SOL long   # gate check: is this trade allowed?
```

A signal fires when smart money and dumb money are on opposite sides with a divergence gap above `EDGE_THRESHOLD` (default 50%):

```
🟢 $SOL   LONG   gap=72%   mark=$142.50   STRONGLY BULLISH
   Smart: 80% long  net=+$4,820,000  lev=3.2x
   Dumb : 20% long  net=-$2,100,000  lev=8.5x
```

### hl-wallet-tracker (Signal Validation)

Fetches complete trade history for top wallets, reconstructs round-trip trades (open to close), and generates performance reports showing win rates, profit factors, hold durations, and per-coin breakdowns.

**No private key required — read-only.**

```bash
cd hl-wallet-tracker

python main.py              # interactive CLI
python main.py all          # one-shot: full report for all tracked wallets
python main.py wallet 0x4f8a3b   # one-shot: single wallet deep dive
python main.py coin BTC     # one-shot: all BTC trades across wallets
```

Use this to validate signals from hl-smartmoney:
- Does the smart money wallet have >60% win rate on that coin?
- Is their profit factor >2?
- What's their typical hold duration?
- Where did they enter last time?

### hl-momentum (Trade Execution)

Detects new perpetual futures listings on Hyperliquid, observes price/volume momentum in the opening 10 seconds, and auto-trades based on momentum signals. Runs unattended via PM2.

**Requires API wallet private key for trading.**

```bash
cd hl-momentum

python main.py              # run directly
pm2 start ecosystem.config.js --only hl-momentum-paper   # paper mode
pm2 start ecosystem.config.js --only hl-momentum-live    # live mode
```

Exit strategy: +3% take profit with 1% trailing stop, -1.5% stop loss, 10-minute time stop.

### morning_briefing.py (Daily Intelligence)

Consolidates all three tools into a single daily Telegram briefing. Runs automatically at 8 AM via PM2 cron.

```bash
# Run manually
python morning_briefing.py

# Or trigger via PM2
pm2 restart morning-briefing
```

The briefing includes:
1. **Macro context** — stablecoin flows, market cap + BTC dominance, Fear & Greed Index, BTC ETF flows (see below)
2. All divergence signals above threshold
3. Wallet tracker validation for each signal
4. Entry cluster data for top signals
5. Trading rules reminder

### Macro Context (Step 0)

Two modules feed the macro section. All data is from free, unauthenticated sources — no API keys.

**`src/data/market_pulse.py`**
- Stablecoin market cap + 1d / 7d / 30d flows (USDT, USDC, DAI) — [DefiLlama](https://stablecoins.llama.fi/stablecoins)
- Total crypto market cap, 24h change, BTC + ETH dominance — [CoinGecko global API](https://api.coingecko.com/api/v3/global)
- BTC, ETH, XRP, SOL prices with 24h / 7d % change — CoinGecko markets API
- Crypto Fear & Greed Index + 7d delta — [Alternative.me](https://api.alternative.me/fng/)

**`src/data/etf_flows.py`**
- US Spot Bitcoin ETF daily flows + per-issuer breakdown (IBIT, FBTC, GBTC, …) scraped from the plain-HTML table at [Bitbo](https://bitbo.io/treasuries/etf-flows/)
- 7d cumulative flow, consecutive in/outflow streak, top movers

Both modules auto-derive directional signals (e.g. "🟢 Heavy stablecoin inflow 7d", "🔴 Extreme Greed — historically a sell zone", "🟢 5-day inflow streak — persistent bid"). Each source fails independently — if one breaks the rest still publish.

> **Note:** ETH / SOL / XRP ETF flows are *not* freely available in raw HTML from any tested source (CoinGlass, SoSoValue, CoinMarketCap all use authenticated JSON APIs or JS-rendered tables). Add these when a paid CoinGlass key ($29/mo) is acquired — the integration point is a 30-line extension to `etf_flows.py`.

### Liquidation Monitor (24/7 Alerts)

Connects to Hyperliquid's WebSocket and sends Telegram alerts when whales get liquidated above `MIN_LIQUIDATION_USD` threshold. Runs as a standalone process via PM2.

```
🔴 WHALE LIQUIDATED
Coin      : $SOL
Side      : LONG
Notional  : $847,000
Liq Price : $138.2400
Wallet    : 0x4f8a3b2c...
```

---

## PM2 Process Management

All services are managed via the root `ecosystem.config.js`:

| Process | Type | Schedule |
|---------|------|----------|
| `hl-momentum-paper` | Long-running | 24/7, auto-restart |
| `hl-momentum-live` | Long-running | 24/7, auto-restart (start manually) |
| `morning-briefing` | One-shot | Cron: `0 8 * * *` (8 AM daily) |
| `hl-liquidation-monitor` | Long-running | 24/7, auto-restart |

### Common Commands

```bash
pm2 status                          # see all processes
pm2 logs                            # tail all logs
pm2 logs morning-briefing           # tail specific logs
pm2 restart morning-briefing        # trigger briefing now
pm2 start ecosystem.config.js       # start all services
pm2 stop all                        # stop everything
pm2 save                            # save process list for reboot
```

### Auto-start on Reboot (macOS)

```bash
pm2 startup
# Run the sudo command it outputs (requires sudo), then:
pm2 save
```

### Important: Mac Must Be Awake

PM2 runs as a user process — if your Mac is asleep or the lid is closed at 8 AM, the morning briefing won't fire. To ensure it runs:

- **Option 1:** Set macOS to auto-wake before 8 AM (System Settings > Energy > Schedule)
- **Option 2:** Change the cron time in `ecosystem.config.js` to match when your Mac is reliably awake
- **Option 3:** Trigger it manually anytime with `pm2 restart morning-briefing`

The liquidation monitor and momentum bot are long-running processes — they resume automatically once the Mac wakes up, but will miss events that occurred while asleep.

---

## Daily Trading Workflow

### 08:00 — Morning Briefing (automated)

The `morning-briefing` PM2 process runs automatically and sends a Telegram message with today's signals. No action needed.

### 08:10 — Review Signals

Open Telegram and review the briefing. For each signal, check:
- Gap strength >60% (higher = stronger conviction)
- Smart money leverage (should be 2-3x, not 8-10x)
- Historical validation (wallet win rate on that coin)

### 08:20 — Identify Entry Zone

For signals you want to trade, check where smart money entered:

```bash
cd hl-smartmoney
python main.py coin SOL
```

Look at the entry clustering data — if smart money entered at $136 and price is at $142, you're late. Either wait for a pullback or accept the later entry.

### 09:00 — Enter Trade

Trade manually on the Hyperliquid app. Match smart money behavior:
- Leverage: 2-3x max
- Take profit: 4-5% (based on historical avg ROI)
- Stop loss: 1.5-2%
- Time stop: 6-8 hours (based on historical avg duration)

### Throughout Day — Monitor Liquidation Alerts

The liquidation monitor runs 24/7. When dumb money gets liquidated at scale, it often accelerates smart money's trade direction — secondary entry signal.

### 17:00 — Review Open Positions

```bash
cd hl-wallet-tracker
python main.py all
```

Check the open positions section. Exit if time stop is hit.

---

## The Edge

The edge is behavioral, not technical:

| Losers | Smart Money |
|--------|-------------|
| High leverage (8-10x) | Low leverage (2-3x) |
| Chase price after move | Enter at accumulation zones |
| Hold losers hoping for reversal | Cut losses quickly |
| Trade every coin | Focus on 3-5 coins they know |
| Ignore funding rates | Time entries around funding |

---

## Project Structure

```
sniper-bot/
├── ecosystem.config.js              # PM2 config for all services
├── morning_briefing.py              # Daily consolidated intelligence
├── tradePlan.txt                    # Trading strategy reference
│
├── src/data/                        # Macro data sources (free APIs)
│   ├── market_pulse.py              # Stablecoins, market cap, F&G, prices
│   └── etf_flows.py                 # BTC ETF flows (Bitbo scrape)
│
├── hl-smartmoney/                   # Edge detection
│   ├── main.py                      # CLI entry point
│   ├── liquidation_watcher.py       # Standalone liquidation monitor
│   └── src/
│       ├── bot.py                   # Orchestrator
│       ├── config.py                # Settings from .env
│       ├── data/
│       │   ├── leaderboard_fetcher.py
│       │   ├── position_fetcher.py
│       │   └── liquidation_monitor.py
│       ├── analysis/
│       │   ├── divergence_engine.py
│       │   └── edge_filter.py
│       ├── report/
│       │   └── report_generator.py
│       └── utils/
│
├── hl-wallet-tracker/               # Signal validation
│   ├── main.py
│   └── src/
│       ├── bot.py
│       ├── config.py
│       ├── data/
│       │   ├── leaderboard_fetcher.py
│       │   └── fills_fetcher.py
│       ├── analysis/
│       │   └── trade_analyser.py
│       ├── report/
│       │   └── report_generator.py
│       └── utils/
│
├── hl-momentum/                     # Trade execution
│   ├── main.py
│   ├── ecosystem.config.js
│   └── src/
│       ├── listener/
│       │   └── listing_listener.py
│       ├── detector/
│       │   └── momentum_detector.py
│       ├── executor/
│       │   └── trade_executor.py
│       ├── exit/
│       │   └── exit_manager.py
│       └── utils/
│
├── venv/                            # Shared Python virtual environment
├── logs/                            # PM2 log files
├── reports/                         # Saved briefings and reports
└── signals/                         # Saved edge filter signals (JSON + TXT)
```

---

## Configuration Reference

### hl-smartmoney/.env

| Variable | Default | Description |
|---|---|---|
| `TOP_N_WALLETS` | 20 | Number of winners and losers to analyse |
| `MIN_ACCOUNT_VALUE` | 10000 | Ignore wallets below this USD value |
| `LEADERBOARD_WINDOW` | allTime | `day`, `week`, `month`, `allTime` |
| `MAX_CONCURRENT_FETCHES` | 8 | Parallel position fetches (keep <=10) |
| `EDGE_THRESHOLD` | 50.0 | Min gap % to emit a signal |
| `MIN_LIQUIDATION_USD` | 100000 | Min liquidation size to alert |
| `SAVE_REPORTS` | true | Save reports to `reports/` folder |
| `TELEGRAM_BOT_TOKEN` | blank | Required for alerts |
| `TELEGRAM_CHAT_ID` | blank | Required for alerts |

### hl-wallet-tracker/.env

| Variable | Default | Description |
|---|---|---|
| `WATCH_WALLETS` | blank | Comma-separated addresses (blank = use leaderboard) |
| `LEADERBOARD_TOP_N` | 10 | Top N wallets if WATCH_WALLETS blank |
| `LEADERBOARD_WINDOW` | allTime | `day`, `week`, `month`, `allTime` |
| `LOOKBACK_DAYS` | 30 | Days of trade history to fetch |
| `MIN_TRADE_PNL` | -999999 | Filter threshold for trades |
| `MAX_CONCURRENT_FETCHES` | 5 | Parallel fetches (keep <=8) |
| `SAVE_REPORTS` | true | Save reports to `reports/` |

### hl-momentum/.env

| Variable | Default | Description |
|---|---|---|
| `PRIVATE_KEY` | required | API wallet private key (0x prefixed) |
| `WALLET_ADDRESS` | required | Main Hyperliquid wallet address |
| `TRADE_SIZE_USD` | 20 | USD per trade (min $10) |
| `LEVERAGE` | 1 | Leverage multiplier |
| `MAX_CONCURRENT_TRADES` | 2 | Max open positions |
| `OBSERVATION_SECONDS` | 10 | Seconds to observe before entering |
| `MIN_MOMENTUM_PCT` | 0.5 | Min price move % to enter |
| `MIN_VOLUME_USD` | 5000 | Min volume in observation window |
| `TAKE_PROFIT_PCT` | 3.0 | Take profit at +3% |
| `STOP_LOSS_PCT` | 1.5 | Stop loss at -1.5% |
| `TRAILING_STOP_PCT` | 1.0 | Trail distance after TP hit |
| `TIME_STOP_MINUTES` | 10 | Exit if flat after 10 min |
| `PAPER_TRADE` | true | Set false for live trading |

---

## Troubleshooting

**Rate limit errors (429)**
Reduce `MAX_CONCURRENT_FETCHES` in `.env` files. Avoid running multiple bots simultaneously during initial data fetch.

**No signals found**
The leaderboard wallets may be aligned (no divergence). Try `LEADERBOARD_WINDOW=week` for more active wallets, or lower `EDGE_THRESHOLD` to 30-40%.

**Morning briefing slow**
With 100 wallets and 365-day lookback, the wallet tracker step takes several minutes. Reduce `LEADERBOARD_TOP_N` to 20 and `LOOKBACK_DAYS` to 30 for faster runs.

**Telegram alerts not arriving**
Verify `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set in the relevant `.env` file. Check `pm2 logs` for error messages.

**PM2 processes keep restarting**
Check `pm2 logs <name>` for the error. Common issues: missing dependencies, invalid API keys, network errors.

---

## Important Caveat

These tools provide an informational edge -- they do not guarantee profits. Even top wallets have a 30-40% loss rate on individual trades. The edge comes from positive expectancy over many trades, not from any single trade being certain.

Start with paper trading. Validate signals over 20-30 trades before sizing up.


----

TOOL              COMMAND                    PURPOSE
─────────────────────────────────────────────────────────────
hl-smartmoney     python main.py edge        Morning watchlist
hl-smartmoney     python main.py coin SOL    Entry cluster data
hl-smartmoney     python main.py check SOL long   Gate check
hl-smartmoney     python main.py global      Full market scan
hl-smartmoney     python main.py             Run + liq monitor

hl-wallet-tracker python main.py all         Validate signals
hl-wallet-tracker python main.py coin SOL    All SOL trades
hl-wallet-tracker python main.py wallet 0x.. Single wallet

hl-momentum       python main.py             Auto-trade bot


-----

python main.py check SOL long

The gate check answers one question before you enter a trade:

"Does the smart money data support this trade right now?"


What it actually does
1. Runs a fresh smart money scan for SOL specifically
         │
         ▼
2. Checks if SOL has a DIVERGENCE signal
   (winners and losers on opposite sides)
         │
         ▼
3. Calculates the gap %
   gap = |smart_notional - dumb_notional| / total_notional × 100
         │
         ▼
4. Checks if gap > EDGE_THRESHOLD (default 50%)
         │
         ▼
5. Checks if the signal direction matches what you want to trade

Example outputs
Trade allowed:
  GATE CHECK — $SOL LONG
  ─────────────────────────────────────────────────────────────────
  ✅ ALLOWED
  Edge confirmed — gap=72.4%  direction=LONG  sentiment=STRONGLY BULLISH
This means: smart money is net long SOL, dumb money is net short, the gap between them is 72% — strong enough to trade.

Blocked — wrong direction:
  GATE CHECK — $SOL LONG
  ─────────────────────────────────────────────────────────────────
  🚫 BLOCKED
  Direction mismatch — smart money says SHORT but bot wants to go LONG
This means: there IS a divergence signal on SOL, but smart money is actually shorting it. You'd be trading against them.

Blocked — no edge:
  GATE CHECK — $SOL LONG
  ─────────────────────────────────────────────────────────────────
  🚫 BLOCKED
  No signal for $SOL above 50% threshold
This means: either no divergence detected at all, or the gap is too small to be meaningful. No edge — don't trade.