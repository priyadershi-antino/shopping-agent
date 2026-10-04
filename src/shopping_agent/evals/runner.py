"""Run evaluation scenarios with the real agent, each in a clean database.

Data always comes from the FICTIONAL demo catalog (seeded with synthetic history), so results
are reproducible and independent of API keys. The model is whatever the settings route to.
The evaluation database is wiped before every scenario, so it must never be the main one.
"""

import asyncio
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from shopping_agent.agent.views import follow_up
from shopping_agent.config import Settings
from shopping_agent.db.base import Base
from shopping_agent.db.repositories.profile import ProfileRepository
from shopping_agent.db.session import create_engine, create_session_factory, ping
from shopping_agent.demo import seed_demo
from shopping_agent.domain.offer import PaymentMethod
from shopping_agent.errors import ConfigurationError, ProviderError
from shopping_agent.evals.dataset import Scenario
from shopping_agent.evals.metrics import ScenarioResult, Summary, score, summarize
from shopping_agent.llm.client import LLMClient
from shopping_agent.llm.factory import build_llm_client
from shopping_agent.prompts import ALL_PROMPTS, load_prompt
from shopping_agent.providers.cache import InMemoryCache
from shopping_agent.providers.factory import create_http_client
from shopping_agent.providers.fixtures import (
    FixturePageFetcher,
    FixtureProductProvider,
    FixtureWebProvider,
)
from shopping_agent.providers.product.base import (
    ProductDetails,
    ProductQuery,
    ProductRef,
    ProviderListing,
)
from shopping_agent.providers.quota import QuotaGuard
from shopping_agent.providers.registry import ProviderRegistry
from shopping_agent.providers.web.base import WebResult
from shopping_agent.runtime import Runtime
from shopping_agent.tools import ToolRegistry, all_tools
from shopping_agent.tools.base import utcnow

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]


class OutageProductProvider:
    """Simulates a product-data outage. Named like the demo provider so runs stay labelled demo."""

    name = "fixture"

    async def search(self, query: ProductQuery) -> list[ProviderListing]:
        raise ProviderError("demo", "simulated outage: product data unavailable")

    async def lookup(self, marketplace: Any, external_id: str) -> list[ProviderListing]:
        raise ProviderError("demo", "simulated outage: product data unavailable")

    async def get_details(self, ref: ProductRef) -> ProductDetails | None:
        raise ProviderError("demo", "simulated outage: product data unavailable")

    async def get_listings(self, ref: ProductRef) -> list[ProviderListing]:
        raise ProviderError("demo", "simulated outage: product data unavailable")


class OutageWebProvider:
    name = "fixture"

    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        include_domains: list[str] | None = None,
        recent_days: int | None = None,
    ) -> list[WebResult]:
        raise ProviderError("demo", "simulated outage: web search unavailable")


def eval_registry(clock: Clock, outage: str | None = None) -> ProviderRegistry:
    cache = InMemoryCache()
    no_products = outage in {"products", "all"}
    no_web = outage in {"web", "all"}
    return ProviderRegistry(
        products=[OutageProductProvider() if no_products else FixtureProductProvider(clock=clock)],
        web=[OutageWebProvider() if no_web else FixtureWebProvider()],
        fetcher=FixturePageFetcher(),
        cache=cache,
        quota=QuotaGuard(cache, {}),
    )


async def reset_database(sessions: async_sessionmaker[AsyncSession]) -> None:
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with sessions() as session, session.begin():
        await session.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


class EvalReport(BaseModel):
    created_at: datetime
    llm_model: str
    llm_fallbacks: str
    role_models: dict[str, str]
    prompt_versions: dict[str, int]
    dataset: str
    note: str = (
        "Data: fictional demo catalog with synthetic price history (shop demo-seed). "
        "Scores measure the agent's behaviour, not real-market accuracy."
    )
    summary: Summary
    results: list[ScenarioResult]


@dataclass
class EvalEnv:
    settings: Settings
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    http: httpx.AsyncClient
    llm: LLMClient
    clock: Clock


async def run_scenario(env: EvalEnv, scenario: Scenario) -> ScenarioResult:
    await reset_database(env.sessions)
    now = env.clock()
    setup = scenario.setup
    if setup.seed_history:
        await seed_demo(eval_registry(env.clock), env.sessions, now=now)
    async with env.sessions() as session, session.begin():
        repo = ProfileRepository(session)
        for card in setup.cards:
            await repo.add_payment_method(
                PaymentMethod(issuer=card.issuer, card_type=card.card_type)
            )
        for name in setup.memberships:
            await repo.add_membership(name)
    runtime = Runtime(
        settings=env.settings,
        engine=env.engine,
        sessions=env.sessions,
        cache=InMemoryCache(),
        http=env.http,
        providers=eval_registry(env.clock, setup.outage),
        tools=ToolRegistry(all_tools()),
        llm_override=env.llm,
        clock=env.clock,
    )
    agent = runtime.agent()
    profile = await runtime.load_profile()
    context, carry = None, list[str]()
    if setup.previous:
        previous = await agent.run(setup.previous, profile=profile)
        context, carry = follow_up(previous)
    started = time.monotonic()
    state = await agent.run(
        scenario.request, profile=profile, context=context, carry_listing_ids=carry
    )
    return score(scenario, state, time.monotonic() - started)


async def migrate(database_url: str) -> None:
    """Bring the evaluation database to the latest schema (URL passed via the environment)."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "alembic",
        "upgrade",
        "head",
        env={**os.environ, "DATABASE_URL": database_url},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        raise ConfigurationError(f"migrating the evaluation database failed: {out.decode()[-500:]}")


async def run_eval(
    scenarios: list[Scenario],
    *,
    settings: Settings,
    dataset_name: str,
    database_url: str | None = None,
    llm: LLMClient | None = None,
    clock: Clock = utcnow,
    run_migrations: bool = True,
    on_result: Callable[[ScenarioResult], Awaitable[None]] | None = None,
) -> EvalReport:
    url = (
        database_url
        or settings.eval_database_url.get_secret_value()
        or settings.test_database_url.get_secret_value()
    )
    if not url or url == settings.database_url.get_secret_value():
        raise ConfigurationError(
            "the evaluation wipes its database before each scenario; set EVAL_DATABASE_URL "
            "(or TEST_DATABASE_URL) to a separate database"
        )
    if run_migrations:
        await migrate(url)
    engine = create_engine(url)
    try:
        if not await ping(engine):
            raise ConfigurationError("the evaluation database is not reachable")
        cache = InMemoryCache()
        async with create_http_client(settings) as http:
            env = EvalEnv(
                settings=settings,
                engine=engine,
                sessions=create_session_factory(engine),
                http=http,
                llm=llm or build_llm_client(settings, cache, http),
                clock=clock,
            )
            results: list[ScenarioResult] = []
            for scenario in scenarios:
                started = time.monotonic()
                try:
                    result = await run_scenario(env, scenario)
                except Exception as exc:  # one broken scenario must not stop the evaluation
                    log.exception("scenario %s crashed", scenario.id)
                    result = ScenarioResult(
                        id=scenario.id,
                        tags=scenario.tags,
                        request=scenario.request,
                        status="error",
                        run_id=None,
                        error=f"{type(exc).__name__}: {exc}",
                        metrics={"completed": 0.0},
                        failures=[f"crashed: {exc}"],
                        latency_s=round(time.monotonic() - started, 1),
                    )
                results.append(result)
                if on_result is not None:
                    await on_result(result)
    finally:
        await engine.dispose()
    return EvalReport(
        created_at=clock(),
        llm_model=settings.llm_model,
        llm_fallbacks=settings.llm_fallbacks,
        role_models={
            role: value
            for role, value in {
                "extraction": settings.llm_model_extraction,
                "research": settings.llm_model_research,
                "synthesis": settings.llm_model_synthesis,
                "analysis": settings.llm_model_analysis,
            }.items()
            if value
        },
        prompt_versions={name: load_prompt(name).version for name in ALL_PROMPTS},
        dataset=dataset_name,
        summary=summarize(results),
        results=results,
    )
