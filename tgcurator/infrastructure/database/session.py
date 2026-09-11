from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import make_url, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


class InvalidDatabaseUrlError(ValueError):
    """Raised when the runtime database is not PostgreSQL through asyncpg."""


def validate_async_database_url(database_url: str) -> URL:
    try:
        parsed = make_url(database_url)
    except ArgumentError as error:
        raise InvalidDatabaseUrlError("database URL is invalid") from error
    if parsed.drivername != "postgresql+asyncpg":
        raise InvalidDatabaseUrlError("database URL must use postgresql+asyncpg")
    return parsed


class AsyncDatabase:
    """Own one lazy async SQLAlchemy engine for a process."""

    def __init__(self, database_url: str | None) -> None:
        normalized = database_url.strip() if isinstance(database_url, str) else None
        self._database_url = normalized or None
        if self._database_url is not None:
            validate_async_database_url(self._database_url)
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None
        self._connect_lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return self._database_url is not None

    async def connect(self) -> None:
        if self._engine is not None:
            return
        if self._database_url is None:
            raise RuntimeError("database is not configured")
        async with self._connect_lock:
            if self._engine is not None:
                return
            engine = create_async_engine(self._database_url, pool_pre_ping=True)
            self._engine = engine
            self._session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def ping(self) -> bool:
        if not self.configured:
            return False
        try:
            await self.connect()
            assert self._engine is not None
            async with self._engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception:
            return False
        return True

    @asynccontextmanager
    async def session(self, *, isolation_level: str | None = None) -> AsyncIterator[AsyncSession]:
        await self.connect()
        assert self._session_factory is not None
        async with self._session_factory() as session:
            if isolation_level is not None:
                await session.connection(execution_options={"isolation_level": isolation_level})
            yield session

    async def dispose(self) -> None:
        async with self._connect_lock:
            if self._engine is not None:
                await self._engine.dispose()
                self._engine = None
                self._session_factory = None
