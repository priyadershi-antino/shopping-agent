"""Wire the agent from settings (used by the CLI, API and evaluation runner)."""

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shopping_agent.agent.orchestrator import RunBudget, ShoppingAgent
from shopping_agent.agent.store import SqlRunStore
from shopping_agent.config import Settings
from shopping_agent.llm.factory import build_llm_client
from shopping_agent.providers.cache import Cache
from shopping_agent.providers.factory import build_registry
from shopping_agent.tools import ToolContext, ToolRegistry, default_tools


def build_agent(
    settings: Settings,
    *,
    sessions: async_sessionmaker[AsyncSession],
    cache: Cache,
    http_client: httpx.AsyncClient,
    persist_runs: bool = True,
) -> ShoppingAgent:
    providers = build_registry(settings, cache, http_client)
    return ShoppingAgent(
        llm=build_llm_client(settings, cache, http_client),
        tools=ToolRegistry(default_tools()),
        tool_context=ToolContext(
            providers=providers,
            sessions=sessions,
            fetch_max_chars=settings.fetch_max_chars,
            platforms=settings.default_marketplaces(),
        ),
        budget=RunBudget(
            max_iterations=settings.agent_max_iterations,
            max_tool_calls=settings.agent_max_tool_calls,
            max_seconds=settings.agent_max_seconds,
        ),
        store=SqlRunStore(sessions) if persist_runs else None,
    )
