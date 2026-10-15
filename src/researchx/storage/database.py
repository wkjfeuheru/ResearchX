"""Explicit asynchronous PostgreSQL lifecycle; never create schema during requests."""

from __future__ import annotations

import os
import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from collections.abc import Iterator
from hashlib import sha256
from pathlib import Path

from asyncpg import PostgresError  # type: ignore[import-untyped]
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

SCHEMA_REVISION = "0007_research_assets"

# Request/task dependency, not a storage singleton: the owner closes its pool.
_bound_database: ContextVar[Database | None] = ContextVar("researchx_database", default=None)


def current_database() -> Database:
    database = _bound_database.get()
    if database is None:
        raise DatabaseConfigurationError("数据库生命周期尚未初始化。")
    return database


@contextmanager
def bind_database(database: Database) -> Iterator[None]:
    token = _bound_database.set(database)
    try:
        yield
    finally:
        _bound_database.reset(token)


class DatabaseConfigurationError(RuntimeError):
    """A safe public error which must not contain a connection string."""


def workspace_id(cwd: str | Path) -> str:
    return sha256(str(Path(cwd).resolve()).encode()).hexdigest()[:16]


class Database:
    def __init__(self, url: str | None = None) -> None:
        raw = url if url is not None else os.environ.get("RESEARCHX_DATABASE_URL", "")
        try:
            parsed = make_url(raw)
            if parsed.drivername not in {"postgresql", "postgresql+asyncpg"}:
                raise ValueError("PostgreSQL required")
            parsed = parsed.set(drivername="postgresql+asyncpg")
        except (ValueError, SQLAlchemyError):
            raise DatabaseConfigurationError(
                "请配置 RESEARCHX_DATABASE_URL 为 PostgreSQL 连接；不支持本地存储回退。"
            ) from None
        try:
            pool_size = int(os.environ.get("RESEARCHX_DB_POOL_SIZE", "5"))
            overflow = int(os.environ.get("RESEARCHX_DB_MAX_OVERFLOW", "5"))
            if not 1 <= pool_size <= 100 or not 0 <= overflow <= 100:
                raise ValueError
        except ValueError:
            raise DatabaseConfigurationError(
                "数据库连接池配置无效：pool_size 为 1..100，max_overflow 为 0..100"
            ) from None
        self.engine = create_async_engine(
            parsed,
            pool_pre_ping=True,
            pool_size=pool_size,
            max_overflow=overflow,
            pool_timeout=30,
            hide_parameters=True,
            connect_args={"timeout": 10, "command_timeout": 30},
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self._transaction: ContextVar[tuple[asyncio.Task[object] | None, AsyncSession] | None] = (
            ContextVar(f"researchx_transaction_{id(self)}", default=None)
        )

    async def check(self) -> None:
        try:
            async with self.engine.connect() as connection:
                revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
                if revision != SCHEMA_REVISION:
                    raise DatabaseConfigurationError("数据库 Schema 版本不匹配，请执行迁移。")
        except (SQLAlchemyError, PostgresError, OSError, TimeoutError, asyncio.TimeoutError):
            raise DatabaseConfigurationError(
                "PostgreSQL 不可用或尚未迁移；请检查连接配置并执行数据库迁移。"
            ) from None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        active = self._transaction.get()
        if active is not None and active[0] is asyncio.current_task():
            yield active[1]
            return
        async with self.sessions.begin() as session:
            token = self._transaction.set((asyncio.current_task(), session))
            try:
                yield session
            finally:
                self._transaction.reset(token)

    def active_session(self) -> AsyncSession:
        active = self._transaction.get()
        if active is None or active[0] is not asyncio.current_task():
            raise RuntimeError("A database transaction is required")
        return active[1]

    async def close(self) -> None:
        await self.engine.dispose()

    async def __aenter__(self) -> Database:
        try:
            await self.check()
        except BaseException:
            await self.close()
            raise
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()


@asynccontextmanager
async def database_lifespan() -> AsyncIterator[Database]:
    """Own the pool at a CLI/evaluation entry, or reuse the caller's explicit scope."""
    existing = _bound_database.get()
    if existing is not None:
        yield existing
        return
    async with Database() as database:
        with bind_database(database):
            yield database
