from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from tgcurator.application.ports.admin import PasswordHasher
from tgcurator.application.ports.admin_api import (
    AdminSessionRepository,
    AuthenticatedAdmin,
)
from tgcurator.domain.audit import sanitize_audit_payload

from .errors import AdminAuthenticationError, AdminCsrfError

SESSION_COOKIE_NAME = "tgcurator_session"


class AdminSessionService:
    """Manage single-administrator Argon2id login, hashed sessions, and CSRF verification."""

    def __init__(
        self,
        *,
        repository: AdminSessionRepository,
        password_hasher: PasswordHasher,
        csrf_secret: bytes,
        session_ttl: timedelta = timedelta(hours=12),
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], UUID] | None = None,
    ) -> None:
        if not isinstance(csrf_secret, bytes) or len(csrf_secret) < 16:
            raise ValueError("csrf_secret must contain at least 16 bytes")
        if session_ttl <= timedelta(0):
            raise ValueError("session_ttl must be positive")
        self._repository = repository
        self._password_hasher = password_hasher
        self._csrf_secret = csrf_secret
        self._session_ttl = session_ttl
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or uuid4

    async def login(
        self,
        *,
        username: str,
        password: str,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> tuple[str, AuthenticatedAdmin]:
        normalized_username = username.strip() if isinstance(username, str) else ""
        if not normalized_username or not isinstance(password, str) or not password:
            raise AdminAuthenticationError("invalid administrator credentials")

        account = await self._repository.find_active_admin_by_username(username=normalized_username)
        if account is None or not account.is_active:
            raise AdminAuthenticationError("invalid administrator credentials")
        if not self._password_hasher.verify(account.password_hash, password):
            raise AdminAuthenticationError("invalid administrator credentials")

        now = self._clock()
        expires_at = now + self._session_ttl
        session_id = str(self._id_factory())
        raw_session_token = secrets.token_urlsafe(32)
        session_token_hash = _sha256_hex(raw_session_token)
        csrf_token = self._derive_csrf_token(raw_session_token)
        csrf_token_hash = _sha256_hex(csrf_token)

        await self._repository.create_session(
            session_id=session_id,
            admin_user_id=account.user_id,
            session_token_hash=session_token_hash,
            csrf_token_hash=csrf_token_hash,
            expires_at=expires_at,
            now=now,
            user_agent=_truncate(user_agent, 512),
            ip_address=_truncate(ip_address, 128),
        )
        await self._repository.record_audit(
            actor_admin_user_id=account.user_id,
            event_type="admin.login",
            entity_type="admin_session",
            entity_id=session_id,
            payload=sanitize_audit_payload(
                {
                    "username": account.username,
                    "ip_address": _truncate(ip_address, 128),
                    "expires_at": expires_at.isoformat(),
                }
            ),
            now=now,
        )
        return (
            raw_session_token,
            AuthenticatedAdmin(
                session_id=session_id,
                user_id=account.user_id,
                username=account.username,
                csrf_token=csrf_token,
                csrf_token_hash=csrf_token_hash,
                expires_at=expires_at,
            ),
        )

    async def authenticate(self, *, session_token: str | None) -> AuthenticatedAdmin:
        if not isinstance(session_token, str) or not session_token.strip():
            raise AdminAuthenticationError("authentication session is required")
        raw_token = session_token.strip()
        token_hash = _sha256_hex(raw_token)
        state = await self._repository.get_session_by_token_hash(session_token_hash=token_hash)
        if state is None:
            raise AdminAuthenticationError("authentication session is invalid")
        now = self._clock()
        if state.revoked_at is not None or state.expires_at <= now:
            raise AdminAuthenticationError("authentication session has expired or been revoked")

        csrf_token = self._derive_csrf_token(raw_token)
        if not secrets.compare_digest(_sha256_hex(csrf_token), state.csrf_token_hash):
            raise AdminAuthenticationError("authentication session integrity check failed")

        await self._repository.touch_session(session_id=state.session_id, now=now)
        return AuthenticatedAdmin(
            session_id=state.session_id,
            user_id=state.admin_user_id,
            username=state.username,
            csrf_token=csrf_token,
            csrf_token_hash=state.csrf_token_hash,
            expires_at=state.expires_at,
        )

    @staticmethod
    def verify_csrf(*, admin: AuthenticatedAdmin, csrf_header: str | None) -> None:
        if not isinstance(csrf_header, str) or not csrf_header.strip():
            raise AdminCsrfError("missing X-CSRF-Token header")
        provided_hash = _sha256_hex(csrf_header.strip())
        if not secrets.compare_digest(provided_hash, admin.csrf_token_hash):
            raise AdminCsrfError("invalid X-CSRF-Token header")

    async def logout(self, *, admin: AuthenticatedAdmin) -> None:
        now = self._clock()
        await self._repository.revoke_session(session_id=admin.session_id, now=now)
        await self._repository.record_audit(
            actor_admin_user_id=admin.user_id,
            event_type="admin.logout",
            entity_type="admin_session",
            entity_id=admin.session_id,
            payload={"username": admin.username},
            now=now,
        )

    def _derive_csrf_token(self, raw_session_token: str) -> str:
        return hmac.new(
            self._csrf_secret,
            raw_session_token.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _truncate(value: str | None, max_len: int) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped[:max_len] if stripped else None
