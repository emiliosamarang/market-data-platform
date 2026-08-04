import logging

import requests

from config import TELEGRAM_CHAT_ID, TELEGRAM_TOKEN

log = logging.getLogger(__name__)


def send(text: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10,
        )
    except Exception:
        log.warning("Telegram notification failed: %s", text[:80])
