# sol-scanner — Solana Survivor Scanner

Finds Solana tokens that **survived** their first minutes and still look clean.
Alert-only: it holds no wallet and cannot trade. Free to run — no API key.

## Why it is not a sniper

The code this folder started from was a sniper: detect a new pool, score it, buy
it. That cannot work from here. Detection at `confirmed` commitment plus a
blocking safety call puts the buy 1–6s after pool creation, while the bots that
win that race land in the *same block* via Geyser feeds and Jito bundles.
Arriving seconds later means buying *from* the snipers — you are the exit
liquidity. And every safety check you add makes you slower, so "safe" and "fast"
pull against each other and you lose both.

This inverts it. It does not race. It asks which tokens are *already* an hour
old and still healthy, which makes latency irrelevant and the safety checks free.

## Why it is not on Helius any more

The first working version streamed pool creations over a Helius websocket. It
burned a **1,000,000-credit monthly quota in 7 hours** — ~137,000 credits/hour.
`logsSubscribe` delivers every transaction that *mentions* pump.fun/Raydium, so
we paid for the full firehose and discarded 99.9% of it. Staying online that way
needed the $499/mo plan.

A survivor scanner never needed a live stream. One ranked Jupiter query returns
the same answer for free.

## How it works

```
GET lite-api.jup.ag/tokens/v2/toporganicscore/1h?limit=100   [1 request/min]
  → filter to tokens aged 15–180 min
  → gate on the payload (no extra API calls)
  → journal every verdict, pass or fail
  → Telegram alert  (only if SCAN_ALERTS_ENABLED=true)
```

Deliberately *not* `/tokens/v2/recent`: that feed rotates 100% every ~42s
(~43 new mints/min), so birth-tracking would miss most tokens **and** need one
lookup per mint at maturity — far past the 60 req/min budget.

## Gates

Hard fails: mint authority active, freeze authority active, and a deceptive
symbol (empty, whitespace-only, or containing zero-width/bidi/control
characters — see `src/utils/symbol.ts`).

Thresholds (all `SCAN_*` in the repo root `.env` — see `.env.example`):
liquidity, holders, top-holder %, Jupiter organic score, 1h liquidity change,
and deployer lifetime mint count.

Two of these earn their keep immediately. **`devMints`** catches token factories:
in the first live poll, two tokens with $186k and $288k liquidity and 2,300+
holders were rejected because their deployers had minted 1,405 and 5,027 tokens.
**`SCAN_MIN_LIQ_CHANGE_PCT`** catches a rug in progress: another with $47k
liquidity and 2,494 holders was shedding 51% of its liquidity per hour.

The symbol gate came from the same first poll: a token whose entire symbol was
a zero-width space cleared every numeric gate. It is now a hard fail, and all
symbols are rendered through `safeSymbol()` so an invisible or bidi-override
name cannot misrepresent itself in a log line or a Telegram alert.

## Running

```bash
npm run typecheck
npm run dev
pm2 start ../ecosystem.config.js --only sol-scanner
```

## Output

- `reports/sol_scanner_journal.jsonl` — every verdict, append-only
- `reports/sol_scanner_journal.helius.jsonl` — archived Helius-era verdicts
  (different gates; not comparable, kept for reference)
- `reports/sol_scanner_state.json` — mints already ruled on, survives restarts

Reviewed weekly by `../sol_scanner_review.py` (pm2 `sol-scanner-review`).

Alerts default to **off**. Let the journal accumulate first.

## Status

Unproven. Verified end-to-end against live data, and the gates demonstrably
reject plausible-looking tokens for good reasons — but **no threshold has been
validated against outcomes.** The numbers in `.env` are opening guesses.
