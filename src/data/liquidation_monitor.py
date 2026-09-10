"""
Liquidation Monitor
───────────────────
Connects to Hyperliquid's WebSocket and subscribes to the
liquidation feed. When a liquidation exceeds the configured
minimum notional threshold, fires a Telegram alert.

Runs in a background thread alongside the main bot.
"""

import json
import threading
import time
from typing import Optional
import websocket
from src.config import config
from src.utils.logger import get_logger
from src.utils.telegram import send_alert

log = get_logger(__name__)

HL_WS_URL = "wss://api.hyperliquid.xyz/ws"


class LiquidationMonitor:
    """
    Subscribes to Hyperliquid's 'activeTrades' WebSocket channel
    and filters for liquidation events above MIN_LIQUIDATION_USD.
    Sends a Telegram alert for each qualifying liquidation.
    """

    def __init__(self) -> None:
        self._ws:      Optional[websocket.WebSocketApp] = None
        self._thread:  Optional[threading.Thread]       = None
        self._running: bool = False
        self._reconnect_delay: int = 3

    def start(self) -> None:
        if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
            log.warning(
                "Liquidation monitor: Telegram not configured — "
                "alerts will only appear in logs. "
                "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env to enable."
            )
        self._running = True
        self._thread  = threading.Thread(target=self._run_loop, daemon=True, name="liq-monitor")
        self._thread.start()
        log.info(f"Liquidation monitor started (min alert: ${config.MIN_LIQUIDATION_USD:,.0f})")

    def stop(self) -> None:
        self._running = False
        if self._ws:
            self._ws.close()

    # ── Private ───────────────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        while self._running:
            try:
                self._connect()
            except Exception as e:
                log.error(f"Liquidation monitor error: {e}")
            if self._running:
                log.warning(f"Liquidation monitor reconnecting in {self._reconnect_delay}s...")
                time.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 60)

    def _connect(self) -> None:
        self._ws = websocket.WebSocketApp(
            HL_WS_URL,
            on_open    = self._on_open,
            on_message = self._on_message,
            on_error   = self._on_error,
            on_close   = self._on_close,
        )
        self._ws.run_forever(ping_interval=30, ping_timeout=10)

    def _on_open(self, ws) -> None:
        log.info("Liquidation monitor WebSocket connected")
        self._reconnect_delay = 3  # reset backoff on successful connect

        # Subscribe to all trades — we filter for liquidations in on_message
        subscription = {
            "method": "subscribe",
            "subscription": {"type": "trades", "coin": ""},
        }
        # Hyperliquid doesn't have a dedicated liquidation channel in the
        # public WS — instead we subscribe to the userEvents-style feed
        # via the public liquidations endpoint available through allMids
        # workaround: subscribe to "notification" type which includes liquidations
        ws.send(json.dumps({
            "method": "subscribe",
            "subscription": {"type": "notification", "user": ""}
        }))

        # Also subscribe to the liquidation-specific stream
        ws.send(json.dumps({
            "method": "subscribe",
            "subscription": {"type": "activeTrades"}
        }))

        log.info("Subscribed to liquidation feed")

    def _on_message(self, ws, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return

        channel = msg.get("channel", "")
        data    = msg.get("data", {})

        # ── Handle activeTrades liquidation events ────────────────────────────
        if channel == "activeTrades":
            trades = data if isinstance(data, list) else [data]
            for trade in trades:
                self._process_trade(trade)

        # ── Handle notification events (user-level liquidations) ──────────────
        elif channel == "notification":
            notification = data.get("notification", "")
            if "liquidated" in notification.lower():
                self._process_notification(notification)

    def _process_trade(self, trade: dict) -> None:
        """Process a trade event — filter for liquidations."""
        # Liquidation trades have a 'liquidation' field in Hyperliquid
        if not trade.get("liquidation"):
            return

        coin      = trade.get("coin", "UNKNOWN")
        side      = "LONG" if trade.get("side") == "B" else "SHORT"
        px        = float(trade.get("px", 0))
        sz        = float(trade.get("sz", 0))
        notional  = px * sz
        liq_data  = trade.get("liquidation", {})
        liq_addr  = liq_data.get("liquidatedUser", "unknown")
        mark_px   = float(liq_data.get("markPx", px))

        if notional < config.MIN_LIQUIDATION_USD:
            return

        self._fire_alert(
            coin     = coin,
            side     = side,
            notional = notional,
            price    = px,
            mark_px  = mark_px,
            address  = liq_addr,
            source   = "activeTrades",
        )

    def _process_notification(self, notification: str) -> None:
        """Process a plain-text notification containing liquidation info."""
        log.warning(f"Liquidation notification: {notification}")
        send_alert(f"🚨 *Liquidation Alert*\n{notification}")

    def _fire_alert(
        self,
        coin:     str,
        side:     str,
        notional: float,
        price:    float,
        mark_px:  float,
        address:  str,
        source:   str,
    ) -> None:
        emoji   = "🔴" if side == "LONG" else "🟢"
        msg = (
            f"{emoji} *WHALE LIQUIDATED*\n"
            f"Coin     : `${coin}`\n"
            f"Side     : `{side}`\n"
            f"Notional : `${notional:,.0f}`\n"
            f"Liq Price: `${price:,.4f}`\n"
            f"Mark Price: `${mark_px:,.4f}`\n"
            f"Wallet   : `{address[:12]}...`"
        )

        log.warning(
            f"💥 LIQUIDATION | ${coin} {side} | "
            f"${notional:,.0f} | price: ${price:,.4f} | "
            f"wallet: {address[:10]}..."
        )
        send_alert(msg)

    def _on_error(self, ws, error) -> None:
        log.error(f"Liquidation WS error: {error}")

    def _on_close(self, ws, code, msg) -> None:
        log.warning(f"Liquidation WS closed: {code} {msg}")
