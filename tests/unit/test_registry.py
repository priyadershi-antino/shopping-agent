"""Provider registry, quota, freshness, fixture providers and normalization helpers."""

from datetime import timedelta

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from shopping_agent.config import Settings
from shopping_agent.domain.product import Marketplace
from shopping_agent.errors import ConfigurationError, ProviderError, QuotaExhaustedError
from shopping_agent.providers.cache import InMemoryCache
from shopping_agent.providers.factory import build_registry, create_http_client
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
from shopping_agent.services.freshness import DataKind, is_stale, ttl_seconds
from shopping_agent.services.normalization import detect_brand, normalize_specs
from tests.factories import NOW


class CountingProvider(FixtureProductProvider):
    def __init__(self, name: str = "counting", fail: bool = False) -> None:
        super().__init__(clock=lambda: NOW)
        self.name = name
        self.calls = 0
        self.fail = fail

    async def search(self, query: ProductQuery) -> list[ProviderListing]:
        self.calls += 1
        if self.fail:
            raise ProviderError(self.name, "server error 503", transient=True)
        return await super().search(query)


class BrokenCache(InMemoryCache):
    async def get(self, key: str) -> str | None:
        raise RedisConnectionError("down")

    async def set(self, key: str, value: str, ttl_s: int) -> None:
        raise RedisConnectionError("down")


def registry(
    *providers: FixtureProductProvider,
    cache: InMemoryCache | None = None,
    limits: dict[str, int] | None = None,
) -> ProviderRegistry:
    cache = cache or InMemoryCache()
    return ProviderRegistry(
        products=list(providers),
        web=[FixtureWebProvider()],
        fetcher=FixturePageFetcher(),
        cache=cache,
        quota=QuotaGuard(cache, limits or {}, clock=lambda: NOW),
    )


QUERY = ProductQuery(query="gaming laptop", category="laptop")


async def test_results_are_cached() -> None:
    provider = CountingProvider()
    reg = registry(provider)
    first = await reg.search_products(QUERY)
    second = await reg.search_products(QUERY)
    assert first.value and not first.cached
    assert second.cached
    assert [x.source_key for x in second.value] == [x.source_key for x in first.value]
    assert provider.calls == 1


async def test_failing_provider_gives_partial_result() -> None:
    good, bad = CountingProvider("good"), CountingProvider("bad", fail=True)
    result = await registry(bad, good).search_products(QUERY)
    assert result.value
    assert result.partial
    assert result.providers == ["good"]
    assert "bad: server error 503" in result.errors[0]


async def test_quota_exhaustion_stops_calls() -> None:
    provider = CountingProvider("limited")
    reg = registry(provider, limits={"limited": 1})
    await reg.search_products(QUERY)
    result = await reg.search_products(ProductQuery(query="phone"))
    assert provider.calls == 1
    assert "quota" in result.errors[0]


async def test_cache_outage_does_not_break_calls() -> None:
    provider = CountingProvider()
    result = await registry(provider, cache=BrokenCache()).search_products(QUERY)
    assert result.value
    assert not result.errors


async def test_lookup_details_and_web_fallbacks() -> None:
    reg = registry(FixtureProductProvider(clock=lambda: NOW))
    found = await reg.lookup(Marketplace.AMAZON, "b0demo0001")
    assert found.value[0].external_id == "B0DEMO0001"
    details = await reg.product_details(ProductRef(brand="ACME", model_number="nb15 g4"))
    assert isinstance(details.value, ProductDetails)
    assert details.value.specs["ram_gb"] == 16
    web = await reg.web_search("acme nimbus 15 review")
    assert web.value
    assert reg.demo_mode


async def test_fetch_page_demo_mode() -> None:
    reg = registry(CountingProvider())
    ok = await reg.fetch_page("https://acme.demo.invalid/nimbus-15")
    assert ok.value is not None
    missing = await reg.fetch_page("https://www.gsmarena.com/x")
    assert missing.value is None
    assert missing.errors


async def test_quota_guard_resets_daily() -> None:
    cache = InMemoryCache()
    now = [NOW]
    guard = QuotaGuard(cache, {"p": 2}, clock=lambda: now[0])
    await guard.acquire("p")
    await guard.acquire("p")
    for _ in range(3):  # refused requests are not spent, so the counter stays at the limit
        with pytest.raises(QuotaExhaustedError):
            await guard.acquire("p")
    assert await guard.used("p") == 2
    now[0] = NOW + timedelta(days=1)
    await guard.acquire("p")
    assert await guard.used("p") == 1
    await guard.acquire("unlimited")  # providers without a limit are not counted


def test_freshness_policy() -> None:
    assert ttl_seconds(DataKind.PRICE) == 1800
    assert ttl_seconds(DataKind.PRICE, sale_active=True) == 600
    assert is_stale(DataKind.PRICE, NOW - timedelta(minutes=31), NOW)
    assert not is_stale(DataKind.SPEC, NOW - timedelta(days=6), NOW)


def test_factory_auto_uses_demo_without_keys() -> None:
    settings = Settings(serpapi_api_key="", tavily_api_key="")
    assert settings.product_provider_names() == ["fixture"]
    keyed = Settings(serpapi_api_key="k", tavily_api_key="t")
    assert keyed.product_provider_names() == ["serpapi"]
    assert keyed.web_provider_names() == ["tavily"]


async def test_real_products_without_tavily_use_no_demo_web_data() -> None:
    settings = Settings(serpapi_api_key="k", tavily_api_key="")
    assert settings.web_provider_names() == []
    async with create_http_client(settings) as client:
        registry = build_registry(settings, InMemoryCache(), client)
    assert not registry.demo_mode
    assert not isinstance(registry.fetcher, FixturePageFetcher)
    found = await registry.web_search("Flipkart Big Billion Days sale dates")
    assert found.value == []
    assert any("TAVILY_API_KEY" in e for e in found.errors)


async def test_factory_requires_keys_for_real_providers() -> None:
    settings = Settings(product_providers="serpapi", serpapi_api_key="")
    async with create_http_client(settings) as client:
        with pytest.raises(ConfigurationError, match="SERPAPI_API_KEY"):
            build_registry(settings, InMemoryCache(), client)
        demo = build_registry(
            Settings(product_providers="fixture", web_providers="fixture"), InMemoryCache(), client
        )
        assert demo.demo_mode


def test_normalize_specs_and_brand() -> None:
    specs = normalize_specs({"RAM": "16 GB", "Hard Disk Size": "1 TB", "Model Name": "Victus"})
    assert specs == {"ram_gb": 16, "storage_gb": 1024, "model_name": "Victus"}
    assert detect_brand("New HP Victus 15") == "hp"
    assert detect_brand("Hewlett-Packard Pavilion") == "hp"
    assert detect_brand("Unknown thing") is None
