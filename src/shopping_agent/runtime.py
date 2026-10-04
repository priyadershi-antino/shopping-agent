"""Shared runtime for the API, CLI, web UI and worker: database, cache, providers, tools, LLM."""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from shopping_agent.agent.orchestrator import RunBudget, ShoppingAgent
from shopping_agent.agent.store import SqlRunStore
from shopping_agent.config import Settings, get_settings
from shopping_agent.db.session import create_engine, create_session_factory
from shopping_agent.domain.offer import UserProfile
from shopping_agent.errors import ToolInputError
from shopping_agent.llm.client import LLMClient
from shopping_agent.llm.factory import build_llm_client
from shopping_agent.providers.cache import Cache, InMemoryCache, RedisCache
from shopping_agent.providers.factory import build_registry, create_http_client
from shopping_agent.providers.registry import ProviderRegistry
from shopping_agent.tools import ToolContext, ToolRegistry, ToolResult, all_tools
from shopping_agent.tools.base import utcnow

log = logging.getLogger(__name__)


@dataclass
class Runtime:
    settings: Settings
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    cache: Cache
    http: httpx.AsyncClient
    providers: ProviderRegistry
    tools: ToolRegistry
    llm_override: LLMClient | None = None
    clock: Callable[[], datetime] = utcnow
    _llm: LLMClient | None = field(default=None, init=False, repr=False)

    def llm(self) -> LLMClient:
        """Built on first use so commands that don't need a model work without an LLM key."""
        if self.llm_override is not None:
            return self.llm_override
        if self._llm is None:
            self._llm = build_llm_client(self.settings, self.cache, self.http)
        return self._llm

    async def load_profile(self) -> UserProfile:
        from shopping_agent.db.repositories.profile import ProfileRepository

        async with self.sessions() as session:
            return await ProfileRepository(session).load()

    def tool_context(self, profile: UserProfile | None = None) -> ToolContext:
        return ToolContext(
            providers=self.providers,
            sessions=self.sessions,
            profile=profile or UserProfile(),
            clock=self.clock,
            fetch_max_chars=self.settings.fetch_max_chars,
        )

    async def call_tool(
        self,
        name: str,
        args: dict[str, Any],
        *,
        profile: UserProfile | None = None,
        allow_side_effects: bool = False,
    ) -> ToolResult[Any]:
        """Run one tool directly (interfaces). Unknown references raise ToolInputError."""
        result, record = await self.tools.execute(
            name, args, self.tool_context(profile), allow_side_effects=allow_side_effects
        )
        if record.status in {"invalid", "unknown_tool", "denied"} or (
            record.status == "error" and result.data is None
        ):
            raise ToolInputError(record.error or "tool failed")
        return result

    async def tool_data[M: BaseModel](
        self,
        name: str,
        args: dict[str, Any],
        model: type[M],
        *,
        profile: UserProfile | None = None,
        allow_side_effects: bool = False,
    ) -> M:
        """Run a tool and return its typed output (raises ToolInputError when it has none)."""
        result = await self.call_tool(
            name, args, profile=profile, allow_side_effects=allow_side_effects
        )
        if not isinstance(result.data, model):
            raise ToolInputError("; ".join(result.errors) or f"{name} returned no data")
        return result.data

    def agent(self) -> ShoppingAgent:
        s = self.settings
        return ShoppingAgent(
            llm=self.llm(),
            tools=self.tools,
            tool_context=self.tool_context(),
            budget=RunBudget(
                max_iterations=s.agent_max_iterations,
                max_tool_calls=s.agent_max_tool_calls,
                max_seconds=s.agent_max_seconds,
            ),
            store=SqlRunStore(self.sessions),
            clock=self.clock,
        )


async def _connect_cache(settings: Settings) -> Cache:
    redis = RedisCache.from_url(settings.redis_url, settings.cache_key_prefix)
    if await redis.ping():
        return redis
    await redis.close()
    log.warning("Redis is not reachable; using an in-process cache (quotas reset on restart)")
    return InMemoryCache()


@asynccontextmanager
async def open_runtime(
    settings: Settings | None = None,
    *,
    cache: Cache | None = None,
    llm: LLMClient | None = None,
    database_url: str | None = None,
) -> AsyncIterator[Runtime]:
    settings = settings or get_settings()
    engine = create_engine(database_url or settings.database_url.get_secret_value())
    cache = cache or await _connect_cache(settings)
    http = create_http_client(settings)
    try:
        yield Runtime(
            settings=settings,
            engine=engine,
            sessions=create_session_factory(engine),
            cache=cache,
            http=http,
            providers=build_registry(settings, cache, http),
            tools=ToolRegistry(all_tools()),
            llm_override=llm,
        )
    finally:
        await http.aclose()
        await cache.close()
        await engine.dispose()
