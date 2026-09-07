from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr


class InvalidMasterKeyError(ValueError):
    """Raised when APP_MASTER_KEY is not a base64-encoded 256-bit key."""


class SecretDecryptionError(RuntimeError):
    """Raised when encrypted secret material cannot be authenticated and decrypted."""


@dataclass(frozen=True, slots=True)
class EncryptedPayload:
    ciphertext: bytes
    nonce: bytes
    key_id: str


class AesGcmSecretCipher:
    """AES-256-GCM encryption boundary for persisted secret payloads."""

    NONCE_SIZE = 12
    KEY_SIZE = 32

    def __init__(self, *, master_key: SecretStr | str | bytes, key_id: str = "primary") -> None:
        if not key_id.strip():
            raise ValueError("key_id must not be blank")
        self._key = self._decode_master_key(master_key)
        self._key_id = key_id
        self._aes_gcm = AESGCM(self._key)

    @property
    def key_id(self) -> str:
        return self._key_id

    def encrypt(self, plaintext: bytes) -> EncryptedPayload:
        if not plaintext:
            raise ValueError("secret plaintext must not be empty")
        nonce = os.urandom(self.NONCE_SIZE)
        return EncryptedPayload(
            ciphertext=self._aes_gcm.encrypt(nonce, plaintext, None),
            nonce=nonce,
            key_id=self._key_id,
        )

    def decrypt(self, payload: EncryptedPayload) -> bytes:
        if payload.key_id != self._key_id:
            raise SecretDecryptionError("secret was encrypted with an unavailable key")
        if len(payload.nonce) != self.NONCE_SIZE:
            raise SecretDecryptionError("secret nonce is invalid")
        try:
            return self._aes_gcm.decrypt(payload.nonce, payload.ciphertext, None)
        except InvalidTag as error:
            raise SecretDecryptionError("secret authentication failed") from error

    @classmethod
    def _decode_master_key(cls, value: SecretStr | str | bytes) -> bytes:
        raw_value = value.get_secret_value() if isinstance(value, SecretStr) else value
        encoded = raw_value.encode("ascii") if isinstance(raw_value, str) else raw_value
        try:
            key = base64.b64decode(encoded, altchars=b"-_", validate=True)
        except (ValueError, binascii.Error, UnicodeEncodeError) as error:
            raise InvalidMasterKeyError("APP_MASTER_KEY must be valid base64") from error
        if len(key) != cls.KEY_SIZE:
            raise InvalidMasterKeyError("APP_MASTER_KEY must decode to exactly 32 bytes")
        return key
