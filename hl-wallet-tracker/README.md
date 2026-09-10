# Hyperliquid Wallet Tracker

Tracks the most successful trading wallets on Hyperliquid. Fetches complete trade history for each wallet, reconstructs round-trip trades (open → close), and generates detailed reports showing exactly how top traders operate — what they trade, how long they hold, their win rates, ROI, fees, and more.

**Read-only — no private key needed.**

---

## Quick Start

```bash
pip install -r requirements.txt
cp env.dev .env
python main.py
```

---

## Commands

```
tracker> all                    Full report — all tracked wallets
tracker> wallet 0x4f8a3b        Deep dive for one wallet
tracker> coin BTC               All BTC trades across all wallets
tracker> coin SOL               All SOL trades across all wallets
tracker> wallets                List tracked wallet addresses
tracker> refresh                Re-fetch all data
```

One-shot from terminal:
```bash

python main.py wallet 0x4f8a3b2c...
python main.py coin BTC
```

---

## What the Report Contains

### Wallet Overview Table

Quick comparison of all tracked wallets:

```
  #   WALLET          TRADES  WIN%    NET PNL     AVG PNL  PROF.F  AVG DUR  TOP COIN
  ·············································································
  1   0x4f8a3b2c...      47  68.1%  +$482,300   +$10,260    3.42     6.2h  BTC
  2   0x7d9e1a4b...      31  61.3%  +$218,900    +$7,061    2.18     2.1d  SOL
  3   0x2c8f5e3a...      28  57.1%  +$141,200    +$5,043    1.87    14.3h  ETH
```

### Per-Wallet Performance Block

```
  PERFORMANCE SUMMARY
  ·············································
  Trades        : 47   Win Rate: ████████████░░░░░░░░ 68.1%  (32W / 15L)
  Net PnL       :    +$482,300   Avg/trade:    +$10,260
  Gross PnL     :    +$531,200   Fees paid:    -$48,900
  Best trade    :     +$84,200   Worst:        -$22,100
  Avg ROI/trade :      +4.82%   Profit factor: 3.42
  Avg notional  :    $142,000

  DURATION PROFILE
  ·············································
  Avg: 6.2h      Median: 4.1h     Min: 12m      Max: 8.3d

  DIRECTION BIAS
  ·············································
  LONG  :  31 trades  ███████████████ 71% win  Net PnL:   +$312,100
  SHORT :  16 trades  ███████████░░░░ 62% win  Net PnL:   +$170,200
```

### Per-Coin Breakdown

```
  PER-COIN BREAKDOWN  (sorted by net PnL)
  ·············································
  COIN     TRADES  WIN%     NET PNL    AVG PNL  AVG ROI  AVG DUR  LONG  SHORT
  BTC          18  72.2%  +$218,400  +$12,133   +5.21%     4.2h    12      6
  SOL          12  66.7%  +$142,800  +$11,900   +4.88%     8.1h     8      4
  ETH           9  55.6%   +$74,200   +$8,244   +3.12%    12.3h     6      3
  HYPE          5  80.0%   +$52,100  +$10,420   +6.44%     2.8h     4      1
```

### Full Trade History

Every completed trade with:

```
  COIN   DIR      OPEN DATE          CLOSE DATE         DUR     SIZE    NOTIONAL       ENTRY       EXIT         PNL     ROI     FEES
  BTC    ▲ LONG   2026-03-15 09:22   2026-03-15 14:44   5.4h  0.5000   $42,500    $84,980.00  $87,200.00   +$1,099  +2.59%   $21.00
  SOL    ▼ SHORT  2026-03-14 11:03   2026-03-15 08:17  21.2h  200.00   $28,400    $142.0000   $130.5000   +$2,283  +8.04%   $17.00
  ETH    ▲ LONG   2026-03-13 16:41   2026-03-13 17:55   1.2h   5.00    $10,050  $2,010.0000  $1,985.0000   -$127  -1.26%    $5.00
```

Fields for every trade:
- **Open/Close Date** — exact timestamps
- **Duration** — how long the trade was held (minutes, hours, or days)
- **Size** — position size in coins
- **Notional** — position value in USD at entry
- **Entry / Exit price**
- **Net PnL** — after fees
- **ROI %** — return on notional
- **Fees** — total fees paid on that trade

---

## Setup

### 1. Install dependencies

```bash
python -m venv venv
source venv/bin/activate    # Mac/Linux
venv\Scripts\activate       # Windows
pip install -r requirements.txt
```

### 2. Configure `.env`

```bash
cp env.dev .env
```

**To track specific wallets**, add their addresses:
```bash
WATCH_WALLETS=0x4f8a3b2c...,0x7d9e1a4b...,0x2c8f5e3a...
```

**To auto-pull top wallets from leaderboard** (default), leave `WATCH_WALLETS` blank:
```bash
WATCH_WALLETS=
LEADERBOARD_TOP_N=10
LEADERBOARD_WINDOW=allTime
```

### 3. Run

```bash
python main.py
```

---

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `WATCH_WALLETS` | blank | Comma-separated wallet addresses. Blank = use leaderboard |
| `LEADERBOARD_TOP_N` | 10 | Number of top wallets to pull if WATCH_WALLETS is blank |
| `LEADERBOARD_WINDOW` | allTime | `day`, `week`, `month`, `allTime` |
| `LOOKBACK_DAYS` | 30 | Days of trade history to fetch |
| `MIN_TRADE_PNL` | -999999 | Filter out trades below this PnL (0 = winners only) |
| `MAX_CONCURRENT_FETCHES` | 5 | Parallel wallet fetches (keep ≤ 8) |
| `SAVE_REPORTS` | true | Save reports to `reports/` folder |
| `TELEGRAM_BOT_TOKEN` | blank | Optional |
| `TELEGRAM_CHAT_ID` | blank | Optional |

---

## How It Works

```
1. Resolve wallet list
   └── From WATCH_WALLETS in .env, OR
   └── Top N from public leaderboard ranked by PnL

2. Fetch trade fills for each wallet
   └── POST /info { type: userFillsByTime, startTime, endTime }
   └── Returns every fill: Open Long / Close Long / Open Short / Close Short

3. Reconstruct round-trip trades
   └── FIFO matching: pair each Open fill with the next matching Close fill
   └── Calculate: duration, net PnL (after fees), ROI, size, notional

4. Analyse patterns
   └── Win rate, profit factor, avg duration, direction bias
   └── Per-coin breakdown, best/worst trades

5. Generate report
   └── Overview table → per-wallet block → full trade history
   └── Saved to reports/ folder
```

---

## Project Structure

```
hl-wallet-tracker/
├── main.py                           # Entry point + CLI
├── requirements.txt
├── env.dev                           # Sample config
└── src/
    ├── config.py
    ├── bot.py                        # Orchestrator
    ├── data/
    │   ├── leaderboard_fetcher.py    # Top wallet discovery
    │   └── fills_fetcher.py          # Trade history + round-trip reconstruction
    ├── analysis/
    │   └── trade_analyser.py         # Stats, patterns, coin breakdown
    ├── report/
    │   └── report_generator.py       # Full formatted reports
    └── utils/
        ├── logger.py
        └── telegram.py
```

---

## Troubleshooting

**Empty trade history**
The wallet may have no closed trades in the lookback window. Try increasing `LOOKBACK_DAYS` to 60 or 90.

**Slow to load**
Each wallet requires one API call. With 10 wallets and 30 days of history, expect 30–60 seconds. Data is cached — subsequent commands are instant.

**Rate limit errors**
Reduce `MAX_CONCURRENT_FETCHES` to 3 in `.env`.

**Trades missing or mismatched**
Trades opened before the `LOOKBACK_DAYS` window won't have a matching open fill — only the close will appear and will be skipped. Increase `LOOKBACK_DAYS` to capture more history.
