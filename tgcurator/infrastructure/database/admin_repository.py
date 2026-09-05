from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from tgcurator.infrastructure.database.models import AdminUser
from tgcurator.infrastructure.database.session import AsyncDatabase


class SqlAlchemyAdminBootstrapRepository:
    """PostgreSQL implementation guarded by the single-active-admin unique index."""

    def __init__(self, database: AsyncDatabase) -> None:
        self._database = database

    async def create_first_active_admin(self, *, username: str, password_hash: str) -> UUID | None:
        try:
            async with self._database.session() as session:
                async with session.begin():
                    existing = await session.scalar(
                        select(AdminUser.id).where(AdminUser.is_active.is_(True)).limit(1)
                    )
                    if existing is not None:
                        return None

                    admin = AdminUser(
                        username=username,
                        password_hash=password_hash,
                        is_active=True,
                    )
                    session.add(admin)
                    await session.flush()
                    return admin.id
        except IntegrityError:
            # A concurrent bootstrap won the partial unique index race.
            return None
