import asyncio
import os
import sys
from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from shopping_agent.config import Settings, get_settings
from shopping_agent.db.session import create_engine, create_session_factory, ping
from shopping_agent.providers.cache import InMemoryCache
from shopping_agent.providers.fixtures import (
    FixturePageFetcher,
    FixtureProductProvider,
    FixtureWebProvider,
)
from shopping_agent.providers.quota import QuotaGuard
from shopping_agent.providers.registry import ProviderRegistry
from shopping_agent.runtime import Runtime
from shopping_agent.tools import ToolRegistry, all_tools
from shopping_agent.tools.base import ToolContext
from tests.factories import NOW
from tests.fakes import scripted_client
from tests.scenarios import discovery_handler

APP_TABLES = (
    "tool_calls, agent_runs, price_observations, offers, evidence, listings, variants, "
    "products, sale_events, payment_methods, memberships, exchange_devices, "
    "user_preferences, users"
)


async def _redis_up(url: str) -> bool:
    client = Redis.from_url(url)
    try:
        return bool(await asyncio.wait_for(client.ping(), timeout=2))
    except Exception:
        return False
    finally:
        await client.aclose()


async def run_alembic(*args: str) -> None:
    env = {**os.environ, "DATABASE_URL": get_settings().test_database_url.get_secret_value()}
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "alembic",
        *args,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    assert proc.returncode == 0, out.decode(errors="replace")


@pytest.fixture(scope="session")
async def redis_url() -> str:
    url = get_settings().redis_url
    if not await _redis_up(url):
        pytest.skip("Redis is not reachable")
    return url


@pytest.fixture(scope="session")
async def test_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_engine(get_settings().test_database_url.get_secret_value())
    if not await ping(engine):
        await engine.dispose()
        pytest.skip("Postgres test database is not reachable (check TEST_DATABASE_URL)")
    yield engine
    await engine.dispose()


@pytest.fixture(scope="session")
async def migrated_engine(test_engine: AsyncEngine) -> AsyncEngine:
    await run_alembic("upgrade", "head")
    return test_engine


@pytest.fixture
async def db(migrated_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory on a freshly emptied test database."""
    async with migrated_engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {APP_TABLES} RESTART IDENTITY CASCADE"))
    return create_session_factory(migrated_engine)


@pytest.fixture
def demo_registry() -> ProviderRegistry:
    cache = InMemoryCache()
    return ProviderRegistry(
        products=[FixtureProductProvider(clock=lambda: NOW)],
        web=[FixtureWebProvider()],
        fetcher=FixturePageFetcher(),
        cache=cache,
        quota=QuotaGuard(cache, {}),
    )


@pytest.fixture
def tool_ctx(db: async_sessionmaker[AsyncSession], demo_registry: ProviderRegistry) -> ToolContext:
    return ToolContext(providers=demo_registry, sessions=db, clock=lambda: NOW)


@pytest.fixture
async def runtime(
    migrated_engine: AsyncEngine,
    db: async_sessionmaker[AsyncSession],
    demo_registry: ProviderRegistry,
) -> AsyncIterator[Runtime]:
    """A runtime on the test database, the demo catalog, a fixed clock and a scripted model
    that plays the standard discovery scenario."""
    llm, _ = scripted_client(discovery_handler)
    async with httpx.AsyncClient() as http:
        yield Runtime(
            settings=Settings(log_json=False, api_key=SecretStr("")),
            engine=migrated_engine,
            sessions=db,
            cache=InMemoryCache(),
            http=http,
            providers=demo_registry,
            tools=ToolRegistry(all_tools()),
            llm_override=llm,
            clock=lambda: NOW,
        )
