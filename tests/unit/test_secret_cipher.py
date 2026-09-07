from __future__ import annotations

import base64
import unittest
from dataclasses import replace

from tgcurator.infrastructure.security import (
    AesGcmSecretCipher,
    InvalidMasterKeyError,
    SecretDecryptionError,
)

MASTER_KEY = base64.b64encode(b"k" * 32).decode("ascii")


class AesGcmSecretCipherTests(unittest.TestCase):
    def test_encrypts_with_a_fresh_nonce_and_round_trips_with_aad(self) -> None:
        cipher = AesGcmSecretCipher(master_key=MASTER_KEY, key_id="test-key")

        first = cipher.encrypt(b"telegram-api-secret", associated_data=b"telegram_session")
        second = cipher.encrypt(b"telegram-api-secret", associated_data=b"telegram_session")

        self.assertEqual(
            cipher.decrypt(first, associated_data=b"telegram_session"),
            b"telegram-api-secret",
        )
        self.assertEqual(len(first.nonce), 12)
        self.assertNotEqual(first.nonce, second.nonce)
        self.assertNotEqual(first.ciphertext, second.ciphertext)
        self.assertEqual(first.key_id, "test-key")

    def test_rejects_wrong_key_aad_and_tampered_ciphertext(self) -> None:
        cipher = AesGcmSecretCipher(master_key=MASTER_KEY, key_id="test-key")
        payload = cipher.encrypt(b"provider-key", associated_data=b"inference_api_key")

        with self.assertRaises(SecretDecryptionError):
            cipher.decrypt(
                replace(payload, key_id="old-key"),
                associated_data=b"inference_api_key",
            )
        with self.assertRaises(SecretDecryptionError):
            cipher.decrypt(payload, associated_data=b"bot_token")
        with self.assertRaises(SecretDecryptionError):
            cipher.decrypt(
                replace(payload, ciphertext=payload.ciphertext[:-1] + b"x"),
                associated_data=b"inference_api_key",
            )

    def test_validates_plaintext_associated_data_and_master_key(self) -> None:
        cipher = AesGcmSecretCipher(master_key=MASTER_KEY)
        with self.assertRaises(ValueError):
            cipher.encrypt(b"")
        with self.assertRaises(TypeError):
            cipher.encrypt(b"secret", associated_data="wrong-type")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            AesGcmSecretCipher(master_key=MASTER_KEY, key_id="x" * 129)
        with self.assertRaises(InvalidMasterKeyError):
            AesGcmSecretCipher(master_key="not base64")
        with self.assertRaises(InvalidMasterKeyError):
            AesGcmSecretCipher(master_key=base64.b64encode(b"too short").decode("ascii"))


if __name__ == "__main__":
    unittest.main()
