"""Phase 9 — secret/token redaction in structured logs."""
import logging

from django.test import SimpleTestCase, override_settings

from config.logging_utils import RedactingFormatter, RedactingJsonFormatter, _redact_text


def _make_record(msg: str, *, exc_info=None) -> logging.LogRecord:
    return logging.LogRecord(
        name="test", level=logging.WARNING, pathname=__file__, lineno=1, msg=msg, args=(), exc_info=exc_info
    )


class RedactTextPatternTests(SimpleTestCase):
    def test_password_key_value_is_redacted(self):
        out = _redact_text("connecting with password=hunter2secretvalue now")
        self.assertNotIn("hunter2secretvalue", out)
        self.assertIn("[REDACTED]", out)

    def test_secret_key_is_redacted(self):
        out = _redact_text('SECRET_KEY="abcDEF1234567890superlongsecret"')
        self.assertNotIn("abcDEF1234567890superlongsecret", out)

    def test_bearer_token_is_fully_redacted_not_just_scheme_word(self):
        out = _redact_text("Authorization: Bearer sk-live-abcdef1234567890")
        self.assertNotIn("sk-live-abcdef1234567890", out)

    def test_url_embedded_credentials_are_redacted_but_host_kept(self):
        out = _redact_text("broker at redis://user:supersecretpass@localhost:6379/0")
        self.assertNotIn("supersecretpass", out)
        self.assertNotIn("user:supersecretpass", out)
        # Host/port are operationally useful and not secret — only the
        # credential part should be scrubbed.
        self.assertIn("localhost:6379", out)

    def test_configured_secret_literal_is_redacted_even_without_key_shape(self):
        out = _redact_text("oops printed the raw value hunter2secretvalue123456 somewhere", extra_literals=("hunter2secretvalue123456",))
        self.assertNotIn("hunter2secretvalue123456", out)

    def test_ordinary_text_is_unaffected(self):
        out = _redact_text("DSE fetch completed: 785 stocks, 635076 rows")
        self.assertEqual(out, "DSE fetch completed: 785 stocks, 635076 rows")


class RedactingFormatterTests(SimpleTestCase):
    def test_human_formatter_redacts_message(self):
        formatter = RedactingFormatter(fmt="%(message)s")
        record = _make_record("login with token=abcDEF1234567890")
        rendered = formatter.format(record)
        self.assertNotIn("abcDEF1234567890", rendered)

    def test_human_formatter_redacts_exception_traceback(self):
        formatter = RedactingFormatter(fmt="%(message)s")
        try:
            raise ValueError("failed with api_key=zzzz1234567890secret")
        except ValueError:
            import sys

            record = _make_record("unhandled", exc_info=sys.exc_info())
        rendered = formatter.format(record)
        self.assertNotIn("zzzz1234567890secret", rendered)

    @override_settings(SECRET_KEY="a-very-long-configured-secret-key-value-000")
    def test_json_formatter_redacts_configured_secret_key_literal(self):
        formatter = RedactingJsonFormatter()
        record = _make_record("startup used key a-very-long-configured-secret-key-value-000 apparently")
        rendered = formatter.format(record)
        self.assertNotIn("a-very-long-configured-secret-key-value-000", rendered)

    def test_json_formatter_produces_parseable_json_with_expected_fields(self):
        import json

        formatter = RedactingJsonFormatter()
        record = _make_record("plain message")
        rendered = formatter.format(record)
        payload = json.loads(rendered)
        for field in ("timestamp", "level", "logger", "message", "request_id", "task_id", "task_name"):
            self.assertIn(field, payload)
        self.assertEqual(payload["message"], "plain message")
