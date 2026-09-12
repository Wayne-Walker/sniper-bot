import axios from "axios";
import { config } from "../config";
import { log } from "../utils/logger";

// ── Shape of a Jupiter token record (only the fields we rely on) ────────────
export interface JupStats {
  priceChange?:     number;
  holderChange?:    number;
  liquidityChange?: number;
  buyVolume?:       number;
  sellVolume?:      number;
  buyOrganicVolume?: number;
  numBuys?:         number;
  numSells?:        number;
  numTraders?:      number;
  numOrganicBuyers?: number;
  numNetBuyers?:    number;
}

export interface JupToken {
  id:        string;          // mint
  name?:     string;
  symbol?:   string;
  dev?:      string;
  launchpad?: string;
  holderCount?: number;
  liquidity?:   number;
  mcap?:        number;
  fdv?:         number;
  usdPrice?:    number;
  organicScore?: number;
  organicScoreLabel?: string;
  graduatedAt?: string;
  firstPool?: { id?: string; createdAt?: string };
  audit?: {
    mintAuthorityDisabled?:   boolean;
    freezeAuthorityDisabled?: boolean;
    topHoldersPercentage?:    number;
    devBalancePercentage?:    number;
    devMints?:                number;
    devMigrations?:           number;
  };
  stats5m?:  JupStats;
  stats1h?:  JupStats;
  stats6h?:  JupStats;
  stats24h?: JupStats;
}

export class JupiterFeed {
  /**
   * One ranked query returns the tokens Jupiter currently rates highest over
   * the window. We then filter by age locally.
   *
   * Deliberately NOT /tokens/v2/recent: that feed rotates 100% every ~42s
   * (~43 new mints/minute), so birth-tracking would both miss most tokens and
   * need one lookup per mint at maturity — far past the 60 req/min budget.
   * Ranking by organic score and filtering on age gets survivors in 1 request.
   */
  async fetchCandidates(): Promise<JupToken[]> {
    const url = `${config.jupiter.baseUrl}/tokens/v2/toporganicscore/` +
                `${config.jupiter.window}?limit=${config.jupiter.limit}`;
    const res = await axios.get(url, {
      timeout: 20_000,
      validateStatus: () => true,
      headers: { accept: "application/json" },
    });

    if (res.status === 429) {
      log.warn("Jupiter rate limited (429) — skipping this poll");
      return [];
    }
    if (res.status !== 200) {
      log.warn(`Jupiter HTTP ${res.status} — skipping this poll`);
      return [];
    }
    if (!Array.isArray(res.data)) {
      log.warn("Jupiter returned a non-array payload — skipping");
      return [];
    }
    return res.data as JupToken[];
  }
}

/** Age of the token's first pool, in minutes. Infinity if unknown. */
export function ageMinutes(t: JupToken): number {
  const created = t.firstPool?.createdAt ?? t.graduatedAt;
  if (!created) return Infinity;
  const ms = Date.parse(created);
  if (!Number.isFinite(ms)) return Infinity;
  return (Date.now() - ms) / 60_000;
}
