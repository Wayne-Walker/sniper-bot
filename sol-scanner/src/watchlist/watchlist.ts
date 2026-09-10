import fs from "fs";
import path from "path";
import { config } from "../config";
import { log } from "../utils/logger";
import type { PoolSource } from "../listener/pool.listener";

export interface Pending {
  mint:      string;
  source:    PoolSource;
  signature: string;
  firstSeen: number;   // epoch ms
  attempts:  number;
}

// The scanner's core inversion: a detected pool is NOT a candidate. It goes on
// a watchlist and is only scored once it has survived `maturity.minutes`. Most
// launches are gone by then, and that is the point — we want the survivors.
export class Watchlist {
  private pending = new Map<string, Pending>();
  private seen    = new Set<string>();

  constructor() { this.load(); }

  has(mint: string): boolean {
    return this.pending.has(mint) || this.seen.has(mint);
  }

  add(mint: string, source: PoolSource, signature: string): boolean {
    if (this.has(mint)) return false;
    if (this.pending.size >= config.maturity.maxPending) {
      log.warn(`Watchlist full (${config.maturity.maxPending}) — dropping ${mint.slice(0, 8)}...`);
      return false;
    }
    this.pending.set(mint, { mint, source, signature, firstSeen: Date.now(), attempts: 0 });
    return true;
  }

  // Entries whose maturity window has elapsed.
  due(): Pending[] {
    const cutoff = Date.now() - config.maturity.minutes * 60_000;
    return [...this.pending.values()].filter((p) => p.firstSeen <= cutoff);
  }

  // Resolved — scored and done. Kept in `seen` so it is never re-queued.
  resolve(mint: string): void {
    this.pending.delete(mint);
    this.seen.add(mint);
  }

  // Transient failure (rate limit) — leave it pending for the next sweep.
  defer(mint: string): void {
    const p = this.pending.get(mint);
    if (!p) return;
    p.attempts += 1;
    // Give up after ~10 tries so a permanently-unindexed mint can't wedge the queue.
    if (p.attempts >= 10) {
      log.warn(`Giving up on ${mint.slice(0, 8)}... after ${p.attempts} attempts`);
      this.resolve(mint);
    }
  }

  stats(): { pending: number; seen: number } {
    return { pending: this.pending.size, seen: this.seen.size };
  }

  // ── Persistence ─────────────────────────────────────────────────────────
  // Survives pm2 restarts — otherwise every restart throws away the whole
  // maturity queue and the scanner is blind for the next window.

  save(): void {
    try {
      fs.mkdirSync(path.dirname(config.paths.state), { recursive: true });
      const cutoff = Date.now() - 24 * 60 * 60_000;
      fs.writeFileSync(config.paths.state, JSON.stringify({
        pending: [...this.pending.values()],
        // Cap `seen` so the file can't grow without bound.
        seen:    [...this.seen].slice(-20_000),
        savedAt: Date.now(),
        cutoff,
      }, null, 2));
    } catch (err) {
      log.warn(`Could not save watchlist state: ${err}`);
    }
  }

  private load(): void {
    try {
      if (!fs.existsSync(config.paths.state)) return;
      const d = JSON.parse(fs.readFileSync(config.paths.state, "utf8"));
      // Drop anything older than 24h — a stale queue is noise, not signal.
      const cutoff = Date.now() - 24 * 60 * 60_000;
      for (const p of d.pending ?? []) {
        if (p.firstSeen >= cutoff) this.pending.set(p.mint, p);
      }
      for (const m of d.seen ?? []) this.seen.add(m);
      log.info(`Watchlist restored — ${this.pending.size} pending, ${this.seen.size} seen`);
    } catch (err) {
      log.warn(`Could not load watchlist state: ${err}`);
    }
  }
}
