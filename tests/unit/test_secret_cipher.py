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
    def test_encrypts_with_a_fresh_nonce_and_round_trips(self) -> None:
        cipher = AesGcmSecretCipher(master_key=MASTER_KEY, key_id="test-key")

        first = cipher.encrypt(b"telegram-api-secret")
        second = cipher.encrypt(b"telegram-api-secret")

        self.assertEqual(cipher.decrypt(first), b"telegram-api-secret")
        self.assertNotEqual(first.nonce, second.nonce)
        self.assertNotEqual(first.ciphertext, second.ciphertext)
        self.assertEqual(first.key_id, "test-key")

    def test_rejects_wrong_key_id_and_tampered_ciphertext(self) -> None:
        cipher = AesGcmSecretCipher(master_key=MASTER_KEY, key_id="test-key")
        payload = cipher.encrypt(b"provider-key")

        with self.assertRaises(SecretDecryptionError):
            cipher.decrypt(replace(payload, key_id="old-key"))
        with self.assertRaises(SecretDecryptionError):
            cipher.decrypt(replace(payload, ciphertext=payload.ciphertext[:-1] + b"x"))

    def test_requires_a_base64_encoded_aes_256_key(self) -> None:
        with self.assertRaises(InvalidMasterKeyError):
            AesGcmSecretCipher(master_key="not base64")
        with self.assertRaises(InvalidMasterKeyError):
            AesGcmSecretCipher(master_key=base64.b64encode(b"too short").decode("ascii"))


if __name__ == "__main__":
    unittest.main()
