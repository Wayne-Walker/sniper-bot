import requests
import time
from dataclasses import dataclass
from typing import List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from src.config import config
from src.utils.logger import get_logger

log = get_logger(__name__)

HL_INFO_URL = "https://api.hyperliquid.xyz/info"


@dataclass
class OpenPosition:
    wallet:        str
    wallet_group:  str    # "winner" or "loser"
    coin:          str
    direction:     str    # "long" or "short"
    size:          float  # in coins
    notional_usd:  float
    entry_price:   float
    mark_price:    float
    unrealized_pnl: float
    leverage:      float
    roi_pct:       float
    wallet_pnl:    float  # owner's overall PnL for context


class PositionFetcher:
    """
    Fetches open perpetual positions for a list of wallet addresses
    using the clearinghouseState endpoint, concurrently.
    """

    def fetch_all(self, wallets: list) -> List[OpenPosition]:
        """
        wallets: list of WalletRank objects
        Returns flat list of OpenPosition across all wallets.
        """
        all_positions: List[OpenPosition] = []
        total = len(wallets)

        log.info(f"Fetching positions for {total} wallets (max {config.MAX_CONCURRENT_FETCHES} concurrent)...")

        with ThreadPoolExecutor(max_workers=config.MAX_CONCURRENT_FETCHES) as executor:
            future_to_wallet = {
                executor.submit(self._fetch_wallet_positions, w): w
                for w in wallets
            }
            completed = 0
            for future in as_completed(future_to_wallet):
                wallet = future_to_wallet[future]
                completed += 1
                try:
                    positions = future.result()
                    all_positions.extend(positions)
                    if positions:
                        log.info(
                            f"[{completed}/{total}] {wallet.address[:8]}... "
                            f"({wallet.group}) — {len(positions)} positions"
                        )
                    else:
                        log.info(f"[{completed}/{total}] {wallet.address[:8]}... — no open positions")
                except Exception as e:
                    log.warning(f"Failed to fetch {wallet.address[:8]}...: {e}")

        log.info(f"Total open positions fetched: {len(all_positions)}")
        return all_positions

    # ── Private ───────────────────────────────────────────────────────────────

    def _fetch_wallet_positions(self, wallet) -> List[OpenPosition]:
        """Fetch clearinghouseState for one wallet and parse positions."""
        try:
            resp = requests.post(
                HL_INFO_URL,
                json={"type": "clearinghouseState", "user": wallet.address},
                timeout=10,
                headers={"Content-Type": "application/json"}
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            raise RuntimeError(f"API error: {e}")

        positions = []
        asset_positions = data.get("assetPositions", [])

        for ap in asset_positions:
            pos = ap.get("position", {})
            if not pos:
                continue

            szi        = float(pos.get("szi", 0))
            if szi == 0:
                continue

            coin       = pos.get("coin", "")
            entry_px   = float(pos.get("entryPx", 0) or 0)
            unrealized = float(pos.get("unrealizedPnl", 0) or 0)
            leverage_v = pos.get("leverage", {})
            leverage   = float(leverage_v.get("value", 1) if isinstance(leverage_v, dict) else 1)
            notional   = abs(float(pos.get("positionValue", 0) or 0))
            mark_px    = (notional / abs(szi)) if szi != 0 else entry_px
            roi_pct    = (unrealized / (notional / leverage) * 100) if notional > 0 else 0

            positions.append(OpenPosition(
                wallet        = wallet.address,
                wallet_group  = wallet.group,
                coin          = coin,
                direction     = "long" if szi > 0 else "short",
                size          = abs(szi),
                notional_usd  = notional,
                entry_price   = entry_px,
                mark_price    = mark_px,
                unrealized_pnl = unrealized,
                leverage      = leverage,
                roi_pct       = roi_pct,
                wallet_pnl    = wallet.pnl,
            ))

        # Polite rate limit — 100ms between calls
        time.sleep(0.1)
        return positions
