import fs from "fs";
import path from "path";
import { config } from "../config";
import { log } from "../utils/logger";
import type { ScoreResult } from "../scorer/token.scorer";
import type { Pending } from "../watchlist/watchlist";

// Every verdict is journaled, pass or fail. This is what lets the scanner earn
// trust before it is allowed to ping anyone — the same discipline discovery_scan
// applies to its breakout tiers. Append-only JSONL, one verdict per line.
export function record(p: Pending, result: ScoreResult): void {
  try {
    fs.mkdirSync(path.dirname(config.paths.journal), { recursive: true });
    fs.appendFileSync(config.paths.journal, JSON.stringify({
      ts:        new Date().toISOString(),
      mint:      p.mint,
      symbol:    result.metrics.symbol ?? "?",
      source:    p.source,
      status:    result.status,
      ageMin:    Math.round((Date.now() - p.firstSeen) / 60_000),
      reasons:   result.reasons,
      metrics:   result.metrics,
      signature: p.signature,
    }) + "\n");
  } catch (err) {
    log.warn(`Journal write failed: ${err}`);
  }
}
