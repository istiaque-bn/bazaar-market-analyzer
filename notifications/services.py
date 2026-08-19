import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
_CONNECT_TIMEOUT = 5
_READ_TIMEOUT = 20


class TelegramError(Exception):
    """Base class — message text is always pre-redacted of the bot token
    before it reaches a log line, an exception, or a stored delivery
    record; see _redact below."""


class TelegramTransientError(TelegramError):
    """Network error, timeout, HTTP 429 (rate limit), or 5xx — safe to
    retry with backoff."""


class TelegramPermanentError(TelegramError):
    """Bad token, unknown/blocked chat, malformed request (4xx other than
    429), or the feature isn't configured at all — retrying won't help."""


def _redact(text: str, token: str) -> str:
    return text.replace(token, "[REDACTED]") if token else text


VPS_REPORT_PREFIX = "VPS Report"


def _is_production() -> bool:
    """True only for the actual VPS deployment (config.settings.production),
    never local dev/test — SETTINGS_MODULE is a Django-managed attribute
    (django.conf.LazySettings._setup), not something this project sets
    itself, so it can't drift out of sync with which settings module is
    genuinely active."""
    return getattr(settings, "SETTINGS_MODULE", "") == "config.settings.production"


def _post_telegram_message(token: str, chat_id: str, text: str) -> dict:
    """Single low-level Telegram Bot API sendMessage call — every
    Telegram send in this project goes through this one function, so
    there is exactly one place that owns HTTPS, timeouts, and token
    redaction. Raises TelegramTransientError/TelegramPermanentError on
    failure; callers decide whether/how to retry.

    Messages sent from the production VPS are prefixed with a bold "VPS
    Report" line — local dev/test also has real Telegram credentials
    configured (see project memory on the Aug 2026 celery-duplication
    incident), so without this, alerts from a developer's laptop are
    indistinguishable from the real production ones in the same chat.
    Bold is applied via the `entities` API (a byte-range formatting
    instruction on the raw text) rather than parse_mode + markdown/HTML
    syntax, so arbitrary report text containing *, _, <, or & can't
    break Telegram's entity parsing and silently drop the message."""
    request_payload: dict = {}
    if _is_production():
        prefix = f"{VPS_REPORT_PREFIX}\n"
        text = f"{prefix}{text}"[:4096]
        request_payload["entities"] = [{"type": "bold", "offset": 0, "length": len(VPS_REPORT_PREFIX)}]
    else:
        text = text[:4096]
    request_payload["chat_id"] = chat_id
    request_payload["text"] = text
    url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
    try:
        resp = requests.post(
            url,
            json=request_payload,
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
        )
    except requests.exceptions.RequestException as exc:
        # str(exc) on a connection error can embed the full request URL
        # (token included) — always redact before it's logged or stored.
        raise TelegramTransientError(_redact(str(exc), token)) from None

    if resp.status_code == 429 or resp.status_code >= 500:
        raise TelegramTransientError(_redact(f"HTTP {resp.status_code}: {resp.text[:300]}", token))
    if resp.status_code >= 400:
        raise TelegramPermanentError(_redact(f"HTTP {resp.status_code}: {resp.text[:300]}", token))

    try:
        payload = resp.json()
    except ValueError:
        raise TelegramTransientError("Telegram returned a non-JSON response") from None

    if not payload.get("ok"):
        raise TelegramPermanentError(_redact(f"Telegram API error: {payload.get('description', 'unknown error')}", token))

    return payload.get("result") or {}


def send_telegram_message(chat_id: str, text: str, *, token: str | None = None) -> bool:
    """Best-effort send for the existing lightweight alert call sites
    (daily digest, feedback notifications) — never raises; logs
    (token-redacted) and returns False on any failure. Defaults to the
    main bot (TELEGRAM_BOT_TOKEN); pass `token=` to send through a
    different bot instead (e.g. the dedicated security bot used for temp
    password delivery — see accounts.services) — see
    send_telegram_message_tracked for callers that need to know exactly
    what happened."""
    token = token if token is not None else settings.TELEGRAM_BOT_TOKEN
    if not token or not chat_id:
        logger.info("Telegram not configured; skipping send")
        return False
    try:
        _post_telegram_message(token, chat_id, text)
        return True
    except TelegramError as exc:
        logger.warning("Telegram send failed: %s", exc)
        return False


def send_telegram_message_tracked(chat_id: str, text: str) -> dict:
    """Stricter variant for callers that need delivery confirmation (a
    message id) and a typed transient/permanent distinction so their own
    retry/idempotency policy can act on it — e.g. the ML daily report.
    Raises TelegramPermanentError immediately if not configured, or
    TelegramTransientError/TelegramPermanentError from the send itself."""
    token = settings.TELEGRAM_BOT_TOKEN
    if not token or not chat_id:
        raise TelegramPermanentError("Telegram is not configured (missing bot token or chat id).")
    result = _post_telegram_message(token, chat_id, text)
    return {"message_id": result.get("message_id")}
