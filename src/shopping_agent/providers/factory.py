"""Build the provider registry from settings."""

import httpx

from shopping_agent.config import Settings
from shopping_agent.errors import ConfigurationError
from shopping_agent.providers.cache import Cache
from shopping_agent.providers.fixtures import (
    FixturePageFetcher,
    FixtureProductProvider,
    FixtureWebProvider,
)
from shopping_agent.providers.http import ProviderHttp
from shopping_agent.providers.product.base import ProductProvider
from shopping_agent.providers.product.serpapi import SerpApiProductProvider
from shopping_agent.providers.quota import QuotaGuard
from shopping_agent.providers.registry import PageFetcher, ProviderRegistry
from shopping_agent.providers.web.base import WebSearchProvider
from shopping_agent.providers.web.fetcher import SafeFetcher
from shopping_agent.providers.web.tavily import TavilySearchProvider
from shopping_agent.security.url_guard import DEFAULT_FETCH_ALLOWLIST

USER_AGENT = "shopping-agent/0.1 (personal shopping research)"


def create_http_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(settings.provider_timeout_s, connect=10.0),
        headers={"User-Agent": USER_AGENT},
    )


def _require(secret: str, name: str) -> str:
    if not secret:
        raise ConfigurationError(f"{name} is not set")
    return secret


def build_registry(settings: Settings, cache: Cache, client: httpx.AsyncClient) -> ProviderRegistry:
    products: list[ProductProvider] = []
    for name in settings.product_provider_names():
        if name == "serpapi":
            key = _require(settings.serpapi_api_key.get_secret_value(), "SERPAPI_API_KEY")
            products.append(SerpApiProductProvider(ProviderHttp("serpapi", client), key))
        elif name == "fixture":
            products.append(FixtureProductProvider())
        else:
            raise ConfigurationError(f"unknown product provider '{name}'")

    web: list[WebSearchProvider] = []
    for name in settings.web_provider_names():
        if name == "tavily":
            key = _require(settings.tavily_api_key.get_secret_value(), "TAVILY_API_KEY")
            web.append(TavilySearchProvider(ProviderHttp("tavily", client), key))
        elif name == "fixture":
            web.append(FixtureWebProvider())
        else:
            raise ConfigurationError(f"unknown web provider '{name}'")

    fetcher: PageFetcher
    if web and all(p.name == "fixture" for p in web):
        fetcher = FixturePageFetcher()
    else:
        allowlist = DEFAULT_FETCH_ALLOWLIST | frozenset(settings.fetch_extra_domain_list())
        fetcher = SafeFetcher(client, allowlist, max_bytes=settings.fetch_max_bytes)

    quota = QuotaGuard(
        cache, {"serpapi": settings.serpapi_daily_quota, "tavily": settings.tavily_daily_quota}
    )
    return ProviderRegistry(products=products, web=web, fetcher=fetcher, cache=cache, quota=quota)
