"""DEV-2400: the DB connection pool is now sized deliberately, not by default.

`create_async_engine`'s own defaults -- pool_size 5, max_overflow 10 -- were
never chosen for this workload; nothing here had ever set them before this
ticket. Production hit `QueuePool limit of size 5 overflow 10 reached` with
all 15 connections checked out at once. See the long comment on
`Settings.database_pool_size` for the full reasoning; this just pins the
resulting contract: the settings exist, have sane bounds, and `db.py` actually
wires them into the engine it builds.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from harborbox.config import Settings
from harborbox.db import engine


def test_defaults_double_sqlalchemys_own_ceiling() -> None:
    """15 (SQLAlchemy's default) was what actually exhausted in production."""
    expected_pool_size = 10
    expected_max_overflow = 20
    expected_ceiling = 30  # double SQLAlchemy's own default (5 + 10 = 15)

    settings = Settings()

    assert settings.database_pool_size == expected_pool_size
    assert settings.database_max_overflow == expected_max_overflow
    assert (
        settings.database_pool_size + settings.database_max_overflow
        == expected_ceiling
    )


def test_pool_size_must_be_at_least_one() -> None:
    with pytest.raises(ValidationError):
        Settings(database_pool_size=0)


def test_max_overflow_may_be_zero_but_not_negative() -> None:
    Settings(database_max_overflow=0)
    with pytest.raises(ValidationError):
        Settings(database_max_overflow=-1)


def test_both_are_configurable_independently_of_each_other() -> None:
    custom_pool_size = 25
    custom_max_overflow = 5

    settings = Settings(
        database_pool_size=custom_pool_size, database_max_overflow=custom_max_overflow
    )

    assert settings.database_pool_size == custom_pool_size
    assert settings.database_max_overflow == custom_max_overflow


def test_the_module_level_engine_is_actually_built_with_these_settings() -> None:
    """Regression guard for `db.py` wiring the settings into the engine.

    A change to `Settings.database_pool_size` that is never read by
    `create_async_engine` would pass every other test in this file and still
    leave production on the old, unconfigured ceiling -- this is what would
    have caught that.
    """
    defaults = Settings()
    assert engine.pool.size() == defaults.database_pool_size
    # SQLAlchemy exposes the configured pool size publicly (`.size()`) but not
    # max_overflow; `_max_overflow` is the pool's own stored constructor
    # argument, not a computed/private-in-spirit value.
    assert engine.pool._max_overflow == defaults.database_max_overflow
