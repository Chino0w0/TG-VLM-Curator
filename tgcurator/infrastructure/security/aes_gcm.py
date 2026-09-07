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
    """Raised when encrypted secret material cannot be authenticated."""


@dataclass(frozen=True, slots=True)
class EncryptedPayload:
    ciphertext: bytes
    nonce: bytes
    key_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.ciphertext, bytes) or len(self.ciphertext) < 16:
            raise ValueError("ciphertext must contain an AES-GCM authentication tag")
        if not isinstance(self.nonce, bytes) or len(self.nonce) != AesGcmSecretCipher.NONCE_SIZE:
            raise ValueError("nonce must be exactly 12 bytes")
        if not isinstance(self.key_id, str) or not self.key_id.strip():
            raise ValueError("key_id must not be blank")


class AesGcmSecretCipher:
    """AES-256-GCM encryption for persisted secret payloads."""

    NONCE_SIZE = 12
    KEY_SIZE = 32
    _AAD_PREFIX = b"tg-vlm-curator\x00"

    def __init__(self, *, master_key: SecretStr | str | bytes, key_id: str = "primary") -> None:
        if not isinstance(key_id, str) or not key_id.strip():
            raise ValueError("key_id must not be blank")
        normalized_key_id = key_id.strip()
        if len(normalized_key_id) > 128:
            raise ValueError("key_id must be at most 128 characters")
        self._key_id = normalized_key_id
        self._aes_gcm = AESGCM(self._decode_master_key(master_key))

    @property
    def key_id(self) -> str:
        return self._key_id

    def encrypt(
        self,
        plaintext: bytes,
        *,
        associated_data: bytes | None = None,
    ) -> EncryptedPayload:
        if not isinstance(plaintext, bytes) or not plaintext:
            raise ValueError("secret plaintext must be non-empty bytes")
        aad = self._associated_data(associated_data)
        nonce = os.urandom(self.NONCE_SIZE)
        return EncryptedPayload(
            ciphertext=self._aes_gcm.encrypt(nonce, plaintext, aad),
            nonce=nonce,
            key_id=self._key_id,
        )

    def decrypt(
        self,
        payload: EncryptedPayload,
        *,
        associated_data: bytes | None = None,
    ) -> bytes:
        if not isinstance(payload, EncryptedPayload):
            raise SecretDecryptionError("encrypted payload is invalid")
        if payload.key_id != self._key_id:
            raise SecretDecryptionError("secret was encrypted with an unavailable key")
        aad = self._associated_data(associated_data)
        try:
            return self._aes_gcm.decrypt(payload.nonce, payload.ciphertext, aad)
        except InvalidTag as error:
            raise SecretDecryptionError("secret authentication failed") from error

    def _associated_data(self, associated_data: bytes | None) -> bytes:
        if associated_data is not None and not isinstance(associated_data, bytes):
            raise TypeError("associated_data must be bytes or None")
        return self._AAD_PREFIX + self._key_id.encode("utf-8") + b"\x00" + (associated_data or b"")

    @classmethod
    def _decode_master_key(cls, value: SecretStr | str | bytes) -> bytes:
        raw_value = value.get_secret_value() if isinstance(value, SecretStr) else value
        if isinstance(raw_value, str):
            try:
                encoded = raw_value.strip().encode("ascii")
            except UnicodeEncodeError as error:
                raise InvalidMasterKeyError("APP_MASTER_KEY must be valid base64") from error
        elif isinstance(raw_value, bytes):
            encoded = raw_value.strip()
        else:
            raise InvalidMasterKeyError("APP_MASTER_KEY must be a base64 string")
        try:
            key = base64.b64decode(encoded, altchars=b"-_", validate=True)
        except (ValueError, binascii.Error) as error:
            raise InvalidMasterKeyError("APP_MASTER_KEY must be valid base64") from error
        if len(key) != cls.KEY_SIZE:
            raise InvalidMasterKeyError("APP_MASTER_KEY must decode to exactly 32 bytes")
        return key
