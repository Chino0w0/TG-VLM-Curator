from __future__ import annotations

import base64
import hashlib
from uuid import UUID

from fastapi import Request

from tgcurator.application.admin import (
    SESSION_COOKIE_NAME,
    AdminApplicationService,
    AdminSessionService,
    InMemoryAdminBackend,
    InMemorySecretVault,
    PromptTestBenchService,
)
from tgcurator.application.ports.admin_api import AuthenticatedAdmin
from tgcurator.application.ports.secrets import SecretVault
from tgcurator.application.settings import Settings
from tgcurator.infrastructure.database import (
    AsyncDatabase,
    SqlAlchemyAdminBackend,
    SqlAlchemySecretVault,
)
from tgcurator.infrastructure.inference import OpenAICompatibleInferenceProvider
from tgcurator.infrastructure.security import AesGcmSecretCipher, Argon2idPasswordHasher

_NON_PROD_FALLBACK_KEY = base64.b64encode(b"tgcurator-dev-test-master-key-32").decode("ascii")


class _VaultInferenceSecretResolver:
    """Resolve encrypted inference API credentials from SecretVault by UUID reference."""

    def __init__(self, vault: SecretVault) -> None:
        self._vault = vault

    async def resolve(self, *, secret_reference: str) -> str:
        raw = await self._vault.resolve(secret_id=UUID(secret_reference.strip()))
        return raw.decode("utf-8")


def build_default_admin_service(*, settings: Settings, database: object) -> AdminApplicationService:
    """Construct the runtime AdminApplicationService from process settings and database."""
    if settings.app_master_key is not None:
        raw_master = settings.app_master_key.get_secret_value()
        cipher = AesGcmSecretCipher(
            master_key=settings.app_master_key,
            key_id=settings.app_master_key_id,
        )
        csrf_secret = hashlib.sha256(b"tgcurator-csrf\x00" + raw_master.encode("utf-8")).digest()
    else:
        cipher = AesGcmSecretCipher(
            master_key=_NON_PROD_FALLBACK_KEY,
            key_id=settings.app_master_key_id,
        )
        csrf_secret = hashlib.sha256(b"tgcurator-csrf\x00non-prod-fallback").digest()

    if isinstance(database, AsyncDatabase):
        backend = SqlAlchemyAdminBackend(database)
        secrets_vault: SecretVault = SqlAlchemySecretVault(database, cipher)
        provider = OpenAICompatibleInferenceProvider(
            secret_resolver=_VaultInferenceSecretResolver(secrets_vault)
        )
        test_bench = PromptTestBenchService(provider=provider)
    else:
        backend = InMemoryAdminBackend()
        secrets_vault = InMemorySecretVault(key_id=settings.app_master_key_id)
        test_bench = PromptTestBenchService(provider=None)

    sessions = AdminSessionService(
        repository=backend,
        password_hasher=Argon2idPasswordHasher(),
        csrf_secret=csrf_secret,
    )
    return AdminApplicationService(
        sessions=sessions,
        secrets=secrets_vault,
        backend=backend,
        test_bench=test_bench,
    )


def get_admin_service(request: Request) -> AdminApplicationService:
    service: AdminApplicationService | None = getattr(request.app.state, "admin_service", None)
    if service is None:
        service = build_default_admin_service(
            settings=request.app.state.settings,
            database=request.app.state.database,
        )
        request.app.state.admin_service = service
    return service


async def require_authenticated_admin(request: Request) -> AuthenticatedAdmin:
    service = get_admin_service(request)
    cookie_token = request.cookies.get(SESSION_COOKIE_NAME)
    return await service.sessions.authenticate(session_token=cookie_token)


async def require_csrf_admin(request: Request) -> AuthenticatedAdmin:
    service = get_admin_service(request)
    admin = await require_authenticated_admin(request)
    service.sessions.verify_csrf(
        admin=admin,
        csrf_header=request.headers.get("X-CSRF-Token"),
    )
    return admin


__all__ = [
    "build_default_admin_service",
    "get_admin_service",
    "require_authenticated_admin",
    "require_csrf_admin",
]
