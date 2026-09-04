from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.models import Base


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        engine_url, engine_options = async_engine_configuration(url)
        self.engine: AsyncEngine = create_async_engine(engine_url, pool_pre_ping=True, **engine_options)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)

    async def initialize_test_schema(self) -> None:
        if self.url.startswith("sqlite"):
            async with self.engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)

    async def check_connection(self) -> None:
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session:
            async with session.begin():
                yield session

    async def close(self) -> None:
        await self.engine.dispose()


def async_engine_configuration(url: str) -> tuple[str, dict]:
    if url.startswith("postgresql+asyncpg"):
        connect_args = {
            "statement_cache_size": 0,
            "prepared_statement_name_func": lambda: f"__asyncpg_{uuid4().hex}__",
        }
        return url, {"poolclass": NullPool, "connect_args": connect_args}
    return url, {}
