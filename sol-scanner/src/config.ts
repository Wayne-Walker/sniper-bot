import dotenv from "dotenv";
import path from "path";

// .env lives at the repo root, shared with the Python bots
dotenv.config({ path: path.resolve(__dirname, "../../.env") });

function requireEnv(key: string): string {
  const val = process.env[key];
  if (!val) throw new Error(`Missing required env var: ${key}`);
  return val;
}

function num(key: string, fallback: number): number {
  const raw = process.env[key];
  if (raw === undefined || raw.trim() === "") return fallback;
  // .env in this repo uses trailing "# comment" on value lines
  const parsed = parseFloat(raw.split("#")[0].trim());
  return Number.isFinite(parsed) ? parsed : fallback;
}

export const config = {
  helius: {
    rpcUrl: requireEnv("HELIUS_RPC_URL"),
    wsUrl:  requireEnv("HELIUS_WS_URL"),
  },

  // How long a token must SURVIVE before it is even scored. This is the whole
  // point of the scanner: we are not racing to be first, we are waiting to see
  // which pools are still standing. Nothing is alerted before this.
  maturity: {
    minutes:        num("SCAN_MATURITY_MINUTES", 15),
    recheckSeconds: num("SCAN_RECHECK_SECONDS", 60),
    maxPending:     num("SCAN_MAX_PENDING", 5000),
  },

  // Gates applied at maturity. Deliberately strict — a scanner that alerts on
  // everything is noise, and noise is what makes an alert channel worthless.
  gates: {
    maxRiskScore:        num("SCAN_MAX_RISK_SCORE", 20),   // RugCheck: LOW = SAFE
    minLpLockedPct:      num("SCAN_MIN_LP_LOCKED_PCT", 50),
    minLiquidityUsd:     num("SCAN_MIN_LIQUIDITY_USD", 15000),
    maxTopHolderPct:     num("SCAN_MAX_TOP_HOLDER_PCT", 15),
    maxTop10HolderPct:   num("SCAN_MAX_TOP10_HOLDER_PCT", 40),
    minHolders:          num("SCAN_MIN_HOLDERS", 75),
    maxInsiderPct:       num("SCAN_MAX_INSIDER_PCT", 10),
  },

  telegram: {
    botToken: process.env.TELEGRAM_BOT_TOKEN ?? "",
    chatId:   process.env.TELEGRAM_CHAT_ID   ?? "",
  },

  // Alerts are suppressed unless explicitly enabled, so a fresh deploy observes
  // quietly and journals before it ever pings the phone.
  alertsEnabled: (process.env.SCAN_ALERTS_ENABLED ?? "false") === "true",

  paths: {
    // Follows discovery_scan.py's convention: runtime state under reports/
    state:   path.resolve(__dirname, "../../reports/sol_scanner_state.json"),
    journal: path.resolve(__dirname, "../../reports/sol_scanner_journal.jsonl"),
  },
} as const;
