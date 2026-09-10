import time
import requests, json, os
from dataclasses import dataclass
from typing import Optional
from hyperliquid.info import Info
from hyperliquid.utils import constants
from src.config import config
from src.utils.logger import get_logger

log = get_logger(__name__)

def _check_edge(coin: str, direction: str) -> tuple[bool, str]:
    """Read latest signals from smart money bot."""
    fn = os.path.join("..", "hl-smartmoney", "signals", "latest.json")
    try:
        with open(fn) as f:
            data = json.load(f)
        for s in data.get("signals", []):
            if s["coin"].upper() == coin.upper():
                if s["direction"] == direction:
                    return True, f"gap={s['gap_pct']:.1f}%"
                else:
                    return False, f"smart money says {s['direction']}"
        return False, "no signal above threshold"
    except FileNotFoundError:
        return True, "edge file not found — allowing trade"

@dataclass
class MomentumSignal:
    coin:         str
    direction:    str          # "long" or "short"
    entry_price:  float
    price_change_pct: float
    volume_usd:   float


class MomentumDetector:
    """
    Observes a newly listed coin for OBSERVATION_SECONDS.
    Samples price and volume at start and end of the window.
    Returns a MomentumSignal if the move exceeds MIN_MOMENTUM_PCT
    and volume exceeds MIN_VOLUME_USD, otherwise returns None.
    """

    def __init__(self) -> None:
        self._info = Info(constants.MAINNET_API_URL, skip_ws=True)

    def observe(self, coin: str) -> Optional[MomentumSignal]:
        log.info(f"[{coin}] Observing for {config.OBSERVATION_SECONDS}s...")

        # ── Snapshot at t=0 ───────────────────────────────────────────────────
        start_price  = self._get_mid_price(coin)
        if start_price is None:
            log.warning(f"[{coin}] Could not get start price — skipping")
            return None

        log.info(f"[{coin}] Start price: {start_price}")

        # ── Collect trades during observation window ───────────────────────────
        start_time   = time.time()
        volume_usd   = 0.0
        sample_count = 0

        while time.time() - start_time < config.OBSERVATION_SECONDS:
            time.sleep(2)
            trades = self._get_recent_trades(coin)
            for t in trades:
                volume_usd += float(t.get("px", 0)) * float(t.get("sz", 0))
            sample_count += 1

        # ── Snapshot at t=end ─────────────────────────────────────────────────
        end_price = self._get_mid_price(coin)
        if end_price is None:
            log.warning(f"[{coin}] Could not get end price — skipping")
            return None

        price_change_pct = ((end_price - start_price) / start_price) * 100
        log.info(
            f"[{coin}] End price: {end_price} | "
            f"Change: {price_change_pct:+.2f}% | "
            f"Volume: ${volume_usd:,.0f}"
        )

        # ── Volume check ──────────────────────────────────────────────────────
        if volume_usd < config.MIN_VOLUME_USD:
            log.warning(
                f"[{coin}] Volume too low: ${volume_usd:,.0f} "
                f"(min ${config.MIN_VOLUME_USD:,.0f}) — skipping"
            )
            return None

        # ── Momentum check ────────────────────────────────────────────────────
        abs_change = abs(price_change_pct)
        if abs_change < config.MIN_MOMENTUM_PCT:
            log.warning(
                f"[{coin}] Momentum too weak: {price_change_pct:+.2f}% "
                f"(min ±{config.MIN_MOMENTUM_PCT}%) — skipping"
            )
            return None

        direction = "long" if price_change_pct > 0 else "short"
        log.info(f"[{coin}] ✅ Momentum signal: {direction.upper()} | {price_change_pct:+.2f}%")

        return MomentumSignal(
            coin             = coin,
            direction        = direction,
            entry_price      = end_price,
            price_change_pct = price_change_pct,
            volume_usd       = volume_usd,
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_mid_price(self, coin: str) -> Optional[float]:
        try:
            mids = self._info.all_mids()
            price = mids.get(coin)
            return float(price) if price else None
        except Exception as e:
            log.error(f"[{coin}] Price fetch error: {e}")
            return None

    def _get_recent_trades(self, coin: str) -> list:
        try:
            return self._info.recent_trades(coin) or []
        except Exception:
            return []
