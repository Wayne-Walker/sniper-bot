import WebSocket from "ws";
import axios from "axios";
import { config } from "../config";
import { log } from "../utils/logger";

// ── Program IDs (all three verified on-chain via getAccountInfo) ─────────────
export const RAYDIUM_AMM_V4 = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8";
export const PUMP_FUN_PROG  = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P";
export const PUMPSWAP_AMM   = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA";

// Quote mints — never the "new token" side of a pool
const QUOTE_MINTS = new Set([
  "So11111111111111111111111111111111111111112", // WSOL
  "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", // USDC
  "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", // USDT
]);

export type PoolSource = "raydium" | "pumpfun" | "pumpswap";

export interface NewPoolEvent {
  source:    PoolSource;
  tokenMint: string;
  signature: string;
  timestamp: number;
}

type PoolHandler = (event: NewPoolEvent) => Promise<void>;

interface Sub { program: string; source: PoolSource; id: number; }

export class PoolListener {
  private ws:        WebSocket | null = null;
  private handlers:  PoolHandler[]    = [];
  private keepalive: NodeJS.Timeout | null = null;
  private reconnectMs = 3000;
  private stopped     = false;
  private subIds      = new Map<number, PoolSource>();  // rpc id -> source
  private liveSubs    = new Map<number, PoolSource>();  // subscription id -> source

  private readonly subs: Sub[] = [
    { program: RAYDIUM_AMM_V4, source: "raydium",  id: 1 },
    { program: PUMP_FUN_PROG,  source: "pumpfun",  id: 2 },
    { program: PUMPSWAP_AMM,   source: "pumpswap", id: 3 },
  ];

  onNewPool(handler: PoolHandler): void { this.handlers.push(handler); }

  start(): void { this.connect(); }

  stop(): void {
    this.stopped = true;
    this.clearKeepalive();
    this.ws?.close();
  }

  // ── Connection ────────────────────────────────────────────────────────────

  private connect(): void {
    log.info("Connecting to Helius WebSocket...");
    this.ws = new WebSocket(config.helius.wsUrl);
    this.ws.on("open",    () => this.onOpen());
    this.ws.on("message", (d) => this.onMessage(d));
    this.ws.on("error",   (e) => log.error(`WS error: ${e.message}`));
    this.ws.on("close",   () => this.onClose());
  }

  private onOpen(): void {
    log.success("Helius WebSocket connected");
    // Reset backoff only after a genuinely successful open, so a healthy
    // reconnect doesn't inherit a 30s delay from an earlier outage.
    this.reconnectMs = 3000;
    this.subIds.clear();
    this.liveSubs.clear();

    for (const s of this.subs) this.subscribe(s);

    // One keepalive per connection — cleared on close so reconnects don't
    // stack timers firing at a dead socket.
    this.clearKeepalive();
    this.keepalive = setInterval(() => {
      if (this.ws?.readyState === WebSocket.OPEN) this.ws.ping();
    }, 30_000);
  }

  private clearKeepalive(): void {
    if (this.keepalive) { clearInterval(this.keepalive); this.keepalive = null; }
  }

  // logsSubscribe is plain Solana JSON-RPC and works on every Helius plan.
  // transactionSubscribe (the previous approach) is an Atlas/paid-tier method —
  // for a scanner the extra latency of a getTransaction follow-up is irrelevant.
  private subscribe(s: Sub): void {
    this.subIds.set(s.id, s.source);
    this.ws?.send(JSON.stringify({
      jsonrpc: "2.0",
      id:      s.id,
      method:  "logsSubscribe",
      params:  [{ mentions: [s.program] }, { commitment: "confirmed" }],
    }));
  }

  private onClose(): void {
    this.clearKeepalive();
    if (this.stopped) return;
    log.warn(`WebSocket closed. Reconnecting in ${this.reconnectMs}ms...`);
    setTimeout(() => this.connect(), this.reconnectMs);
    this.reconnectMs = Math.min(this.reconnectMs * 2, 30_000);
  }

  // ── Messages ──────────────────────────────────────────────────────────────

  private onMessage(raw: WebSocket.RawData): void {
    let payload: any;
    try { payload = JSON.parse(raw.toString()); }
    catch { return; }

    // Subscription confirmations. If these never arrive the bot is deaf, so we
    // surface it loudly rather than sitting silent and looking healthy.
    if (payload.id !== undefined && payload.result !== undefined) {
      const source = this.subIds.get(payload.id);
      if (source) {
        this.liveSubs.set(payload.result, source);
        log.success(`Subscribed: ${source} (sub id ${payload.result})`);
      }
      return;
    }
    if (payload.id !== undefined && payload.error) {
      log.error(`Subscription FAILED for id ${payload.id}: ${JSON.stringify(payload.error)}`);
      return;
    }

    const value = payload?.params?.result?.value;
    if (!value || value.err) return;   // skip failed transactions

    const source = this.liveSubs.get(payload.params.subscription);
    if (!source) return;

    const logs: string[] = value.logs ?? [];
    if (!this.isPoolCreation(logs, source)) return;

    void this.resolveAndEmit(value.signature, source);
  }

  private isPoolCreation(logs: string[], source: PoolSource): boolean {
    if (source === "raydium") {
      return logs.some((l) => l.includes("initialize2") || l.includes("InitializeInstruction2"));
    }
    if (source === "pumpswap") {
      return logs.some((l) => l.includes("Instruction: CreatePool"));
    }
    // pump.fun bonding-curve completion / migration to an AMM
    return logs.some((l) =>
      l.includes("Instruction: Migrate") ||
      l.includes("MigrateFundsFromBondingCurve") ||
      l.includes("Instruction: Withdraw")
    );
  }

  // ── Mint resolution ───────────────────────────────────────────────────────

  // The previous implementation guessed the mint by fixed account index
  // (accounts[8], accounts[4], accounts[2]). That is unreliable — versioned
  // transactions move accounts into lookup tables, so the index shifts and the
  // fee payer's wallet can be emitted as a "mint". Token balances name their
  // mint explicitly, so we read it from there instead.
  private async resolveAndEmit(signature: string, source: PoolSource): Promise<void> {
    try {
      const res = await axios.post(config.helius.rpcUrl, {
        jsonrpc: "2.0",
        id:      1,
        method:  "getTransaction",
        params:  [signature, {
          encoding: "jsonParsed",
          commitment: "confirmed",
          maxSupportedTransactionVersion: 0,
        }],
      }, { timeout: 15_000 });

      const meta = res.data?.result?.meta;
      if (!meta) return;

      const balances = [...(meta.postTokenBalances ?? []), ...(meta.preTokenBalances ?? [])];
      const mints: string[] = balances
        .map((b: { mint?: string }) => b?.mint)
        .filter((m: string | undefined): m is string => typeof m === "string" && !QUOTE_MINTS.has(m));

      const tokenMint = mints[0];
      if (!tokenMint) return;   // SOL/stable-only pool — nothing new here

      this.emit({ source, tokenMint, signature, timestamp: Date.now() });
    } catch (err) {
      log.warn(`Could not resolve tx ${signature.slice(0, 8)}...: ${err}`);
    }
  }

  private emit(event: NewPoolEvent): void {
    for (const h of this.handlers) {
      h(event).catch((err) => log.error(`Handler error: ${err}`));
    }
  }
}
