"""The boot-time column additions in `harborbox.db.create_schema`.

There is no migration tool here: `create_all` adds missing tables, and a few
idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` statements add missing
columns. That syntax is PostgreSQL's, so this needs a real one; CI points
``HARBORBOX_TEST_DATABASE_URL`` at it and without that variable the module skips.

Each test works in its own schema, because the database is shared with the
other PostgreSQL tests and this one has to drop columns to fake an old table.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from harborbox import db

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.postgres


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    url = os.environ.get("HARBORBOX_TEST_DATABASE_URL")
    if not url:
        pytest.skip("HARBORBOX_TEST_DATABASE_URL is not set; needs a real PostgreSQL")
    schema = f"migration_{uuid4().hex}"
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped = create_async_engine(
        url, connect_args={"server_settings": {"search_path": schema}}
    )
    try:
        yield scoped
    finally:
        await scoped.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


async def test_a_template_table_from_before_dev_2611_gains_a_null_version(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rows built before the column existed must read as stale, i.e. NULL."""
    async with engine.begin() as connection:
        await connection.run_sync(db.Base.metadata.create_all)
        for table in ("sandbox_templates", "sandboxes"):
            await connection.execute(
                text(f"ALTER TABLE {table} DROP COLUMN conformance_version")
            )
        await connection.execute(
            text(
                "INSERT INTO sandbox_templates "
                "(name, base, spec_hash, spec, image, status, memory_mb, cpu, "
                "created_at, updated_at, last_used_at) VALUES "
                "('custom-a1b2c3d4e5f6', '', 'a1b2c3d4e5f6', '{}', "
                "'harborbox-sandbox-custom-a1b2c3d4e5f6:local', 'ready', 512, 1.0, "
                "now(), now(), now())"
            )
        )
    monkeypatch.setattr(db, "engine", engine)

    await db.create_schema()
    # Every boot runs it, so a second run must be a no-op, not an error.
    await db.create_schema()

    async with engine.connect() as connection:
        version = await connection.scalar(
            text("SELECT conformance_version FROM sandbox_templates")
        )
        sandbox_columns = await connection.scalar(
            text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = 'sandboxes' "
                "AND column_name = 'conformance_version'"
            )
        )
    assert version is None
    assert sandbox_columns == 1
