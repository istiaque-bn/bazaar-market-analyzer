"""
Phase 9: correlation IDs + secret redaction for every log line.

Two independent concerns live here:

1. Correlation: a web request gets a request_id (from an inbound
   X-Request-ID header, or a fresh one) via market.middleware.
   RequestIDMiddleware; a Celery task gets a task_id/task_name via the
   signal handlers wired in config/celery.py. Both are stashed in
   ContextVars so any log call anywhere in the request/task's call
   stack can be tagged with them via CorrelationFilter, without having
   to thread an id through every function signature.

2. Redaction: nothing this app logs should ever contain a real secret
   value, even by accident (an exception message that happens to
   include a request URL with embedded credentials, a stray
   `str(settings.X)`, a copy-pasted `Authorization: Bearer ...` header
   in a debug log, etc). RedactingFormatter re-renders every record
   through a small set of pattern- and value-based substitutions as
   the very last step before a line leaves the process — after
   exception tracebacks are rendered, not before — so it can't be
   bypassed by logging exc_info or extra fields the filter never saw.
"""
from __future__ import annotations

import contextvars
import json
import logging
import re

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
task_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("task_id", default="-")
task_name_var: contextvars.ContextVar[str] = contextvars.ContextVar("task_name", default="-")

REDACTED = "[REDACTED]"

# Pattern-based redaction: catches secret-shaped text regardless of
# whether it happens to match a currently-configured value (e.g. a
# token embedded in a URL from a third-party error message). Order
# matters: the Authorization/Bearer rule must run before the generic
# key=value rule, or "Authorization: Bearer <token>" gets only its
# scheme word ("Bearer") redacted by the generic rule (which stops at
# the first whitespace) while the actual token after it survives.
_PATTERN_REDACTIONS = [
    # HTTP Authorization header values: "Bearer <x>", "Token <x>", "Basic <x>"
    (re.compile(r"(?i)\b(Bearer|Token|Basic)\s+[A-Za-z0-9\-._~+/]+=*"), r"\1 " + REDACTED),
    # Credentials embedded in a connection URL: scheme://user:pass@host
    (re.compile(r"(?i)(\b[a-z][a-z0-9+.\-]*://)[^/\s:@]+:[^/\s@]+@"), r"\1" + REDACTED + "@"),
    # key=value / key: "value" / key="value" for common secret-ish keys
    # (deliberately excludes "authorization" — handled above, since its
    # value is "<scheme> <credential>", not a bare key=value pair).
    (
        re.compile(
            r'(?i)\b(secret[_-]?key|password|passwd|token|api[_-]?key|access[_-]?key)'
            r'(["\']?\s*[:=]\s*["\']?)([^\s"\'&,;]{3,})'
        ),
        r"\1\2" + REDACTED,
    ),
]


def _redact_text(text: str, extra_literals: tuple[str, ...] = ()) -> str:
    if not text:
        return text
    for literal in extra_literals:
        if literal and len(literal) >= 6:
            text = text.replace(literal, REDACTED)
    for pattern, replacement in _PATTERN_REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def _configured_secrets() -> tuple[str, ...]:
    """Literal, currently-configured secret values — belt-and-suspenders
    on top of the pattern rules above, in case one is logged verbatim
    with no recognizable key= / header shape around it. Imported lazily
    (settings may not be ready yet when the logging config is built) and
    swallows any error so a broken settings read never breaks logging.
    """
    try:
        from django.conf import settings

        candidates = [
            getattr(settings, "SECRET_KEY", ""),
            getattr(settings, "TELEGRAM_BOT_TOKEN", ""),
            getattr(settings, "EMAIL_HOST_PASSWORD", ""),
            (settings.DATABASES.get("default", {}) or {}).get("PASSWORD", ""),
        ]
        return tuple(c for c in candidates if c)
    except Exception:
        return ()


class CorrelationFilter(logging.Filter):
    """Attaches request_id/task_id/task_name to every record so the
    formatter can include them, whether or not the current context set
    one (default "-")."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.task_id = task_id_var.get()
        record.task_name = task_name_var.get()
        return True


class RedactingFormatterMixin:
    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)  # type: ignore[misc]
        return _redact_text(rendered, _configured_secrets())


class RedactingFormatter(RedactingFormatterMixin, logging.Formatter):
    """Human-readable console formatter with redaction — used in
    development/test so local terminal output stays readable."""


class RedactingJsonFormatter(logging.Formatter):
    """One JSON object per line — used in production so a log
    aggregator can parse fields instead of scraping free text. Builds
    its own payload (doesn't reuse RedactingFormatterMixin.format)."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
            "task_id": getattr(record, "task_id", "-"),
            "task_name": getattr(record, "task_name", "-"),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        rendered = json.dumps(payload, default=str)
        return _redact_text(rendered, _configured_secrets())
