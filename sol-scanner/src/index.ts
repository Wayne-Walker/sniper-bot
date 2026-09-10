import { config } from "./config";
import { log } from "./utils/logger";
import { PoolListener, NewPoolEvent } from "./listener/pool.listener";
import { TokenScorer } from "./scorer/token.scorer";
import { Watchlist } from "./watchlist/watchlist";
import { record } from "./journal/journal";
import { sendAlert } from "./utils/telegram";

const listener  = new PoolListener();
const scorer    = new TokenScorer();
const watchlist = new Watchlist();

let sweeping = false;
let sweepTimer: NodeJS.Timeout | null = null;
let saveTimer:  NodeJS.Timeout | null = null;

// ── Detect ───────────────────────────────────────────────────────────────────
// Detection does not trade and does not alert. It only enqueues.
async function onNewPool(e: NewPoolEvent): Promise<void> {
  if (watchlist.add(e.tokenMint, e.source, e.signature)) {
    log.info(`Queued [${e.source}] ${e.tokenMint.slice(0, 8)}... — scoring in ${config.maturity.minutes}m`);
  }
}

// ── Sweep ────────────────────────────────────────────────────────────────────
// Runs every recheckSeconds. Scores everything that has survived the maturity
// window. Serial, with a guard, so we never stampede RugCheck's rate limit.
async function sweep(): Promise<void> {
  if (sweeping) return;
  sweeping = true;
  try {
    const due = watchlist.due();
    if (due.length) log.info(`Sweep — ${due.length} matured candidate(s)`);

    for (const p of due) {
      const result = await scorer.score(p.mint);

      if (result.status === "rate_limited") {
        // Do NOT resolve — retry on the next sweep. Treating a rate limit as a
        // rejection is how the old scorer silently rejected everything.
        log.warn(`Rate limited on ${p.mint.slice(0, 8)}... — will retry`);
        watchlist.defer(p.mint);
        break;   // back off entirely for this sweep
      }

      record(p, result);
      watchlist.resolve(p.mint);

      const sym = result.metrics.symbol ?? "?";
      if (result.status === "pass") {
        log.success(`PASS ${sym} ${p.mint.slice(0, 8)}...`);
        await alert(p.mint, sym, p.source, result);
      } else {
        log.info(`fail ${sym} ${p.mint.slice(0, 8)}... — ${result.reasons[0]}`);
      }

      // Gentle pacing between full-report calls.
      await new Promise((r) => setTimeout(r, 1200));
    }
  } catch (err) {
    log.error(`Sweep error: ${err}`);
  } finally {
    sweeping = false;
  }
}

// ── Alert ────────────────────────────────────────────────────────────────────
async function alert(mint: string, symbol: string, source: string, r: any): Promise<void> {
  const m = r.metrics;
  const ageM = config.maturity.minutes;
  const body = [
    `🔎 *Solana survivor* — $${symbol}`,
    "",
    `\`${mint}\``,
    "",
    `venue      ${source}`,
    `survived   ${ageM}m`,
    `risk score ${m.riskScore} (lower = safer)`,
    `liquidity  $${Math.round(m.liquidityUsd ?? 0).toLocaleString()}`,
    `LP locked  ${(m.lpLockedPct ?? 0).toFixed(1)}%`,
    `holders    ${m.holders}`,
    `top holder ${(m.topHolderPct ?? 0).toFixed(1)}%  |  top10 ${(m.top10Pct ?? 0).toFixed(1)}%`,
    `insiders   ${(m.insiderPct ?? 0).toFixed(1)}%`,
    "",
    `https://dexscreener.com/solana/${mint}`,
    "",
    `_Scanner output — not a trade signal. Verify before acting._`,
  ].join("\n");

  if (!config.alertsEnabled) {
    log.warn(`[alerts off] would have alerted $${symbol} — journaled only`);
    return;
  }
  await sendAlert(body);
}

// ── Bootstrap ────────────────────────────────────────────────────────────────
async function main(): Promise<void> {
  console.log("\n╔══════════════════════════════════════════╗");
  console.log(  "║   Solana Survivor Scanner — alert only   ║");
  console.log(  "╚══════════════════════════════════════════╝\n");

  log.info(`Maturity window : ${config.maturity.minutes}m`);
  log.info(`Sweep interval  : ${config.maturity.recheckSeconds}s`);
  log.info(`Alerts          : ${config.alertsEnabled ? "ENABLED" : "OFF (journal only)"}`);
  log.info(`Journal         : ${config.paths.journal}`);
  const s = watchlist.stats();
  log.info(`Watchlist       : ${s.pending} pending, ${s.seen} seen`);

  listener.onNewPool(onNewPool);
  listener.start();

  sweepTimer = setInterval(() => void sweep(), config.maturity.recheckSeconds * 1000);
  saveTimer  = setInterval(() => watchlist.save(), 60_000);

  log.success("Scanner running — watching for new pools.\n");

  process.on("SIGINT",  shutdown);
  process.on("SIGTERM", shutdown);
}

function shutdown(): void {
  log.warn("Shutting down...");
  if (sweepTimer) clearInterval(sweepTimer);
  if (saveTimer)  clearInterval(saveTimer);
  listener.stop();
  watchlist.save();
  process.exit(0);
}

main().catch((err) => {
  log.error(`Fatal: ${err}`);
  process.exit(1);
});
