import axios from "axios";
import { config } from "../config";
import { log } from "../utils/logger";
import { JupToken, ageMinutes } from "../feed/jupiter.feed";

export type ScoreStatus = "pass" | "fail";

export interface ScoreResult {
  status:  ScoreStatus;
  reasons: string[];
  metrics: Metrics;
}

export interface Metrics {
  symbol:        string;
  ageMin:        number;
  liquidityUsd:  number;
  holders:       number;
  topHoldersPct: number;
  organicScore:  number;
  organicLabel:  string;
  liqChange1h:   number;
  devMints:      number;
  mcap:          number;
  launchpad:     string;
  numTraders1h:  number;
  organicBuyers1h: number;
  organicRatio:  number;   // organic buy volume / total buy volume
  lpLockedPct:   number | null;   // null = not checked
}

function pct(n: number | undefined, fallback = 0): number {
  return typeof n === "number" && Number.isFinite(n) ? n : fallback;
}

export class TokenScorer {

  /**
   * Gates run entirely on the Jupiter payload — no per-token API call, so
   * scoring 100 candidates costs zero extra requests. Only a token that clears
   * every gate triggers the optional RugCheck LP-lock confirmation.
   */
  async score(t: JupToken): Promise<ScoreResult> {
    const a = t.audit ?? {};
    const s1 = t.stats1h ?? {};
    const buyVol = pct(s1.buyVolume);
    const m: Metrics = {
      symbol:        t.symbol ?? "?",
      ageMin:        ageMinutes(t),
      liquidityUsd:  pct(t.liquidity),
      holders:       pct(t.holderCount),
      topHoldersPct: pct(a.topHoldersPercentage, 100),
      organicScore:  pct(t.organicScore),
      organicLabel:  t.organicScoreLabel ?? "?",
      liqChange1h:   pct(s1.liquidityChange),
      devMints:      pct(a.devMints),
      mcap:          pct(t.mcap),
      launchpad:     t.launchpad ?? "?",
      numTraders1h:  pct(s1.numTraders),
      organicBuyers1h: pct(s1.numOrganicBuyers),
      organicRatio:  buyVol > 0 ? pct(s1.buyOrganicVolume) / buyVol : 0,
      lpLockedPct:   null,
    };

    const r: string[] = [];
    const g = config.gates;

    // ── Hard fails — authority still held means the supply isn't safe ───────
    if (a.mintAuthorityDisabled !== true)
      r.push("mint authority still active (supply can be inflated)");
    if (a.freezeAuthorityDisabled !== true)
      r.push("freeze authority still active (can freeze your tokens)");

    // ── Survival window ────────────────────────────────────────────────────
    if (m.ageMin < config.age.minMinutes)
      r.push(`too young: ${m.ageMin.toFixed(0)}m < ${config.age.minMinutes}m`);
    if (m.ageMin > config.age.maxMinutes)
      r.push(`too old: ${m.ageMin.toFixed(0)}m > ${config.age.maxMinutes}m`);

    // ── Substance ──────────────────────────────────────────────────────────
    if (m.liquidityUsd < g.minLiquidityUsd)
      r.push(`liquidity $${Math.round(m.liquidityUsd).toLocaleString()} < $${g.minLiquidityUsd.toLocaleString()}`);
    if (m.holders < g.minHolders)
      r.push(`${m.holders} holders < ${g.minHolders}`);
    if (m.topHoldersPct > g.maxTopHoldersPct)
      r.push(`top holders ${m.topHoldersPct.toFixed(1)}% > ${g.maxTopHoldersPct}%`);
    if (m.organicScore < g.minOrganicScore)
      r.push(`organic score ${m.organicScore.toFixed(0)} < ${g.minOrganicScore}`);

    // ── Rug-in-progress: liquidity being pulled right now ──────────────────
    if (m.liqChange1h < g.minLiquidityChange)
      r.push(`liquidity ${m.liqChange1h.toFixed(0)}% in 1h (draining, floor ${g.minLiquidityChange}%)`);

    // ── Serial launcher: a deployer with thousands of mints is a factory ───
    if (m.devMints > g.maxDevMints)
      r.push(`dev has minted ${m.devMints} tokens > ${g.maxDevMints}`);

    if (r.length) return { status: "fail", reasons: r, metrics: m };

    // ── Optional LP-lock confirmation (only for finalists) ─────────────────
    if (g.minLpLockedPct > 0) {
      const lp = await this.lpLockedPct(t.id);
      m.lpLockedPct = lp;
      if (lp !== null && lp < g.minLpLockedPct) {
        return {
          status: "fail",
          reasons: [`LP locked ${lp.toFixed(1)}% < ${g.minLpLockedPct}%`],
          metrics: m,
        };
      }
    }

    return { status: "pass", reasons: [], metrics: m };
  }

  /** RugCheck summary is ~135 bytes and is the only place LP-lock % is free.
   *  Returns null on any failure — a missing second opinion must not be
   *  mistaken for a failed one. */
  private async lpLockedPct(mint: string): Promise<number | null> {
    try {
      const res = await axios.get(
        `https://api.rugcheck.xyz/v1/tokens/${mint}/report/summary`,
        { timeout: 10_000, validateStatus: () => true },
      );
      if (res.status !== 200) return null;
      const v = res.data?.lpLockedPct;
      return typeof v === "number" ? v : null;
    } catch {
      log.warn(`LP-lock check failed for ${mint.slice(0, 8)}...`);
      return null;
    }
  }
}
