"""
fills_fetcher.py
────────────────
Fetches raw fills via userFillsByTime and reconstructs complete
round-trip trades by pairing Open fills with their matching Close fills.

Each fill has a `dir` field:
  "Open Long"   — entered a long
  "Close Long"  — exited a long  (closedPnl is realised PnL)
  "Open Short"  — entered a short
  "Close Short" — exited a short (closedPnl is realised PnL)

We match opens to closes by coin, building a FIFO queue per coin/direction.
"""

import time
import requests
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from src.config import config
from src.utils.logger import get_logger

log = get_logger(__name__)

HL_INFO_URL = "https://api.hyperliquid.xyz/info"


@dataclass
class CompletedTrade:
    wallet:       str
    coin:         str
    direction:    str        # "long" or "short"

    open_time:    datetime
    close_time:   datetime
    duration_hrs: float      # how long the trade was open

    open_price:   float      # avg entry price (size-weighted if partial fills)
    close_price:  float      # avg exit price

    size:         float      # total size traded (in coins)
    notional_usd: float      # open_price * size

    closed_pnl:   float      # realised PnL in USD
    fees:         float      # total fees paid
    net_pnl:      float      # closed_pnl - fees
    roi_pct:      float      # net_pnl / (notional / leverage) * 100

    leverage:     float      # estimated from fill data (size * price / margin)
    max_size:     float      # peak position size during trade

    open_tx:      str        # transaction hash of open fill
    close_tx:     str        # transaction hash of close fill

    # Convenience
    @property
    def open_date(self) -> str:
        return self.open_time.strftime("%Y-%m-%d %H:%M")

    @property
    def close_date(self) -> str:
        return self.close_time.strftime("%Y-%m-%d %H:%M")

    @property
    def duration_str(self) -> str:
        h = self.duration_hrs
        if h < 1:
            return f"{int(h * 60)}m"
        if h < 24:
            return f"{h:.1f}h"
        return f"{h/24:.1f}d"


@dataclass
class _OpenFill:
    """Internal — an unmatched open fill waiting for a close."""
    time:    datetime
    price:   float
    size:    float
    fee:     float
    tx_hash: str


@dataclass
class CurrentPosition:
    """A position that is currently open — not yet closed."""
    wallet:         str
    coin:           str
    direction:      str    # "long" or "short"
    size:           float
    notional_usd:   float
    entry_price:    float
    mark_price:     float
    unrealized_pnl: float
    leverage:       float
    roi_pct:        float

    @property
    def open_since(self) -> str:
        """We don't have open time from clearinghouseState — show N/A."""
        return "N/A"


class FillsFetcher:

    def fetch_all(self, wallets: list) -> dict:
        """
        Returns dict: {address: List[CompletedTrade]}
        """
        results = {}
        total   = len(wallets)
        log.info(f"Fetching {config.LOOKBACK_DAYS}d fills for {total} wallets...")

        with ThreadPoolExecutor(max_workers=config.MAX_CONCURRENT_FETCHES) as ex:
            futures = {ex.submit(self._fetch_wallet, w): w for w in wallets}
            done    = 0
            for future in as_completed(futures):
                w    = futures[future]
                done += 1
                try:
                    trades = future.result()
                    results[w.address] = trades
                    log.info(f"[{done}/{total}] {w.address[:10]}... — {len(trades)} completed trades")
                except Exception as e:
                    log.warning(f"[{done}/{total}] {w.address[:10]}... failed: {e}")
                    results[w.address] = []

        return results

    def fetch_open_positions(self, wallets: list) -> dict:
        """
        Fetch current open positions for all wallets via clearinghouseState.
        Uses lower concurrency and retry logic to avoid 429 rate limit errors.
        Returns dict: {address: List[CurrentPosition]}
        """
        results = {}
        total   = len(wallets)
        # Use lower concurrency for open positions — clearinghouseState is heavier
        concurrency = min(3, config.MAX_CONCURRENT_FETCHES)
        log.info(f"Fetching current open positions for {total} wallets (concurrency={concurrency})...")

        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            futures = {ex.submit(self._fetch_open_positions_with_retry, w.address): w for w in wallets}
            done    = 0
            for future in as_completed(futures):
                w    = futures[future]
                done += 1
                try:
                    positions = future.result()
                    results[w.address] = positions
                    if positions:
                        log.info(f"[{done}/{total}] {w.address[:10]}... — {len(positions)} open positions")
                    else:
                        log.info(f"[{done}/{total}] {w.address[:10]}... — no open positions")
                except Exception as e:
                    log.warning(f"[{done}/{total}] {w.address[:10]}... open pos failed: {e}")
                    results[w.address] = []

        return results

    def fetch_with_extended_lookback(self, wallet, multiplier: int = 3) -> List:
        """
        Re-fetch with a longer lookback window when no trades found in default window.
        Uses multiplier * LOOKBACK_DAYS.
        """
        extended_days = config.LOOKBACK_DAYS * multiplier
        log.info(f"No trades found — retrying with {extended_days}d lookback for {wallet.address[:10]}...")
        fills  = self._get_fills(wallet.address, days_override=extended_days)
        trades = self._reconstruct_trades(wallet.address, fills)
        return trades

    # ── Private ───────────────────────────────────────────────────────────────

    def _fetch_wallet(self, wallet) -> List[CompletedTrade]:
        fills  = self._get_fills(wallet.address)
        trades = self._reconstruct_trades(wallet.address, fills)
        if config.MIN_TRADE_PNL > -999999:
            trades = [t for t in trades if t.net_pnl >= config.MIN_TRADE_PNL]
        time.sleep(0.2)
        return trades

    def _get_fills(self, address: str, days_override: int = None) -> list:
        """
        Fetch fills in 30-day chunks to avoid the 2000-fill API cap.
        Works backwards from now to cover the full lookback window.
        """
        total_days  = days_override or config.LOOKBACK_DAYS
        chunk_days  = 30   # fetch 30 days at a time
        all_fills   = []
        seen_hashes = set()

        end_dt   = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(days=total_days)

        # Walk backwards in 30-day chunks from now → start
        chunk_end = end_dt
        while chunk_end > start_dt:
            chunk_start = max(chunk_end - timedelta(days=chunk_days), start_dt)

            start_ms = int(chunk_start.timestamp() * 1000)
            end_ms   = int(chunk_end.timestamp() * 1000)

            try:
                chunk_fills = self._post_with_retry(HL_INFO_URL, {
                    "type":            "userFillsByTime",
                    "user":            address,
                    "startTime":       start_ms,
                    "endTime":         end_ms,
                    "aggregateByTime": True,
                }) or []

                # Dedup by hash in case of overlap at chunk boundaries
                new_fills = [f for f in chunk_fills if f.get("hash") not in seen_hashes]
                for f in new_fills:
                    seen_hashes.add(f.get("hash"))
                all_fills.extend(new_fills)

                log.info(
                    f"  chunk {chunk_start.strftime('%Y-%m-%d')} → "
                    f"{chunk_end.strftime('%Y-%m-%d')}: "
                    f"{len(chunk_fills)} fills ({len(all_fills)} total so far)"
                )

            except Exception as e:
                log.warning(f"  chunk failed ({chunk_start.strftime('%Y-%m-%d')} → "
                            f"{chunk_end.strftime('%Y-%m-%d')}): {e}")

            chunk_end = chunk_start
            time.sleep(0.15)  # polite rate limiting between chunks

        log.info(f"  total fills fetched across all chunks: {len(all_fills)}")
        return all_fills

    def _post_with_retry(self, url: str, payload: dict, max_retries: int = 4) -> dict:
        """POST with exponential backoff on 429 rate limit errors."""
        delay = 2.0
        for attempt in range(max_retries):
            try:
                resp = requests.post(
                    url, json=payload, timeout=15,
                    headers={"Content-Type": "application/json"},
                )
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code == 429:
                    if attempt < max_retries - 1:
                        log.warning(f"Rate limited — retrying in {delay:.0f}s (attempt {attempt + 1}/{max_retries})")
                        time.sleep(delay)
                        delay *= 2
                    else:
                        raise
                else:
                    raise
        return {}

    def _fetch_open_positions_with_retry(self, address: str, max_retries: int = 4) -> List[CurrentPosition]:
        """Fetch open positions with exponential backoff on 429 rate limit errors."""
        delay = 2.0
        for attempt in range(max_retries):
            try:
                return self._fetch_open_positions(address)
            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code == 429:
                    if attempt < max_retries - 1:
                        log.warning(
                            f"Rate limited fetching {address[:10]}... "
                            f"— retrying in {delay:.0f}s (attempt {attempt + 1}/{max_retries})"
                        )
                        time.sleep(delay)
                        delay *= 2   # exponential backoff: 2s, 4s, 8s, 16s
                    else:
                        log.error(f"Rate limit exceeded for {address[:10]}... after {max_retries} retries")
                        return []
                else:
                    raise
            except Exception as e:
                log.warning(f"Error fetching {address[:10]}...: {e}")
                return []
        return []

    def _fetch_open_positions(self, address: str) -> List[CurrentPosition]:
        data = self._post_with_retry(
            HL_INFO_URL,
            {"type": "clearinghouseState", "user": address},
        )

        positions = []
        for ap in data.get("assetPositions", []):
            pos = ap.get("position", {})
            if not pos:
                continue
            szi = float(pos.get("szi", 0))
            if szi == 0:
                continue

            entry_px   = float(pos.get("entryPx", 0) or 0)
            unrealized = float(pos.get("unrealizedPnl", 0) or 0)
            notional   = abs(float(pos.get("positionValue", 0) or 0))
            lev_data   = pos.get("leverage", {})
            leverage   = float(lev_data.get("value", 1) if isinstance(lev_data, dict) else 1)
            mark_px    = (notional / abs(szi)) if szi != 0 else entry_px
            roi_pct    = (unrealized / (notional / leverage) * 100) if notional > 0 else 0

            positions.append(CurrentPosition(
                wallet         = address,
                coin           = pos.get("coin", ""),
                direction      = "long" if szi > 0 else "short",
                size           = abs(szi),
                notional_usd   = notional,
                entry_price    = entry_px,
                mark_price     = mark_px,
                unrealized_pnl = unrealized,
                leverage       = leverage,
                roi_pct        = roi_pct,
            ))

        time.sleep(0.1)
        return positions

    def _reconstruct_trades(self, address: str, fills: list) -> List[CompletedTrade]:
        """
        Pair Open fills with Close fills per coin to build round-trip trades.
        Uses FIFO matching — earliest open matched with earliest close.
        """
        open_queue: dict = {}
        completed: List[CompletedTrade] = []

        # Sort fills chronologically
        fills = sorted(fills, key=lambda f: f.get("time", 0))

        # ── Diagnostic: log all unique dir values seen ────────────────────────
        dir_values = set(f.get("dir", "") for f in fills)
        log.info(f"[{address[:10]}] {len(fills)} fills fetched | dir values seen: {dir_values}")

        # Log the 5 most recent fills so we can see what's coming back
        recent = sorted(fills, key=lambda f: f.get("time", 0), reverse=True)[:5]
        for f in recent:
            ts = datetime.fromtimestamp(f.get("time", 0) / 1000, tz=timezone.utc)
            log.info(
                f"  fill: {ts.strftime('%Y-%m-%d %H:%M')}  "
                f"coin={f.get('coin','')}  "
                f"dir={f.get('dir','')}  "
                f"sz={f.get('sz','')}  "
                f"px={f.get('px','')}  "
                f"closedPnl={f.get('closedPnl','')}"
            )
        # ─────────────────────────────────────────────────────────────────────

        for fill in fills:
            direction_raw = fill.get("dir", "")
            coin          = fill.get("coin", "")
            px            = float(fill.get("px", 0))
            sz            = float(fill.get("sz", 0))
            fee           = float(fill.get("fee", 0))
            ts            = datetime.fromtimestamp(fill.get("time", 0) / 1000, tz=timezone.utc)
            tx_hash       = fill.get("hash", "")
            closed_pnl    = float(fill.get("closedPnl", 0))

            if not coin or sz == 0:
                continue

            # Parse direction
            if direction_raw in ("Open Long",):
                direction = "long"
                is_open   = True
            elif direction_raw in ("Close Long",):
                direction = "long"
                is_open   = False
            elif direction_raw in ("Open Short",):
                direction = "short"
                is_open   = True
            elif direction_raw in ("Close Short",):
                direction = "short"
                is_open   = False
            else:
                continue  # skip unknowns like liquidation fills

            key = (coin, direction)
            open_queue.setdefault(key, [])

            if is_open:
                open_queue[key].append(_OpenFill(
                    time=ts, price=px, size=sz, fee=fee, tx_hash=tx_hash
                ))
            else:
                if not open_queue[key]:
                    # ── Orphaned close — open was before the lookback window ──
                    # We still have the PnL and close price from the fill.
                    # Reconstruct a partial trade — open price estimated from
                    # close price and closedPnl, open time marked as unknown.
                    # notional based on close price * size since open price unknown
                    notional   = px * sz
                    net_pnl    = closed_pnl - fee
                    roi        = (net_pnl / notional * 100) if notional > 0 else 0

                    # Estimate open price: price = close_price - (pnl/size) for long
                    if direction == "long" and sz > 0:
                        est_open_px = px - (closed_pnl / sz)
                    elif direction == "short" and sz > 0:
                        est_open_px = px + (closed_pnl / sz)
                    else:
                        est_open_px = px

                    # Use close time as both open/close — duration unknown
                    completed.append(CompletedTrade(
                        wallet       = address,
                        coin         = coin,
                        direction    = direction,
                        open_time    = ts,   # unknown — using close time as placeholder
                        close_time   = ts,
                        duration_hrs = 0.0,  # unknown
                        open_price   = est_open_px,
                        close_price  = px,
                        size         = sz,
                        notional_usd = notional,
                        closed_pnl   = closed_pnl,
                        fees         = fee,   # only close-side fee known
                        net_pnl      = net_pnl,
                        roi_pct      = roi,
                        leverage     = 1.0,
                        max_size     = sz,
                        open_tx      = "pre-window",
                        close_tx     = tx_hash,
                    ))
                    continue

                open_fill = open_queue[key].pop(0)

                # Build the completed trade
                notional    = open_fill.price * sz
                total_fees  = open_fill.fee + fee
                net_pnl     = closed_pnl - total_fees
                duration_hr = (ts - open_fill.time).total_seconds() / 3600
                roi         = (net_pnl / notional * 100) if notional > 0 else 0

                completed.append(CompletedTrade(
                    wallet       = address,
                    coin         = coin,
                    direction    = direction,
                    open_time    = open_fill.time,
                    close_time   = ts,
                    duration_hrs = max(duration_hr, 0),
                    open_price   = open_fill.price,
                    close_price  = px,
                    size         = sz,
                    notional_usd = notional,
                    closed_pnl   = closed_pnl,
                    fees         = total_fees,
                    net_pnl      = net_pnl,
                    roi_pct      = roi,
                    leverage     = 1.0,
                    max_size     = max(open_fill.size, sz),
                    open_tx      = open_fill.tx_hash,
                    close_tx     = tx_hash,
                ))

        return completed
