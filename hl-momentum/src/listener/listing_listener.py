import time
import threading
from typing import Callable, Set
from requests.exceptions import ConnectionError as ReqConnectionError
from hyperliquid.info import Info
from hyperliquid.utils import constants
from src.utils.logger import get_logger

log = get_logger(__name__)

# Callback type: receives the new coin name e.g. "NEWTOKEN"
NewListingCallback = Callable[[str], None]


class ListingListener:
    """
    Polls Hyperliquid's /info meta endpoint every POLL_INTERVAL seconds.
    When a new perp asset appears in the universe, fires all registered callbacks.
    """

    POLL_INTERVAL      = 15  # seconds — new listings are rare, no need to hammer API
    MAX_BACKOFF        = 120  # max seconds to wait after repeated errors
    HEARTBEAT_INTERVAL = 600  # log a liveness line every N seconds of quiet polling

    def __init__(self) -> None:
        self._info      = self._build_info()
        self._known:    Set[str] = set()
        self._callbacks: list[NewListingCallback] = []
        self._running   = False
        self._thread:   threading.Thread | None = None
        self._last_heartbeat = 0.0

    def on_new_listing(self, callback: NewListingCallback) -> None:
        self._callbacks.append(callback)

    def start(self) -> None:
        # Seed known coins so we don't fire on existing listings at startup
        self._known = self._fetch_current_coins()
        log.info(f"Listing listener started — tracking {len(self._known)} existing perps")

        self._running = True
        self._thread  = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    # ── Private ───────────────────────────────────────────────────────────────

    def _build_info(self) -> Info:
        """Build an Info client that disables HTTP keep-alive.

        Why: we poll every 15s, well past HL's idle-keepalive window, so
        reuse buys nothing. Keep-alive sockets that HL closes from their end
        sit in CLOSE_WAIT until the next request reaps them, leaking fds.
        """
        info = Info(constants.MAINNET_API_URL, skip_ws=True)
        info.session.headers.update({"Connection": "close"})
        return info

    def _recreate_session(self) -> None:
        """Create a fresh Info client to clear stale connection pool."""
        try:
            self._info.session.close()
        except Exception:
            pass
        self._info = self._build_info()
        log.info("Recreated HTTP session")

    def _poll_loop(self) -> None:
        consecutive_errors = 0

        while self._running:
            try:
                current = self._fetch_current_coins()
                new     = current - self._known
                consecutive_errors = 0  # reset on success

                for coin in new:
                    log.info(f"🆕 New listing detected: {coin}")
                    self._known.add(coin)
                    for cb in self._callbacks:
                        try:
                            cb(coin)
                        except Exception as e:
                            log.error(f"Callback error for {coin}: {e}")

                now = time.monotonic()
                if now - self._last_heartbeat >= self.HEARTBEAT_INTERVAL:
                    log.info(f"Heartbeat — polling, tracking {len(self._known)} perps")
                    self._last_heartbeat = now

            except (ReqConnectionError, ConnectionError, OSError) as e:
                consecutive_errors += 1
                log.warning(f"Connection error (attempt {consecutive_errors}): {e}")
                self._recreate_session()
                self._backoff_sleep(consecutive_errors)
                continue

            except Exception as e:
                consecutive_errors += 1
                err_str = str(e)
                if "429" in err_str:
                    log.warning(f"Rate limited (attempt {consecutive_errors}), backing off")
                elif "502" in err_str or "504" in err_str:
                    log.warning(f"Server error (attempt {consecutive_errors}): {e}")
                else:
                    log.error(f"Listing listener poll error: {e}")
                self._backoff_sleep(consecutive_errors)
                continue

            time.sleep(self.POLL_INTERVAL)

    def _backoff_sleep(self, consecutive_errors: int) -> None:
        """Exponential backoff: 15, 30, 60, 120, 120, ..."""
        delay = min(self.POLL_INTERVAL * (2 ** (consecutive_errors - 1)), self.MAX_BACKOFF)
        log.info(f"Waiting {delay}s before next poll")
        time.sleep(delay)

    def _fetch_current_coins(self) -> Set[str]:
        meta = self._info.meta()
        return {asset["name"] for asset in meta["universe"]}
