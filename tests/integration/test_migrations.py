import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import run_alembic

pytestmark = pytest.mark.db

M2_TABLES = {
    "products",
    "variants",
    "listings",
    "price_observations",
    "offers",
    "evidence",
    "sale_events",
}


async def _tables(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        return set(await conn.run_sync(lambda c: inspect(c).get_table_names()))


async def test_upgrade_and_downgrade_roundtrip(test_engine: AsyncEngine) -> None:
    await run_alembic("downgrade", "base")
    assert not ({"users"} | M2_TABLES) & await _tables(test_engine)
    await run_alembic("upgrade", "head")
    assert {"users"} | M2_TABLES <= await _tables(test_engine)
    await run_alembic("downgrade", "0001")
    assert not M2_TABLES & await _tables(test_engine)
    await run_alembic("upgrade", "head")


async def test_models_match_migrations(test_engine: AsyncEngine) -> None:
    await run_alembic("upgrade", "head")
    await run_alembic("check")
