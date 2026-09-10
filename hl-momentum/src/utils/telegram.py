import requests
from src.config import config
from src.utils.logger import get_logger

log = get_logger(__name__)

def send_alert(message: str) -> None:
    """Send a Telegram message. Silently skips if token/chat not configured."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id":    config.TELEGRAM_CHAT_ID,
            "text":       message,
            "parse_mode": "Markdown",
        }, timeout=5)
    except Exception:
        log.warning("Telegram alert failed")
