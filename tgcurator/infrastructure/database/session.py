from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


class AsyncDatabase:
    """Owns one async SQLAlchemy engine and never embeds business transactions in adapters."""

    def __init__(self, database_url: str | None) -> None:
        self._database_url = database_url
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    @property
    def configured(self) -> bool:
        return self._database_url is not None

    async def connect(self) -> None:
        if self._engine is not None:
            return
        if self._database_url is None:
            raise RuntimeError("database is not configured")
        self._engine = create_async_engine(self._database_url, pool_pre_ping=True)
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)

    async def ping(self) -> bool:
        if not self.configured:
            return False
        await self.connect()
        assert self._engine is not None
        try:
            async with self._engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception:
            return False
        return True

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        await self.connect()
        assert self._session_factory is not None
        async with self._session_factory() as session:
            yield session

    async def dispose(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None
