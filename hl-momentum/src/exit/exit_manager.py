import time
import threading
from dataclasses import dataclass, field
from typing import Dict, Optional
from hyperliquid.info import Info
from hyperliquid.utils import constants
from src.config import config
from src.utils.logger import get_logger
from src.utils.telegram import send_alert

log = get_logger(__name__)


@dataclass
class Position:
    coin:          str
    direction:     str        # "long" or "short"
    entry_price:   float
    size:          float
    open_time:     float = field(default_factory=time.time)
    peak_price:    float = 0.0   # tracks highest price seen (for trailing stop)
    tp_hit:        bool  = False  # true once initial take profit was triggered


class ExitManager:
    """
    Polls positions every 5 seconds and applies exit rules:
      1. Take profit  (+TAKE_PROFIT_PCT%)
      2. Stop loss    (-STOP_LOSS_PCT%)
      3. Time stop    (no significant move after TIME_STOP_MINUTES)
      4. Trailing stop (after TP hit, trails by TRAILING_STOP_PCT%)
    """

    POLL_INTERVAL = 5  # seconds

    def __init__(self, executor) -> None:
        self._executor  = executor
        self._info      = Info(constants.MAINNET_API_URL, skip_ws=True)
        self._positions: Dict[str, Position] = {}
        self._lock      = threading.Lock()
        self._running   = False
        self._thread:   Optional[threading.Thread] = None

    def add_position(self, position: Position) -> None:
        with self._lock:
            position.peak_price = position.entry_price
            self._positions[position.coin] = position
            log.info(
                f"[{position.coin}] Tracking {position.direction.upper()} | "
                f"entry=${position.entry_price:.4f} | size={position.size}"
            )

    def active_count(self) -> int:
        with self._lock:
            return len(self._positions)

    def start(self) -> None:
        self._running = True
        self._thread  = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        log.info("Exit manager started")

    def stop(self) -> None:
        self._running = False

    # ── Private ───────────────────────────────────────────────────────────────

    def _monitor_loop(self) -> None:
        while self._running:
            with self._lock:
                coins = list(self._positions.keys())

            for coin in coins:
                try:
                    self._check_position(coin)
                except Exception as e:
                    log.error(f"[{coin}] Exit check error: {e}")

            time.sleep(self.POLL_INTERVAL)

    def _check_position(self, coin: str) -> None:
        with self._lock:
            pos = self._positions.get(coin)
        if not pos:
            return

        current_price = self._get_price(coin)
        if current_price is None:
            return

        # Update peak price for trailing stop
        if pos.direction == "long" and current_price > pos.peak_price:
            pos.peak_price = current_price
        elif pos.direction == "short" and current_price < pos.peak_price:
            pos.peak_price = current_price

        pnl_pct    = self._pnl_pct(pos, current_price)
        age_mins   = (time.time() - pos.open_time) / 60

        log.info(
            f"[{coin}] {pos.direction.upper()} | "
            f"price=${current_price:.4f} | "
            f"PnL={pnl_pct:+.2f}% | "
            f"age={age_mins:.1f}m"
        )

        # ── 1. Take profit ────────────────────────────────────────────────────
        if pnl_pct >= config.TAKE_PROFIT_PCT and not pos.tp_hit:
            log.info(f"[{coin}] 🎯 Take profit hit at {pnl_pct:+.2f}%")
            send_alert(f"🎯 *TAKE PROFIT* `{coin}` | PnL: {pnl_pct:+.2f}%")
            pos.tp_hit = True
            # Don't close yet — let trailing stop manage the rest

        # ── 2. Trailing stop (activates after TP) ─────────────────────────────
        if pos.tp_hit:
            trail_trigger = self._trailing_stop_trigger(pos)
            if (pos.direction == "long"  and current_price <= trail_trigger) or \
               (pos.direction == "short" and current_price >= trail_trigger):
                self._close_and_remove(coin, pos, "TRAILING_STOP")
                return

        # ── 3. Stop loss ──────────────────────────────────────────────────────
        if pnl_pct <= -config.STOP_LOSS_PCT:
            log.warning(f"[{coin}] 🛑 Stop loss hit at {pnl_pct:+.2f}%")
            self._close_and_remove(coin, pos, "STOP_LOSS")
            return

        # ── 4. Time stop ──────────────────────────────────────────────────────
        if age_mins >= config.TIME_STOP_MINUTES and abs(pnl_pct) < 0.5:
            log.warning(f"[{coin}] ⏱ Time stop after {age_mins:.1f}m | PnL={pnl_pct:+.2f}%")
            self._close_and_remove(coin, pos, "TIME_STOP")
            return

    def _close_and_remove(self, coin: str, pos: Position, label: str) -> None:
        success = self._executor.close_position(coin, pos.direction, pos.size, label)
        if success:
            with self._lock:
                self._positions.pop(coin, None)

    def _pnl_pct(self, pos: Position, current_price: float) -> float:
        if pos.direction == "long":
            return ((current_price - pos.entry_price) / pos.entry_price) * 100
        else:
            return ((pos.entry_price - current_price) / pos.entry_price) * 100

    def _trailing_stop_trigger(self, pos: Position) -> float:
        """Price level at which the trailing stop fires."""
        if pos.direction == "long":
            return pos.peak_price * (1 - config.TRAILING_STOP_PCT / 100)
        else:
            return pos.peak_price * (1 + config.TRAILING_STOP_PCT / 100)

    def _get_price(self, coin: str) -> Optional[float]:
        try:
            mids  = self._info.all_mids()
            price = mids.get(coin)
            return float(price) if price else None
        except Exception:
            return None
