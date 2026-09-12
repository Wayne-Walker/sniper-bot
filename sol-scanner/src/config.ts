import dotenv from "dotenv";
import path from "path";

// .env lives at the repo root, shared with the Python bots
dotenv.config({ path: path.resolve(__dirname, "../../.env") });

function num(key: string, fallback: number): number {
  const raw = process.env[key];
  if (raw === undefined || raw.trim() === "") return fallback;
  // .env in this repo uses trailing "# comment" on value lines
  const parsed = parseFloat(raw.split("#")[0].trim());
  return Number.isFinite(parsed) ? parsed : fallback;
}

export const config = {
  // Jupiter's free public mirror: no API key, 60 req/min. We poll once a
  // minute, so we sit at ~1/60th of the allowance.
  //
  // This replaced a Helius websocket feed that cost ~137,000 credits/hour —
  // it burned a 1,000,000-credit monthly quota in 7 hours. logsSubscribe
  // delivers EVERY transaction mentioning pump.fun/Raydium and we discarded
  // 99.9% of it; staying online that way needed the $499/mo plan. A survivor
  // scanner never needed a live stream: it needs tokens that already survived,
  // which is one ranked query.
  jupiter: {
    baseUrl:      process.env.JUP_BASE_URL ?? "https://lite-api.jup.ag",
    window:       process.env.JUP_WINDOW   ?? "1h",   // ranking window
    limit:        num("JUP_LIMIT", 100),
    pollSeconds:  num("SCAN_POLL_SECONDS", 60),
  },

  // The survival window. Tokens younger than minAgeMinutes haven't proven
  // anything yet; older than maxAgeMinutes and we're not early any more.
  age: {
    minMinutes: num("SCAN_MIN_AGE_MINUTES", 15),
    maxMinutes: num("SCAN_MAX_AGE_MINUTES", 180),
  },

  gates: {
    minLiquidityUsd:   num("SCAN_MIN_LIQUIDITY_USD", 15000),
    minHolders:        num("SCAN_MIN_HOLDERS", 75),
    maxTopHoldersPct:  num("SCAN_MAX_TOP_HOLDERS_PCT", 25),
    minOrganicScore:   num("SCAN_MIN_ORGANIC_SCORE", 40),
    // stats1h.liquidityChange — a large negative number is LP being pulled.
    minLiquidityChange: num("SCAN_MIN_LIQ_CHANGE_PCT", -25),
    // audit.devMints — how many tokens this deployer has ever minted.
    // Thousands means a serial launcher, not a project.
    maxDevMints:       num("SCAN_MAX_DEV_MINTS", 50),
    // Optional second opinion on LP lock (RugCheck summary, ~135 bytes).
    minLpLockedPct:    num("SCAN_MIN_LP_LOCKED_PCT", 0),
  },

  telegram: {
    botToken: process.env.TELEGRAM_BOT_TOKEN ?? "",
    chatId:   process.env.TELEGRAM_CHAT_ID   ?? "",
  },

  // Alerts stay off until the journal shows the signal is worth trusting.
  alertsEnabled: (process.env.SCAN_ALERTS_ENABLED ?? "false") === "true",

  paths: {
    state:   path.resolve(__dirname, "../../reports/sol_scanner_state.json"),
    journal: path.resolve(__dirname, "../../reports/sol_scanner_journal.jsonl"),
  },
} as const;
