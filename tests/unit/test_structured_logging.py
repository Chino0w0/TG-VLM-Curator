from __future__ import annotations

import io
import json
import logging
import unittest

from pydantic import SecretStr

from tgcurator.infrastructure.observability import (
    JsonFormatter,
    configure_structured_logging,
    redact_for_log,
    redact_text,
)


class StructuredLoggingTests(unittest.TestCase):
    def test_sensitive_fields_are_redacted_recursively(self) -> None:
        recursive: dict[str, object] = {}
        recursive["self"] = recursive
        value = redact_for_log(
            {
                "api_token": "do-not-log",
                "nested": {"password_hash": "also-do-not-log", "safe": "visible"},
                "items": [{"nonce": b"hidden"}, {"id": "visible"}],
                "wrapped": SecretStr("hidden"),
                "recursive": recursive,
                "secret_id": "safe-reference",
            }
        )

        self.assertEqual(value["api_token"], "[REDACTED]")
        self.assertEqual(value["nested"]["password_hash"], "[REDACTED]")
        self.assertEqual(value["nested"]["safe"], "visible")
        self.assertEqual(value["items"][0]["nonce"], "[REDACTED]")
        self.assertEqual(value["wrapped"], "[REDACTED]")
        self.assertEqual(value["recursive"]["self"], "[RECURSIVE]")
        self.assertEqual(value["secret_id"], "safe-reference")

    def test_text_redaction_covers_bearer_and_key_value_credentials(self) -> None:
        message = "Authorization: Bearer abc.def password=hunter2 cookie=session-value"
        redacted = redact_text(message)

        self.assertNotIn("abc.def", redacted)
        self.assertNotIn("hunter2", redacted)
        self.assertNotIn("session-value", redacted)
        self.assertGreaterEqual(redacted.count("[REDACTED]"), 3)

    def test_json_formatter_keeps_correlation_and_exception_type_only(self) -> None:
        formatter = JsonFormatter(service="api")
        try:
            raise RuntimeError("database password=must-not-appear")
        except RuntimeError:
            record = logging.makeLogRecord(
                {
                    "name": "tgcurator.test",
                    "levelno": logging.ERROR,
                    "levelname": "ERROR",
                    "msg": "request failed token=must-not-appear",
                    "event": "request.failed",
                    "message_id": "message-1",
                    "context": {
                        "secret_value": "must-not-appear",
                        "secret_id": "safe-id",
                    },
                    "exc_info": __import__("sys").exc_info(),
                }
            )

        rendered = formatter.format(record)
        payload = json.loads(rendered)
        self.assertEqual(payload["service"], "api")
        self.assertEqual(payload["event"], "request.failed")
        self.assertEqual(payload["message_id"], "message-1")
        self.assertEqual(payload["context"]["secret_value"], "[REDACTED]")
        self.assertEqual(payload["context"]["secret_id"], "safe-id")
        self.assertEqual(payload["exception_type"], "RuntimeError")
        self.assertNotIn("must-not-appear", rendered)
        self.assertNotIn("traceback", rendered.lower())

    def test_configure_structured_logging_replaces_handlers_with_json(self) -> None:
        root_logger = logging.getLogger()
        original_handlers = list(root_logger.handlers)
        original_level = root_logger.level
        stream = io.StringIO()
        try:
            configure_structured_logging("warning", service="worker")
            handler = root_logger.handlers[0]
            handler.setStream(stream)
            root_logger.warning("operation failed api_key=hidden", extra={"event": "test"})
            payload = json.loads(stream.getvalue())
            self.assertEqual(payload["service"], "worker")
            self.assertEqual(payload["level"], "WARNING")
            self.assertNotIn("hidden", stream.getvalue())
        finally:
            for handler in list(root_logger.handlers):
                root_logger.removeHandler(handler)
                handler.close()
            for handler in original_handlers:
                root_logger.addHandler(handler)
            root_logger.setLevel(original_level)


if __name__ == "__main__":
    unittest.main()
