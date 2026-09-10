# Hyperliquid New Listing Momentum Bot

Detects new perpetual futures listings on Hyperliquid, observes price and volume momentum in the opening seconds, then automatically opens a long or short position in the direction of the move.

---

## How It Works

```
Poll Hyperliquid every 15s → new coin detected
             │
             ▼
   Observe 10 seconds of price + volume
             │
        Weak signal? → Skip
             │
             ▼
   Open LONG (rising) or SHORT (falling)
             │
             ▼
   Monitor every 5s and exit on:
     +3%  → take profit, activate trailing stop
     -1.5% → stop loss
     10min → time stop if still flat
     Trail → close if price reverses 1% from peak
```

---

## Prerequisites

Before running the bot you need three things:

1. **Python 3.10+** installed on your machine
2. **A Hyperliquid account** with USDC deposited
3. **A Hyperliquid API wallet** (separate from your main wallet — instructions below)

---

## Step 1 — Get Your Hyperliquid API Key

This is the most important step. You need a dedicated **API wallet** — this is different from your main wallet. It can trade on your behalf but cannot withdraw funds, which makes it safer to use in a bot.

**1. Go to the Hyperliquid app**
- Open your browser and go to: https://app.hyperliquid.xyz
- Connect your main wallet (MetaMask or similar)

**2. Generate an API wallet**
- Click **More** in the top navigation
- Click **API**
- Click **Generate API Wallet**
- Give it a name (e.g. "momentum-bot")
- Click **Approve** in your wallet — this authorises the API wallet to trade

**3. Copy your credentials**
- You will see a **Private Key** — copy this immediately, it is only shown once
- This goes into your `.env` file as `PRIVATE_KEY`

**4. Get your main wallet address**
- This is the wallet address you connected with (shown in the top right)
- Copy it — this goes into your `.env` file as `WALLET_ADDRESS`

> ⚠️ The API wallet private key only controls trading — it cannot withdraw funds.
> Never share it or commit it to git.

---

## Step 2 — Deposit USDC to Hyperliquid

The bot trades from your Hyperliquid perpetuals balance.

1. On https://app.hyperliquid.xyz click **Deposit**
2. Deposit USDC from Arbitrum (cheapest bridge fees)
3. Make sure you have at least **$50 USDC** to start safely
4. The minimum trade size on Hyperliquid is **$10 per order**

---

## Step 3 — Install Python Dependencies

Open a terminal in the project folder and run:

```bash
# Optional but recommended — create a virtual environment first
python -m venv venv

# Activate it
# On Mac/Linux:
source venv/bin/activate
# On Windows:
venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

---

## Step 4 — Configure Your Environment

Copy the sample config file and fill in your values:

```bash
cp env.dev .env
```

Then open `.env` in a text editor and fill in at minimum:

```bash
PRIVATE_KEY=0x...        # from Step 1 — your API wallet private key
WALLET_ADDRESS=0x...     # from Step 1 — your main wallet address
PAPER_TRADE=true         # keep true until you are ready to go live
```

Full `.env` settings are explained in the **Configuration Reference** section below.

---

## Step 5 — Run in Paper Trade Mode

Paper trade mode watches real markets and goes through all the logic — but never sends any real orders. Always start here.

```bash
python main.py
```

You should see output like:

```
╔══════════════════════════════════════════╗
║   Hyperliquid New Listing Momentum Bot   ║
╚══════════════════════════════════════════╝

[INFO]    Account value: $250.00 USDC
[INFO]    Listing listener started — tracking 142 existing perps
[INFO]    Exit manager started
[INFO]    Bot running — listening for new listings...

[INFO]    🆕 New listing detected: NEWTOKEN
[INFO]    [NEWTOKEN] Observing for 10s...
[INFO]    [NEWTOKEN] Start price: 1.2400
[INFO]    [NEWTOKEN] End price: 1.2900 | Change: +4.03% | Volume: $18,200
[INFO]    [NEWTOKEN] ✅ Momentum signal: LONG | +4.03%
[WARNING] [PAPER] Would open LONG NEWTOKEN | size=16.129 @ $1.2900 | notional=$20
```

Watch the logs for a while. When you are comfortable with what the bot is doing, move to Step 6.

---

## Step 6 — Go Live

When you are ready to trade with real money:

1. Open `.env` and change:
```bash
PAPER_TRADE=false
```

2. Set a conservative trade size to start:
```bash
TRADE_SIZE_USD=20     # start small
LEVERAGE=1            # no leverage until you understand the bot
```

3. Run the bot:
```bash
python main.py
```

---

## Step 7 — Run in the Background with PM2 (Optional)

If you want the bot to keep running after you close your terminal, use PM2.

**Install PM2 (requires Node.js):**
```bash
npm install -g pm2
```

**Start in paper mode:**
```bash
pm2 start ecosystem.config.js --only hl-momentum-paper
```

**Start in live mode:**
```bash
pm2 start ecosystem.config.js --only hl-momentum-live
```

**Useful PM2 commands:**
```bash
pm2 status          # see if bot is running
pm2 logs            # watch live logs
pm2 stop all        # stop the bot
pm2 restart all     # restart the bot
```

**Log files** are written to the `logs/` folder:
- `logs/bot.log` — all bot activity
- `logs/pm2-out.log` — PM2 output
- `logs/pm2-error.log` — PM2 errors

---

## Project Structure

```
hl-momentum/
├── main.py                         # Entry point — wires all modules
├── requirements.txt                # Python dependencies
├── env.dev                         # Sample environment config
├── ecosystem.config.js             # PM2 background runner config
└── src/
    ├── config.py                   # Loads and validates .env settings
    ├── listener/
    │   └── listing_listener.py     # Polls Hyperliquid for new perp listings
    ├── detector/
    │   └── momentum_detector.py    # Measures price/volume in opening window
    ├── executor/
    │   └── trade_executor.py       # Opens and closes positions via HL API
    ├── exit/
    │   └── exit_manager.py         # Take profit / stop loss / trailing / time stop
    └── utils/
        ├── logger.py               # Coloured console + file logging
        └── telegram.py             # Optional Telegram alerts
```

---

## Configuration Reference

All settings live in your `.env` file.

| Variable | Default | Description |
|---|---|---|
| `PRIVATE_KEY` | required | API wallet private key (0x prefixed) |
| `WALLET_ADDRESS` | required | Your main Hyperliquid wallet address |
| `TRADE_SIZE_USD` | 20 | USD notional per trade (min $10) |
| `LEVERAGE` | 1 | Leverage multiplier — keep at 1 to start |
| `MAX_CONCURRENT_TRADES` | 2 | Max open positions at once |
| `OBSERVATION_SECONDS` | 10 | Seconds to observe before entering |
| `MIN_MOMENTUM_PCT` | 0.5 | Min price move % required to enter |
| `MIN_VOLUME_USD` | 5000 | Min USD volume in observation window |
| `TAKE_PROFIT_PCT` | 3.0 | Close % gain — activates trailing stop |
| `STOP_LOSS_PCT` | 1.5 | Close % loss — exits immediately |
| `TIME_STOP_MINUTES` | 10 | Exit if no significant move after N mins |
| `TRAILING_STOP_PCT` | 1.0 | Trailing stop % after take profit hit |
| `PAPER_TRADE` | true | true = simulation only, false = live |
| `TELEGRAM_BOT_TOKEN` | blank | Optional — leave blank to disable |
| `TELEGRAM_CHAT_ID` | blank | Optional — leave blank to disable |

---

## Optional — Telegram Alerts

To receive trade alerts on your phone:

1. Open Telegram and search for **@BotFather**
2. Send `/newbot` and follow the prompts — copy the **bot token**
3. Start a chat with your new bot, then visit:
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
4. Send a message to the bot, refresh the URL, find your **chat_id**
5. Add both values to your `.env`:
```bash
TELEGRAM_BOT_TOKEN=123456:ABCdef...
TELEGRAM_CHAT_ID=987654321
```

---

## Safety Checklist

Before going live, confirm:

- [ ] You have tested in `PAPER_TRADE=true` mode and seen signals fire
- [ ] `TRADE_SIZE_USD` is set to an amount you are comfortable losing entirely
- [ ] `LEVERAGE=1` until you are confident in the bot's behaviour
- [ ] Your API wallet private key is only in `.env` — not committed to git
- [ ] You have enough USDC deposited (at least 3x your trade size)
- [ ] You understand that new listings are highly volatile and this strategy can lose money

---

## Troubleshooting

**Bot starts but never detects a listing**
New perp listings on Hyperliquid are rare — sometimes days apart. This is normal. The bot is working if you see `Listing listener started — tracking N existing perps` in the logs.

**`Missing required env var: PRIVATE_KEY`**
Your `.env` file is missing or not in the same folder as `main.py`. Make sure you ran `cp env.dev .env` and filled in the values.

**`Insufficient balance`**
Your Hyperliquid account value is too low. Deposit more USDC at https://app.hyperliquid.xyz.

**`Order failed`**
Usually means the position size is below Hyperliquid's $10 minimum. Increase `TRADE_SIZE_USD` to at least 15 to account for price fluctuation.
