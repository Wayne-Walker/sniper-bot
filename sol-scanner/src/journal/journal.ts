import fs from "fs";
import path from "path";
import { config } from "../config";
import { log } from "../utils/logger";
import type { ScoreResult } from "../scorer/token.scorer";

// Every verdict is journaled, pass or fail — this is what lets the scanner
// earn trust before it is allowed to ping anyone. Append-only JSONL.
// Read weekly by ../sol_scanner_review.py.
export function record(mint: string, result: ScoreResult): void {
  try {
    fs.mkdirSync(path.dirname(config.paths.journal), { recursive: true });
    fs.appendFileSync(config.paths.journal, JSON.stringify({
      ts:      new Date().toISOString(),
      mint,
      symbol:  result.metrics.symbol,
      source:  result.metrics.launchpad,
      status:  result.status,
      ageMin:  Math.round(result.metrics.ageMin),
      reasons: result.reasons,
      metrics: result.metrics,
    }) + "\n");
  } catch (err) {
    log.warn(`Journal write failed: ${err}`);
  }
}
