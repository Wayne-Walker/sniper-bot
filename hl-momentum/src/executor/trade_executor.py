from typing import Optional
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from src.config import config
from src.utils.logger import get_logger
from src.utils.telegram import send_alert

log = get_logger(__name__)


class TradeResult:
    def __init__(
        self,
        success:    bool,
        coin:       str,
        direction:  str,
        size:       float = 0.0,
        entry_price: float = 0.0,
        order_id:   Optional[int] = None,
        error:      Optional[str] = None,
    ):
        self.success     = success
        self.coin        = coin
        self.direction   = direction
        self.size        = size
        self.entry_price = entry_price
        self.order_id    = order_id
        self.error       = error


class TradeExecutor:
    def __init__(self) -> None:
        self._account  = Account.from_key(config.PRIVATE_KEY)
        self._info     = Info(constants.MAINNET_API_URL, skip_ws=True)

        # Exchange client — handles signing automatically
        self._exchange = Exchange(
            self._account,
            constants.MAINNET_API_URL,
            account_address=config.WALLET_ADDRESS or self._account.address,
        )

        log.info(f"Executor wallet: {self._account.address[:8]}...")

    def open_position(self, coin: str, direction: str, entry_price: float) -> TradeResult:
        is_buy = direction == "long"
        size   = round(config.TRADE_SIZE_USD / entry_price, 6)

        # ── Paper trade ───────────────────────────────────────────────────────
        if config.PAPER_TRADE:
            log.warning(
                f"[PAPER] Would open {direction.upper()} {coin} | "
                f"size={size} @ ${entry_price:.4f} | "
                f"notional=${config.TRADE_SIZE_USD}"
            )
            return TradeResult(
                success=True, coin=coin, direction=direction,
                size=size, entry_price=entry_price, order_id=0
            )

        try:
            # Set leverage first
            self._exchange.update_leverage(config.LEVERAGE, coin, is_cross=True)

            # Place market order
            order_result = self._exchange.market_open(
                coin      = coin,
                is_buy    = is_buy,
                sz        = size,
                slippage  = 0.03,   # 3% slippage tolerance for new listings
            )

            status = order_result.get("status")
            if status != "ok":
                error = str(order_result)
                log.error(f"[{coin}] Order failed: {error}")
                return TradeResult(success=False, coin=coin, direction=direction, error=error)

            filled    = order_result["response"]["data"]["statuses"][0].get("filled", {})
            avg_px    = float(filled.get("avgPx", entry_price))
            total_sz  = float(filled.get("totalSz", size))
            order_id  = filled.get("oid", 0)

            log.info(
                f"[{coin}] ✅ {direction.upper()} opened | "
                f"size={total_sz} @ ${avg_px:.4f} | oid={order_id}"
            )
            send_alert(
                f"🟢 *{direction.upper()} OPENED*\n"
                f"Coin: `{coin}`\n"
                f"Size: {total_sz} @ ${avg_px:.4f}\n"
                f"Notional: ~${config.TRADE_SIZE_USD}"
            )

            return TradeResult(
                success=True, coin=coin, direction=direction,
                size=total_sz, entry_price=avg_px, order_id=order_id
            )

        except Exception as e:
            log.error(f"[{coin}] Open position error: {e}")
            return TradeResult(success=False, coin=coin, direction=direction, error=str(e))

    def close_position(self, coin: str, direction: str, size: float, label: str = "CLOSE") -> bool:
        is_buy = direction == "short"   # close long = sell, close short = buy

        if config.PAPER_TRADE:
            log.warning(f"[PAPER] Would CLOSE {coin} | size={size} | reason={label}")
            return True

        try:
            result = self._exchange.market_close(
                coin     = coin,
                is_buy   = is_buy,
                sz       = size,
                slippage = 0.03,
            )

            status = result.get("status")
            if status != "ok":
                log.error(f"[{coin}] Close failed: {result}")
                return False

            filled   = result["response"]["data"]["statuses"][0].get("filled", {})
            avg_px   = float(filled.get("avgPx", 0))

            log.info(f"[{coin}] ✅ {label} | closed @ ${avg_px:.4f}")
            send_alert(
                f"🔴 *{label}*\n"
                f"Coin: `{coin}`\n"
                f"Closed @ ${avg_px:.4f}"
            )
            return True

        except Exception as e:
            log.error(f"[{coin}] Close position error: {e}")
            return False

    def get_usdc_balance(self) -> float:
        try:
            state = self._info.user_state(self._account.address)
            return float(state.get("marginSummary", {}).get("accountValue", 0))
        except Exception:
            return 0.0
