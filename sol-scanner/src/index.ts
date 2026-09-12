import fs from "fs";
import path from "path";
import { config } from "./config";
import { log } from "./utils/logger";
import { JupiterFeed, JupToken, ageMinutes } from "./feed/jupiter.feed";
import { TokenScorer } from "./scorer/token.scorer";
import { record } from "./journal/journal";
import { sendAlert } from "./utils/telegram";

const feed   = new JupiterFeed();
const scorer = new TokenScorer();

// Mints we have already ruled on, so a token sitting in the ranked feed for an
// hour is journaled and alerted exactly once.
const seen = new Set<string>();

let polling = false;
let pollTimer: NodeJS.Timeout | null = null;
let saveTimer: NodeJS.Timeout | null = null;

// ── Poll ─────────────────────────────────────────────────────────────────────
async function poll(): Promise<void> {
  if (polling) return;
  polling = true;
  try {
    const tokens = await feed.fetchCandidates();
    if (!tokens.length) return;

    // Age-filter first: it costs nothing and removes the established tokens
    // that dominate a score-ranked feed.
    const inWindow = tokens.filter((t: JupToken) => {
      const a = ageMinutes(t);
      return a >= config.age.minMinutes && a <= config.age.maxMinutes;
    });

    const fresh = inWindow.filter((t) => !seen.has(t.id));
    log.info(`Poll — ${tokens.length} ranked · ${inWindow.length} in age window · ${fresh.length} new`);

    for (const t of fresh) {
      seen.add(t.id);
      const result = await scorer.score(t);
      record(t.id, result);

      if (result.status === "pass") {
        log.success(`PASS $${result.metrics.symbol} ${t.id.slice(0, 8)}...`);
        await alert(t.id, result);
      } else {
        log.info(`fail $${result.metrics.symbol} — ${result.reasons[0]}`);
      }
    }
  } catch (err) {
    log.error(`Poll error: ${err}`);
  } finally {
    polling = false;
  }
}

// ── Alert ────────────────────────────────────────────────────────────────────
async function alert(mint: string, r: { metrics: any }): Promise<void> {
  const m = r.metrics;
  const body = [
    `🔎 *Solana survivor* — $${m.symbol}`,
    "",
    `\`${mint}\``,
    "",
    `age        ${m.ageMin.toFixed(0)}m on ${m.launchpad}`,
    `liquidity  $${Math.round(m.liquidityUsd).toLocaleString()}  (${m.liqChange1h >= 0 ? "+" : ""}${m.liqChange1h.toFixed(0)}% 1h)`,
    `holders    ${m.holders}`,
    `organic    ${m.organicScore.toFixed(0)} (${m.organicLabel})  ·  ${(m.organicRatio * 100).toFixed(1)}% of buy vol`,
    `top hldrs  ${m.topHoldersPct.toFixed(1)}%`,
    `dev mints  ${m.devMints}`,
    m.lpLockedPct !== null ? `LP locked  ${m.lpLockedPct.toFixed(1)}%` : "",
    "",
    `https://dexscreener.com/solana/${mint}`,
    "",
    `_Scanner output — not a trade signal. Verify before acting._`,
  ].filter(Boolean).join("\n");

  if (!config.alertsEnabled) {
    log.warn(`[alerts off] would have alerted $${m.symbol} — journaled only`);
    return;
  }
  await sendAlert(body);
}

// ── Seen-set persistence (so a restart doesn't re-alert) ────────────────────
function save(): void {
  try {
    fs.mkdirSync(path.dirname(config.paths.state), { recursive: true });
    fs.writeFileSync(config.paths.state, JSON.stringify({
      seen:    [...seen].slice(-20_000),
      savedAt: Date.now(),
    }, null, 2));
  } catch (err) {
    log.warn(`Could not save state: ${err}`);
  }
}

function load(): void {
  try {
    if (!fs.existsSync(config.paths.state)) return;
    const d = JSON.parse(fs.readFileSync(config.paths.state, "utf8"));
    for (const m of d.seen ?? []) seen.add(m);
    log.info(`State restored — ${seen.size} mints already ruled on`);
  } catch (err) {
    log.warn(`Could not load state: ${err}`);
  }
}

// ── Bootstrap ────────────────────────────────────────────────────────────────
async function main(): Promise<void> {
  console.log("\n╔══════════════════════════════════════════╗");
  console.log(  "║   Solana Survivor Scanner — alert only   ║");
  console.log(  "╚══════════════════════════════════════════╝\n");

  load();
  log.info(`Source     : Jupiter ${config.jupiter.window} organic-score top ${config.jupiter.limit} (free, no key)`);
  log.info(`Age window : ${config.age.minMinutes}–${config.age.maxMinutes} min`);
  log.info(`Poll       : every ${config.jupiter.pollSeconds}s (~${(60 / config.jupiter.pollSeconds).toFixed(1)} req/min of a 60/min allowance)`);
  log.info(`Alerts     : ${config.alertsEnabled ? "ENABLED" : "OFF (journal only)"}`);
  log.info(`Journal    : ${config.paths.journal}`);

  await poll();
  pollTimer = setInterval(() => void poll(), config.jupiter.pollSeconds * 1000);
  saveTimer = setInterval(save, 60_000);

  log.success("Scanner running.\n");
  process.on("SIGINT",  shutdown);
  process.on("SIGTERM", shutdown);
}

function shutdown(): void {
  log.warn("Shutting down...");
  if (pollTimer) clearInterval(pollTimer);
  if (saveTimer) clearInterval(saveTimer);
  save();
  process.exit(0);
}

main().catch((err) => {
  log.error(`Fatal: ${err}`);
  process.exit(1);
});
