# sol-scanner — Solana Survivor Scanner

Watches Solana for newly created pools, then **waits** and scores only the ones
still standing. Alert-only: it holds no wallet and cannot trade.

## Why this is not a sniper

The original code in this folder was a sniper — detect a pool, score it, buy it.
That design cannot work from here:

```
detect (confirmed commitment)   ~400-800ms behind chain
  → rugcheck HTTP call          up to 5s, blocking
  → buy
```

One to six seconds after pool creation. The bots that win that race land in the
*same block* as the pool init using Geyser/gRPC feeds and Jito bundles. Arriving
seconds later means buying *from* the snipers, not with them — you are the exit
liquidity. Worse, every safety check you add makes you slower, so "safe" and
"fast" pull in opposite directions and you lose both.

This scanner inverts the premise. It does not race. It waits out the maturity
window and reports which tokens **survived**, which turns latency from the whole
edge into an irrelevance and lets the safety checks be as thorough as you like.

## How it works

```
logsSubscribe (Raydium / pump.fun / PumpSwap)
  → resolve mint from tx token balances
  → enqueue on watchlist              [no alert, no trade]
  → wait SCAN_MATURITY_MINUTES
  → RugCheck summary  (cheap pre-filter: risk score, LP locked)
  → RugCheck full     (liquidity, holders, concentration, authorities)
  → journal verdict   (always, pass or fail)
  → Telegram alert    (only if SCAN_ALERTS_ENABLED=true)
```

**Free Helius tier is sufficient.** It uses standard `logsSubscribe`, not the
Atlas-only `transactionSubscribe` the sniper relied on.

## Gates

Applied once a token reaches maturity. Hard fails: `rugged`, mint authority
active, freeze authority active. Thresholds are the `SCAN_*` vars in the repo
root `.env` — see `.env.example`.

One counterintuitive detail: **RugCheck's score is a RISK score — lower is
safer.** USDC and WIF both return `1`. The gate is `score <= SCAN_MAX_RISK_SCORE`.

## Running

```bash
npm run typecheck        # tsc --noEmit
npm run dev              # ts-node, foreground
pm2 start ../ecosystem.config.js --only sol-scanner
```

## Output

- `reports/sol_scanner_journal.jsonl` — every verdict, append-only
- `reports/sol_scanner_state.json`    — watchlist, survives restarts

Alerts default to **off**. Let the journal accumulate first and check whether
passes actually go anywhere before wiring it to your phone — the same discipline
`discovery_scan.py` applies to its breakout tiers.

## Status

Unproven. The pipeline is verified end-to-end against live mainnet (subscriptions
connect, mints resolve correctly, watchlist persists), but **no gate threshold has
been validated against outcomes.** The numbers in `.env` are opening guesses.
