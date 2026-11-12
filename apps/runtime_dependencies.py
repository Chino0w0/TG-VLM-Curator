from __future__ import annotations

from uuid import UUID

from tgcurator.application.ports.cleanup import MappingArchiveStorageResolver
from tgcurator.application.ports.secrets import SecretVault
from tgcurator.application.settings import Settings
from tgcurator.infrastructure.archive import LocalArchiveStorage
from tgcurator.infrastructure.database.secret_vault import SqlAlchemySecretVault
from tgcurator.infrastructure.database.session import AsyncDatabase
from tgcurator.infrastructure.security import AesGcmSecretCipher


class VaultInferenceSecretResolver:
    def __init__(self, vault: SecretVault) -> None:
        self.vault = vault

    async def resolve(self, *, secret_reference: str) -> str:
        raw = await self.vault.resolve(secret_id=UUID(secret_reference.strip()))
        return raw.decode("utf-8")


def configured_secret_vault(
    *, settings: Settings, database: AsyncDatabase
) -> SqlAlchemySecretVault | None:
    if settings.app_master_key is None:
        return None
    cipher = AesGcmSecretCipher(
        master_key=settings.app_master_key, key_id=settings.app_master_key_id
    )
    return SqlAlchemySecretVault(database, cipher)


def configured_archive_storage(settings: Settings) -> MappingArchiveStorageResolver | None:
    if settings.archive_root is None:
        return None
    return MappingArchiveStorageResolver({"local": LocalArchiveStorage(root=settings.archive_root)})
