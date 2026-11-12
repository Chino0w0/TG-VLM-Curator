from __future__ import annotations

import unittest
from datetime import UTC, datetime
from uuid import uuid4

from tgcurator.domain.audit import (
    AUDIT_REDACTED,
    AuditActorType,
    AuditRecordDraft,
    is_sensitive_audit_key,
    sanitize_audit_payload,
)
from tgcurator.domain.telegram import (
    TelegramCapabilityResolver,
    TelegramIdentityHealth,
    TelegramIdentityKind,
)
from tgcurator.shared import DomainValidationError, decode_cursor, encode_cursor


class TelegramCapabilitiesAndAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.resolver = TelegramCapabilityResolver()

    def test_mtproto_user_has_full_source_and_destination_capabilities(self) -> None:
        caps = self.resolver.resolve(
            identity_type=TelegramIdentityKind.MTPROTO_USER,
            enabled=True,
            health_status=TelegramIdentityHealth.HEALTHY,
        )
        self.assertTrue(caps.can_read_source)
        self.assertTrue(caps.can_download_source_media)
        self.assertTrue(caps.can_native_forward)
        self.assertTrue(caps.can_upload_media)
        self.assertTrue(caps.can_send_message)
        self.assertTrue(caps.can_access_destination)
        self.assertEqual(caps.reasons, ())

    def test_bot_identity_cannot_read_historical_sources(self) -> None:
        caps = self.resolver.resolve(
            identity_type="bot",
            enabled=True,
            health_status="healthy",
        )
        self.assertFalse(caps.can_read_source)
        self.assertFalse(caps.can_download_source_media)
        self.assertTrue(caps.can_native_forward)
        self.assertTrue(caps.can_upload_media)
        self.assertTrue(caps.can_send_message)
        self.assertIn("bot_history_read_restricted", caps.reasons)

    def test_protected_content_enforces_non_bypassable_download_and_forward_restrictions(
        self,
    ) -> None:
        caps = self.resolver.resolve(
            identity_type="mtproto_user",
            enabled=True,
            health_status="healthy",
            source_protected_content=True,
            capability_overrides={
                "can_download_source_media": True,
                "can_native_forward": True,
            },
        )
        self.assertTrue(caps.can_read_source)
        self.assertFalse(caps.can_download_source_media)
        self.assertFalse(caps.can_native_forward)
        self.assertTrue(caps.can_send_message)
        self.assertIn("source_protected_content", caps.reasons)

    def test_disabled_and_failed_identities_block_all_capabilities(self) -> None:
        disabled = self.resolver.resolve(
            identity_type="mtproto_user",
            enabled=False,
            health_status="healthy",
        )
        self.assertFalse(disabled.can_read_source)
        self.assertFalse(disabled.can_send_message)
        self.assertEqual(disabled.reasons, ("identity_disabled",))

        failed = self.resolver.resolve(
            identity_type="bot",
            enabled=True,
            health_status="failed",
        )
        self.assertFalse(failed.can_send_message)
        self.assertEqual(failed.reasons, ("identity_failed",))

    def test_missing_permissions_and_invalid_overrides_are_enforced(self) -> None:
        caps = self.resolver.resolve(
            identity_type="mtproto_user",
            enabled=True,
            health_status="degraded",
            has_source_access=False,
            has_destination_post_permission=False,
        )
        self.assertFalse(caps.can_read_source)
        self.assertFalse(caps.can_access_destination)
        self.assertIn("identity_degraded", caps.reasons)
        self.assertIn("source_access_missing", caps.reasons)
        self.assertIn("destination_permission_missing", caps.reasons)

        with self.assertRaises(DomainValidationError):
            self.resolver.resolve(
                identity_type="mtproto_user",
                enabled=True,
                capability_overrides={"unknown_capability": True},
            )

    def test_audit_sanitizer_redacts_secrets_while_preserving_safe_metadata(self) -> None:
        self.assertTrue(is_sensitive_audit_key("plaintext"))
        self.assertTrue(is_sensitive_audit_key("api_key"))
        self.assertTrue(is_sensitive_audit_key("session_string"))
        self.assertFalse(is_sensitive_audit_key("secret_id"))
        self.assertFalse(is_sensitive_audit_key("secret_configured"))
        self.assertFalse(is_sensitive_audit_key("api_secret_reference"))

        secret_id = str(uuid4())
        sanitized = sanitize_audit_payload(
            {
                "secret_id": secret_id,
                "secret_type": "inference_api_key",
                "secret_configured": True,
                "plaintext": "sk-super-secret-value",
                "ciphertext": b"\x01\x02\x03",
                "nested": {
                    "bot_token": "123456:ABCDEF",
                    "header": "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9",
                    "inline": "api_key=sk-inline-secret",
                    "prompt_tokens": 120,
                },
            }
        )
        self.assertEqual(sanitized["secret_id"], secret_id)
        self.assertEqual(sanitized["secret_type"], "inference_api_key")
        self.assertTrue(sanitized["secret_configured"])
        self.assertEqual(sanitized["plaintext"], AUDIT_REDACTED)
        self.assertEqual(sanitized["ciphertext"], AUDIT_REDACTED)
        self.assertEqual(sanitized["nested"]["bot_token"], AUDIT_REDACTED)
        self.assertNotIn("eyJhbGciOiJIUzI1NiJ9", sanitized["nested"]["header"])
        self.assertNotIn("sk-inline-secret", sanitized["nested"]["inline"])
        self.assertEqual(sanitized["nested"]["prompt_tokens"], 120)

        draft = AuditRecordDraft(
            event_id=str(uuid4()),
            actor_type=AuditActorType.ADMIN,
            actor_admin_user_id=str(uuid4()),
            event_type="secret.rotated",
            entity_type="encrypted_secret",
            entity_id=secret_id,
            payload={"secret_id": secret_id, "password": "top-secret-password"},
            created_at=datetime(2026, 11, 12, 8, 0, tzinfo=UTC),
        )
        self.assertEqual(draft.payload["password"], AUDIT_REDACTED)

    def test_cursor_pagination_round_trip_and_tamper_rejection(self) -> None:
        ts = datetime(2026, 11, 12, 10, 30, tzinfo=UTC)
        uid = uuid4()
        encoded = encode_cursor(timestamp=ts, entity_id=uid)
        decoded = decode_cursor(encoded)
        self.assertEqual(decoded.timestamp, ts)
        self.assertEqual(decoded.entity_id, str(uid))

        with self.assertRaises(DomainValidationError):
            decode_cursor("not-a-valid-cursor!!!")


if __name__ == "__main__":
    unittest.main()
