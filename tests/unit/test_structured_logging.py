from __future__ import annotations

import json
import logging
import unittest

from tgcurator.infrastructure.observability import JsonFormatter, redact_for_log


class StructuredLoggingTests(unittest.TestCase):
    def test_sensitive_fields_are_redacted_recursively(self) -> None:
        value = redact_for_log(
            {
                "api_token": "do-not-log",
                "nested": {"password_hash": "also-do-not-log", "safe": "visible"},
                "items": [{"nonce": "hidden"}, {"id": "visible"}],
            }
        )

        self.assertEqual(value["api_token"], "[REDACTED]")
        self.assertEqual(value["nested"]["password_hash"], "[REDACTED]")
        self.assertEqual(value["nested"]["safe"], "visible")
        self.assertEqual(value["items"][0]["nonce"], "[REDACTED]")

    def test_json_formatter_omits_traceback_and_redacts_context(self) -> None:
        formatter = JsonFormatter()
        try:
            raise RuntimeError("not for logs")
        except RuntimeError:
            record = logging.makeLogRecord(
                {
                    "name": "tgcurator.test",
                    "levelno": logging.ERROR,
                    "levelname": "ERROR",
                    "msg": "secret operation failed",
                    "event": "secret.store_failed",
                    "context": {"secret_value": "must-not-appear", "secret_id": "safe-id"},
                    "exc_info": __import__("sys").exc_info(),
                }
            )

        payload = json.loads(formatter.format(record))
        self.assertEqual(payload["event"], "secret.store_failed")
        self.assertEqual(payload["context"]["secret_value"], "[REDACTED]")
        self.assertEqual(payload["context"]["secret_id"], "[REDACTED]")
        self.assertEqual(payload["exception"], "exception details withheld from structured logs")
        self.assertNotIn("RuntimeError", payload)


if __name__ == "__main__":
    unittest.main()
