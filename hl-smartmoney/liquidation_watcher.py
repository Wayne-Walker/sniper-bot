#!/usr/bin/env python3
"""
Standalone liquidation monitor — runs 24/7, no interactive CLI.
Connects to Hyperliquid WebSocket and sends Telegram alerts
when whales get liquidated above MIN_LIQUIDATION_USD threshold.
"""

import signal
import sys
import time

from src.data.liquidation_monitor import LiquidationMonitor
from src.config import config
from src.utils.logger import get_logger

log = get_logger("liquidation-watcher")


def main():
    log.info("Starting standalone liquidation monitor...")
    log.info(f"  Min alert threshold: ${config.MIN_LIQUIDATION_USD:,.0f}")
    log.info(f"  Telegram: {'configured' if config.TELEGRAM_BOT_TOKEN else 'NOT configured'}")

    monitor = LiquidationMonitor()
    monitor.start()

    def shutdown(signum, frame):
        log.info("Shutting down liquidation monitor...")
        monitor.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Keep the main thread alive
    log.info("Liquidation monitor running. Press Ctrl+C to stop.")
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
