from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from harborbox.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
# pool_size/max_overflow: see the long comment on Settings.database_pool_size
# for why these are set explicitly rather than left to SQLAlchemy's defaults
# (DEV-2400).
engine: AsyncEngine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=settings.database_pool_size,
    max_overflow=settings.database_max_overflow,
)
session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def create_schema() -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        # create_all adds missing tables, never missing columns, and there is no
        # migration tool here. Idempotent and cheap, so it runs every boot.
        await connection.execute(
            text(
                "ALTER TABLE sandboxes "
                "ADD COLUMN IF NOT EXISTS egress BOOLEAN NOT NULL DEFAULT FALSE"
            )
        )


async def get_session() -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session

