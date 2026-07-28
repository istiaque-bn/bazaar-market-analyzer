import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


def send_telegram_message(chat_id: str, text: str) -> bool:
    token = settings.TELEGRAM_BOT_TOKEN
    if not token or not chat_id:
        logger.info("Telegram not configured; skipping send")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(url, json={"chat_id": chat_id, "text": text[:4000]}, timeout=20)
        resp.raise_for_status()
        return True
    except Exception as exc:
        logger.warning("Telegram send failed: %s", exc)
        return False
