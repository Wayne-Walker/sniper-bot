import axios from "axios";
import { config } from "../config";
import { log } from "../utils/logger";

const API = "https://api.rugcheck.xyz/v1/tokens";

// ── Result ───────────────────────────────────────────────────────────────────
// "rate_limited" is deliberately distinct from "fail". The previous version
// collapsed every error into passed:false, which meant that under rate limiting
// the bot rejected 100% of tokens while looking perfectly healthy. A scanner
// that silently stops scanning is worse than one that crashes.
export type ScoreStatus = "pass" | "fail" | "rate_limited" | "error";

export interface ScoreResult {
  status:  ScoreStatus;
  reasons: string[];
  metrics: Partial<Metrics>;
}

export interface Metrics {
  riskScore:      number;   // RugCheck `score_normalised` — LOW = SAFE
  lpLockedPct:    number;
  liquidityUsd:   number;
  topHolderPct:   number;
  top10Pct:       number;
  insiderPct:     number;
  holders:        number;
  mintAuthority:  boolean;
  freezeAuthority: boolean;
  rugged:         boolean;
  risks:          string[];
  symbol:         string;
}

class RateLimited extends Error {}

export class TokenScorer {

  async score(mint: string): Promise<ScoreResult> {
    try {
      // Stage 1 — the summary endpoint is ~135 bytes and carries the three
      // cheapest disqualifiers. Most candidates die here, which keeps us well
      // inside RugCheck's rate limit.
      const summary = await this.get(`${API}/${mint}/report/summary`);

      const riskScore   = Number(summary.score_normalised ?? summary.score ?? 999);
      const lpLockedPct = Number(summary.lpLockedPct ?? 0);
      const risks       = (summary.risks ?? [])
        .map((r: { name?: string }) => r?.name)
        .filter(Boolean) as string[];

      const early: string[] = [];
      // RugCheck's score is a RISK score: 1 = safe (verified against USDC and
      // WIF, both of which return 1). Higher is worse. The previous code had
      // this inverted and would have rejected both.
      if (riskScore > config.gates.maxRiskScore) {
        early.push(`risk score ${riskScore} > ${config.gates.maxRiskScore}`);
      }
      if (lpLockedPct < config.gates.minLpLockedPct) {
        early.push(`LP locked ${lpLockedPct.toFixed(1)}% < ${config.gates.minLpLockedPct}%`);
      }
      if (early.length) {
        return { status: "fail", reasons: early, metrics: { riskScore, lpLockedPct, risks } };
      }

      // Stage 2 — the full report. Only reached by survivors. Note this can be
      // multi-megabyte on established tokens (WIF returned 1.8MB), hence the
      // generous timeout and size cap.
      const full = await this.get(`${API}/${mint}/report`, 30_000);
      return this.evaluate({ riskScore, lpLockedPct, risks }, full);

    } catch (err) {
      if (err instanceof RateLimited) {
        return { status: "rate_limited", reasons: ["RugCheck rate limited"], metrics: {} };
      }
      return { status: "error", reasons: [`scorer error: ${err}`], metrics: {} };
    }
  }

  // ── HTTP ────────────────────────────────────────────────────────────────

  private async get(url: string, timeout = 12_000): Promise<any> {
    const res = await axios.get(url, {
      timeout,
      maxContentLength: 32 * 1024 * 1024,
      validateStatus: () => true,
    });
    if (res.status === 429 || res.status === 503) throw new RateLimited();
    if (res.status !== 200) throw new Error(`HTTP ${res.status}`);
    return res.data;
  }

  // ── Gates ───────────────────────────────────────────────────────────────

  private evaluate(
    s: { riskScore: number; lpLockedPct: number; risks: string[] },
    full: any,
  ): ScoreResult {
    const topHolders: any[] = full.topHolders ?? [];

    // Exclude LP and known accounts (exchanges, lockers) from concentration —
    // an LP position holding most of the supply is normal, not a red flag.
    const known = full.knownAccounts ?? {};
    const real  = topHolders.filter((h) => {
      const k = known[h.owner] ?? known[h.address];
      const type = (k?.type ?? "").toLowerCase();
      return !h.insider && type !== "amm" && type !== "liquidity" && type !== "lp";
    });

    const m: Metrics = {
      riskScore:       s.riskScore,
      lpLockedPct:     s.lpLockedPct,
      risks:           s.risks,
      liquidityUsd:    Number(full.totalMarketLiquidity ?? 0),
      holders:         Number(full.totalHolders ?? 0),
      topHolderPct:    Number(real[0]?.pct ?? 100),
      top10Pct:        real.slice(0, 10).reduce((a, h) => a + Number(h.pct ?? 0), 0),
      insiderPct:      topHolders.filter((h) => h.insider)
                                 .reduce((a, h) => a + Number(h.pct ?? 0), 0),
      mintAuthority:   full.mintAuthority   != null,
      freezeAuthority: full.freezeAuthority != null,
      rugged:          Boolean(full.rugged),
      symbol:          full.tokenMeta?.symbol ?? full.fileMeta?.symbol ?? "?",
    };

    const reasons: string[] = [];
    const g = config.gates;

    // Hard fails — these are unconditional, not thresholds.
    if (m.rugged)          reasons.push("flagged as RUGGED");
    if (m.mintAuthority)   reasons.push("mint authority still active (supply can be inflated)");
    if (m.freezeAuthority) reasons.push("freeze authority still active (can freeze your tokens)");

    // Thresholds.
    if (m.liquidityUsd < g.minLiquidityUsd)
      reasons.push(`liquidity $${Math.round(m.liquidityUsd).toLocaleString()} < $${g.minLiquidityUsd.toLocaleString()}`);
    if (m.holders < g.minHolders)
      reasons.push(`${m.holders} holders < ${g.minHolders}`);
    if (m.topHolderPct > g.maxTopHolderPct)
      reasons.push(`top holder ${m.topHolderPct.toFixed(1)}% > ${g.maxTopHolderPct}%`);
    if (m.top10Pct > g.maxTop10HolderPct)
      reasons.push(`top 10 hold ${m.top10Pct.toFixed(1)}% > ${g.maxTop10HolderPct}%`);
    if (m.insiderPct > g.maxInsiderPct)
      reasons.push(`insiders hold ${m.insiderPct.toFixed(1)}% > ${g.maxInsiderPct}%`);

    return { status: reasons.length ? "fail" : "pass", reasons, metrics: m };
  }
}
