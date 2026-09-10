import signal
import sys
import threading
from src.config import config
from src.utils.logger import get_logger
from src.utils.telegram import send_alert
from src.listener.listing_listener import ListingListener
from src.detector.momentum_detector import MomentumDetector
from src.executor.trade_executor import TradeExecutor, TradeResult
from src.exit.exit_manager import ExitManager, Position

log = get_logger("main")

# ── Modules ───────────────────────────────────────────────────────────────────
listener = ListingListener()
detector = MomentumDetector()
executor = TradeExecutor()
exit_mgr = ExitManager(executor)

# Dedup — prevent double-processing the same coin
seen_coins: set[str] = set()
seen_lock  = threading.Lock()


# ── New listing handler ───────────────────────────────────────────────────────

def handle_new_listing(coin: str) -> None:
    with seen_lock:
        if coin in seen_coins:
            return
        seen_coins.add(coin)

    # Enforce max concurrent positions
    if exit_mgr.active_count() >= config.MAX_CONCURRENT_TRADES:
        log.warning(
            f"[{coin}] Max concurrent trades reached "
            f"({config.MAX_CONCURRENT_TRADES}) — skipping"
        )
        return

    log.info(f"{'─' * 50}")
    log.info(f"[{coin}] New listing — starting momentum observation")
    send_alert(f"👀 *New listing detected:* `{coin}`\nObserving for {config.OBSERVATION_SECONDS}s...")

    # Run observation in a separate thread so listener keeps polling
    thread = threading.Thread(
        target=_observe_and_trade,
        args=(coin,),
        daemon=True,
        name=f"trade-{coin}",
    )
    thread.start()


def _observe_and_trade(coin: str) -> None:
    try:
        # ── Detect momentum ───────────────────────────────────────────────────
        signal_data = detector.observe(coin)
        if signal_data is None:
            log.info(f"[{coin}] No momentum signal — skipping")
            return

        # ── Execute trade ─────────────────────────────────────────────────────
        result: TradeResult = executor.open_position(
            coin        = signal_data.coin,
            direction   = signal_data.direction,
            entry_price = signal_data.entry_price,
        )

        if not result.success:
            log.error(f"[{coin}] Trade failed: {result.error}")
            return

        # ── Track position ────────────────────────────────────────────────────
        position = Position(
            coin        = result.coin,
            direction   = result.direction,
            entry_price = result.entry_price,
            size        = result.size,
        )
        exit_mgr.add_position(position)

    except Exception as e:
        log.error(f"[{coin}] Unexpected error in trade thread: {e}")


# ── Startup ───────────────────────────────────────────────────────────────────

def main() -> None:
    print("\n╔══════════════════════════════════════════╗")
    print(  "║   Hyperliquid New Listing Momentum Bot   ║")
    print(  "╚══════════════════════════════════════════╝\n")

    if config.PAPER_TRADE:
        log.warning("PAPER TRADE MODE — no real orders will be sent")
    else:
        log.warning("⚠️  LIVE MODE — real USDC will be used")

    balance = executor.get_usdc_balance()
    log.info(f"Account value: ${balance:,.2f} USDC")

    if not config.PAPER_TRADE and balance < config.TRADE_SIZE_USD * 2:
        log.error(
            f"Insufficient balance: ${balance:.2f}. "
            f"Need at least ${config.TRADE_SIZE_USD * 2:.2f}"
        )
        sys.exit(1)

    send_alert(
        f"🤖 *Momentum Bot Started*\n"
        f"Mode: {'PAPER' if config.PAPER_TRADE else 'LIVE'}\n"
        f"Balance: ${balance:,.2f}\n"
        f"Trade size: ${config.TRADE_SIZE_USD} | Leverage: {config.LEVERAGE}x"
    )

    # Start exit manager
    exit_mgr.start()

    # Register listing handler and start listener
    listener.on_new_listing(handle_new_listing)
    listener.start()

    log.info("Bot running — listening for new listings...\n")

    # ── Graceful shutdown ─────────────────────────────────────────────────────
    def shutdown(signum, frame):
        log.warning("Shutting down...")
        listener.stop()
        exit_mgr.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Keep main thread alive
    signal.pause()


if __name__ == "__main__":
    main()
