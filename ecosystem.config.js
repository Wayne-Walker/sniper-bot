const path = require("path");
const ROOT = __dirname;
const VENV_PYTHON = path.join(ROOT, "venv", "bin", "python");

module.exports = {
  apps: [

    // ── Hyperliquid Momentum Bot (paper) ────────────────────────
    {
      name:        "hl-momentum-paper",
      script:      "main.py",
      interpreter: VENV_PYTHON,
      cwd:         path.join(ROOT, "hl-momentum"),
      env: {
        PAPER_TRADE: "true",
      },
      out_file:        "logs/momentum-out.log",
      error_file:      "logs/momentum-error.log",
      merge_logs:      true,
      log_date_format: "YYYY-MM-DD HH:mm:ss",
      autorestart:     true,
      max_restarts:    10,
      min_uptime:      "10s",
      restart_delay:   5000,
    },

    // ── Hyperliquid Momentum Bot (live) ─────────────────────────
    {
      name:        "hl-momentum-live",
      script:      "main.py",
      interpreter: VENV_PYTHON,
      cwd:         path.join(ROOT, "hl-momentum"),
      env: {
        PAPER_TRADE: "false",
      },
      out_file:        "logs/momentum-out.log",
      error_file:      "logs/momentum-error.log",
      merge_logs:      true,
      log_date_format: "YYYY-MM-DD HH:mm:ss",
      autorestart:     true,
      max_restarts:    10,
      min_uptime:      "10s",
      restart_delay:   5000,
    },

    // ── Morning Briefing (one-shot, triggered by cron) ──────────
    {
      name:          "morning-briefing",
      script:        "morning_briefing.py",
      interpreter:   VENV_PYTHON,
      cwd:           ROOT,
      cron_restart:  "30 6 * * *",      // every day at 06:30
      autorestart:   false,              // one-shot — don't restart after it finishes
      out_file:      "logs/briefing-out.log",
      error_file:    "logs/briefing-error.log",
      merge_logs:    true,
      log_date_format: "YYYY-MM-DD HH:mm:ss",
    },

    // ── Flow Alert — intraday macro-shift watcher (one-shot, hourly) ─
    {
      name:          "flow-alert",
      script:        "flow_alert.py",
      interpreter:   VENV_PYTHON,
      cwd:           ROOT,
      cron_restart:  "0 */4 * * *",      // every 4 hours (00,04,08,12,16,20 UTC)
      autorestart:   false,              // one-shot — dedup prevents repeat alerts
      out_file:      "logs/flow-alert-out.log",
      error_file:    "logs/flow-alert-error.log",
      merge_logs:    true,
      log_date_format: "YYYY-MM-DD HH:mm:ss",
    },

    // ── Crypto News Scan — AM (one-shot, web-searched catalysts) ─
    {
      name:          "crypto-news-am",
      script:        "crypto_news.py",
      interpreter:   VENV_PYTHON,
      cwd:           ROOT,
      cron_restart:  "30 7 * * *",       // every day at 07:30 UTC
      autorestart:   false,              // one-shot
      out_file:      "logs/news-out.log",
      error_file:    "logs/news-error.log",
      merge_logs:    true,
      log_date_format: "YYYY-MM-DD HH:mm:ss",
    },

    // ── Crypto News Scan — PM (one-shot, web-searched catalysts) ─
    {
      name:          "crypto-news-pm",
      script:        "crypto_news.py",
      interpreter:   VENV_PYTHON,
      cwd:           ROOT,
      cron_restart:  "30 16 * * *",      // every day at 16:30 UTC
      autorestart:   false,              // one-shot
      out_file:      "logs/news-out.log",
      error_file:    "logs/news-error.log",
      merge_logs:    true,
      log_date_format: "YYYY-MM-DD HH:mm:ss",
    },

    // ── Exhaustion Watch — reversal early warning, top+bottom (one-shot, 30m) ─
    // [reversal] Cross-exchange confluence: dual-listed coins (on Hyperliquid
    // AND Binance) require STRICT AND — both venues must agree on direction and
    // each clear MIN_SIGNALS before alerting. Single-venue coins (e.g. TLM/VET,
    // Binance only) fall back to single-source, tagged "<venue> only". Funding
    // is normalized to annualized % across venues (HL hourly, Binance 8h).
    {
      name:          "exhaustion-watch",
      script:        "exhaustion_watch.py",
      interpreter:   VENV_PYTHON,
      cwd:           ROOT,
      // no args — coin list comes from EXHAUSTION_COINS in .env
      cron_restart:  "*/30 * * * *",     // every 30 minutes
      autorestart:   false,              // one-shot — cooldown/dedup prevents spam
      out_file:      "logs/exhaustion-out.log",
      error_file:    "logs/exhaustion-error.log",
      merge_logs:    true,
      log_date_format: "YYYY-MM-DD HH:mm:ss",
    },

    // ── Macro Bottom Scanner — weekly cycle accumulation zone (one-shot, daily) ─
    // BTC-anchored 0-100 score for BTC/ETH/SOL/XRP/BNB from weekly 200W MA, RSI,
    // structure, Fear & Greed, and funding. Publishes macro/{COIN} to Firebase
    // and alerts the private chat on band transitions (dedup in macro_state.json).
    {
      name:          "macro-bottom",
      script:        "macro_bottom.py",
      interpreter:   VENV_PYTHON,
      cwd:           ROOT,
      // no args — coin list comes from MACRO_COINS in .env (default BTC,ETH,SOL,XRP,BNB)
      cron_restart:  "5 8 * * *",        // 08:05 daily — offset from the :00/:30 exhaustion ticks
      autorestart:   false,              // one-shot — transition-only alerts prevent spam
      out_file:      "logs/macro-out.log",
      error_file:    "logs/macro-error.log",
      merge_logs:    true,
      log_date_format: "YYYY-MM-DD HH:mm:ss",
    },

    // ── USDT Dominance — risk-on/off rotation gauge for BTC+alts (one-shot, hourly) ─
    // USDT.D S/R (inverse to crypto): rejecting resistance / breaking support = a
    // rally window for BTC+alts. Backfills S/R once (reconstructed + calibrated),
    // then keeps the authoritative /global value current; publishes
    // intelligence/usdt_dominance and pushes the private chat ONLY on a key-level
    // event (break, or test-and-reject) — drift states stay silent.
    // Hourly, not daily: a level break is a timing signal and was previously up to
    // 24h late. The series still holds one point per DAY (each run replaces today's
    // sample), so S/R is unchanged by the polling rate; cost is 24 free /global
    // calls a day.
    {
      name:          "usdt-dominance",
      script:        "usdt_dominance.py",
      interpreter:   VENV_PYTHON,
      cwd:           ROOT,
      cron_restart:  "15 * * * *",       // :15 every hour — a level break shouldn't wait for tomorrow
      autorestart:   false,              // one-shot — level-event alerts only (see USDTD_REALERT_H)
      out_file:      "logs/usdtd-out.log",
      error_file:    "logs/usdtd-error.log",
      merge_logs:    true,
      log_date_format: "YYYY-MM-DD HH:mm:ss",
    },

    // ── Macro Conditions — 10Y yield + DXY risk-on/off regime (one-shot, daily) ─
    // A "don't fight the macro" read: rising yields+dollar = risk-off headwind,
    // falling = risk-on tailwind. Caches reports/macro_conditions.json (read by
    // the exhaustion engine) and publishes intelligence/macro_conditions.
    {
      name:          "macro-conditions",
      script:        "macro_conditions.py",
      interpreter:   VENV_PYTHON,
      cwd:           ROOT,
      cron_restart:  "10 8 * * *",       // 08:10 daily — after macro-bottom, macro data is daily
      autorestart:   false,              // one-shot — daily regime read
      out_file:      "logs/macro-cond-out.log",
      error_file:    "logs/macro-cond-error.log",
      merge_logs:    true,
      log_date_format: "YYYY-MM-DD HH:mm:ss",
    },

    // ── Discovery Scan — buy/fade/momentum radar over Bitget (one-shot, 30m) ─
    // Two lenses (gainers + RVOL) → classify buy/fade/momentum, crypto-only,
    // exhaustion-confirmed. Publishes scan/{COIN} to Firebase and pings the
    // private chat on a NEW buy/fade candidate (dedup in scanner_state.json).
    {
      name:          "discovery-scan",
      script:        "discovery_scan.py",
      interpreter:   VENV_PYTHON,
      cwd:           ROOT,
      cron_restart:  "15,45 * * * *",    // :15/:45 — offset from exhaustion (:00/:30) + macro (08:05)
      autorestart:   false,              // one-shot — transition-only alerts prevent spam
      out_file:      "logs/scan-out.log",
      error_file:    "logs/scan-error.log",
      merge_logs:    true,
      log_date_format: "YYYY-MM-DD HH:mm:ss",
    },

    // ── Ratio Watch — alt/BTC relative-strength monitor (one-shot, hourly) ─
    // Tracks XRP/BTC (and any RATIO_PAIRS) in sats: RSI + TRAMA trend + S/R.
    // Pings the private chat only on a TRAMA trend flip, a TRAMA reclaim/lose,
    // or a dominant-shelf break (dedup in ratio_state.json), plus one daily
    // snapshot at RATIO_SNAPSHOT_HOUR UTC. No funding on a spot ratio.
    {
      name:          "ratio-watch",
      script:        "ratio_watch.py",
      interpreter:   VENV_PYTHON,
      cwd:           ROOT,
      // no args — pairs come from RATIO_PAIRS in .env (default XRP/BTC)
      cron_restart:  "50 * * * *",       // :50 — offset from exhaustion(:00/:30), discovery(:15/:45), macro(08:05)
      autorestart:   false,              // one-shot — transition-only alerts prevent spam
      out_file:      "logs/ratio-out.log",
      error_file:    "logs/ratio-error.log",
      merge_logs:    true,
      log_date_format: "YYYY-MM-DD HH:mm:ss",
    },

    // ── Smart Money Liquidation Monitor (24/7 background) ───────
    {
      name:        "hl-liquidation-monitor",
      script:      "liquidation_watcher.py",
      interpreter: VENV_PYTHON,
      cwd:         path.join(ROOT, "hl-smartmoney"),
      out_file:        "logs/liquidation-out.log",
      error_file:      "logs/liquidation-error.log",
      merge_logs:      true,
      log_date_format: "YYYY-MM-DD HH:mm:ss",
      autorestart:     true,
      max_restarts:    10,
      min_uptime:      "10s",
      restart_delay:   10000,
    },
    // ── Solana Survivor Scanner (24/7 background, alert-only) ───
    // Watches Raydium/pump.fun/PumpSwap for new pools, then scores them only
    // AFTER they survive a maturity window. Never trades — no wallet is loaded.
    {
      name:        "sol-scanner",
      script:      "node_modules/.bin/ts-node",
      args:        "src/index.ts",
      cwd:         path.join(ROOT, "sol-scanner"),
      out_file:        "logs/sol-scanner-out.log",
      error_file:      "logs/sol-scanner-error.log",
      merge_logs:      true,
      log_date_format: "YYYY-MM-DD HH:mm:ss",
      autorestart:     true,
      max_restarts:    10,
      min_uptime:      "30s",
      restart_delay:   10000,
    },
    // ── sol-scanner weekly calibration review (one-shot, weekly) ─
    // The scanner's SCAN_* gates were opening guesses. This reads its journal
    // and reports which gate is doing the rejecting + the near-misses that name
    // the threshold worth moving, then asks Claude (subscription, API creds
    // stripped) for a calibration read. Pushes to the private chat.
    {
      name:          "sol-scanner-review",
      script:        "sol_scanner_review.py",
      interpreter:   VENV_PYTHON,
      cwd:           ROOT,
      cron_restart:  "0 8 * * 1",        // Mondays 08:00 local — start of the week
      autorestart:   false,              // one-shot
      out_file:      "logs/sol-review-out.log",
      error_file:    "logs/sol-review-error.log",
      merge_logs:      true,
      log_date_format: "YYYY-MM-DD HH:mm:ss",
    },
  ],
};
