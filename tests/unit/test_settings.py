from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from tgcurator.application.settings import Settings

MASTER_KEY = base64.b64encode(bytes(range(32))).decode("ascii")


class SettingsTests(unittest.TestCase):
    def test_loads_database_and_master_key_from_supported_aliases(self) -> None:
        environment = {
            "DATABASE_URL": "postgresql+asyncpg://user:pass@localhost/curator",
            "APP_MASTER_KEY": MASTER_KEY,
            "TGCURATOR_LOG_LEVEL": "warning",
        }
        with patch.dict("os.environ", environment, clear=True):
            settings = Settings(_env_file=None)

        self.assertEqual(settings.database_url, environment["DATABASE_URL"])
        self.assertEqual(settings.app_master_key.get_secret_value(), MASTER_KEY)
        self.assertEqual(settings.log_level, "WARNING")

    def test_loads_and_trims_master_key_from_read_only_style_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            secret_file = Path(directory, "master-key")
            secret_file.write_text(f"  {MASTER_KEY}\n", encoding="utf-8")
            settings = Settings(
                environment="production",
                app_master_key_file=secret_file,
                _env_file=None,
            )

        self.assertEqual(settings.app_master_key.get_secret_value(), MASTER_KEY)

    def test_rejects_ambiguous_or_missing_production_master_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            secret_file = Path(directory, "master-key")
            secret_file.write_text(MASTER_KEY, encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "not both"):
                Settings(
                    app_master_key=MASTER_KEY,
                    app_master_key_file=secret_file,
                    _env_file=None,
                )

        with self.assertRaisesRegex(ValidationError, "required in production"):
            Settings(environment="production", _env_file=None)

    def test_blank_database_url_is_treated_as_unconfigured(self) -> None:
        settings = Settings(database_url="   ", _env_file=None)
        self.assertIsNone(settings.database_url)


if __name__ == "__main__":
    unittest.main()
