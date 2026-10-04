"""Provider registry: ordered fallback, merging, caching with freshness TTLs, quota budgets.

Provider failures never fail the whole call — they are collected in `errors` and the result
is marked partial, so tools can continue with what they have.
"""

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from pydantic import BaseModel, Field, TypeAdapter
from redis.exceptions import RedisError

from shopping_agent.domain.product import Marketplace
from shopping_agent.errors import ShoppingAgentError
from shopping_agent.providers.cache import Cache
from shopping_agent.providers.product.base import (
    ProductDetails,
    ProductProvider,
    ProductQuery,
    ProductRef,
    ProviderListing,
)
from shopping_agent.providers.quota import QuotaGuard
from shopping_agent.providers.web.base import WebResult, WebSearchProvider
from shopping_agent.providers.web.fetcher import FetchedPage
from shopping_agent.services.freshness import DataKind, ttl_seconds

log = logging.getLogger(__name__)

_LISTINGS = TypeAdapter(list[ProviderListing])
_DETAILS: TypeAdapter[ProductDetails | None] = TypeAdapter(ProductDetails | None)
_WEB = TypeAdapter(list[WebResult])
_PAGE = TypeAdapter(FetchedPage)


class PageFetcher(Protocol):
    @property
    def allowlist(self) -> frozenset[str]: ...

    async def fetch(self, url: str) -> FetchedPage: ...


class Gathered[T](BaseModel):
    value: T
    providers: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    cached: bool = False

    @property
    def partial(self) -> bool:
        return bool(self.errors)


def _cache_key(provider: str, op: str, args: Any) -> str:
    digest = hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()
    return f"prov:{provider}:{op}:{digest[:32]}"


class ProviderRegistry:
    def __init__(
        self,
        *,
        products: list[ProductProvider],
        web: list[WebSearchProvider],
        fetcher: PageFetcher,
        cache: Cache,
        quota: QuotaGuard,
    ) -> None:
        self.products = products
        self.web = web
        self.fetcher = fetcher
        self._cache = cache
        self._quota = quota

    @property
    def demo_mode(self) -> bool:
        return all(p.name == "fixture" for p in self.products) and all(
            w.name == "fixture" for w in self.web
        )

    async def _cache_get(self, key: str) -> str | None:
        try:
            return await self._cache.get(key)
        except (RedisError, OSError):
            log.warning("cache unavailable; continuing without it")
            return None

    async def _cache_set(self, key: str, value: str, ttl: int) -> None:
        try:
            await self._cache.set(key, value, ttl)
        except (RedisError, OSError):
            log.warning("cache unavailable; result not cached")

    async def _call[T](
        self,
        provider: str,
        op: str,
        args: Any,
        kind: DataKind,
        sale_active: bool,
        adapter: TypeAdapter[T],
        loader: Callable[[], Awaitable[T]],
    ) -> tuple[T, bool]:
        key = _cache_key(provider, op, args)
        if (hit := await self._cache_get(key)) is not None:
            return adapter.validate_json(hit), True
        await self._quota.acquire(provider)
        value = await loader()
        await self._cache_set(
            key, adapter.dump_json(value).decode(), ttl_seconds(kind, sale_active=sale_active)
        )
        return value, False

    async def _gather_listings(
        self,
        op: str,
        args: Any,
        sale_active: bool,
        call: Callable[[ProductProvider], Awaitable[list[ProviderListing]]],
    ) -> Gathered[list[ProviderListing]]:
        async def one(p: ProductProvider) -> tuple[list[ProviderListing], bool]:
            return await self._call(
                p.name, op, args, DataKind.PRICE, sale_active, _LISTINGS, lambda: call(p)
            )

        results = await asyncio.gather(*(one(p) for p in self.products), return_exceptions=True)
        out: Gathered[list[ProviderListing]] = Gathered(value=[])
        seen: set[str] = set()
        all_cached = True
        for provider, result in zip(self.products, results, strict=True):
            if isinstance(result, BaseException):
                if not isinstance(result, ShoppingAgentError | TimeoutError):
                    log.exception("provider %s failed", provider.name, exc_info=result)
                out.errors.append(f"{provider.name}: {_describe(result)}")
                continue
            listings, cached = result
            all_cached &= cached
            out.providers.append(provider.name)
            for listing in listings:
                if listing.source_key not in seen:
                    seen.add(listing.source_key)
                    out.value.append(listing)
        out.cached = bool(out.providers) and all_cached
        return out

    async def search_products(
        self, query: ProductQuery, *, sale_active: bool = False
    ) -> Gathered[list[ProviderListing]]:
        return await self._gather_listings(
            "search", query.model_dump(mode="json"), sale_active, lambda p: p.search(query)
        )

    async def product_listings(
        self, ref: ProductRef, *, sale_active: bool = False
    ) -> Gathered[list[ProviderListing]]:
        return await self._gather_listings(
            "listings", ref.model_dump(mode="json"), sale_active, lambda p: p.get_listings(ref)
        )

    async def lookup(
        self, marketplace: Marketplace, external_id: str, *, sale_active: bool = False
    ) -> Gathered[list[ProviderListing]]:
        out: Gathered[list[ProviderListing]] = Gathered(value=[])
        for p in self.products:
            try:
                value, cached = await self._call(
                    p.name,
                    "lookup",
                    [marketplace.value, external_id],
                    DataKind.PRICE,
                    sale_active,
                    _LISTINGS,
                    lambda p=p: p.lookup(marketplace, external_id),  # type: ignore[misc]
                )
            except (ShoppingAgentError, TimeoutError) as exc:
                out.errors.append(f"{p.name}: {_describe(exc)}")
                continue
            out.providers.append(p.name)
            if value:
                out.value, out.cached = value, cached
                break
        return out

    async def product_details(
        self, ref: ProductRef, *, sale_active: bool = False
    ) -> Gathered[ProductDetails | None]:
        out: Gathered[ProductDetails | None] = Gathered(value=None)
        for p in self.products:
            try:
                value, cached = await self._call(
                    p.name,
                    "details",
                    ref.model_dump(mode="json"),
                    DataKind.SPEC,
                    sale_active,
                    _DETAILS,
                    lambda p=p: p.get_details(ref),  # type: ignore[misc]
                )
            except (ShoppingAgentError, TimeoutError) as exc:
                out.errors.append(f"{p.name}: {_describe(exc)}")
                continue
            out.providers.append(p.name)
            if value is not None:
                out.value, out.cached = value, cached
                break
        return out

    async def web_search(
        self,
        query: str,
        *,
        max_results: int = 5,
        include_domains: list[str] | None = None,
        recent_days: int | None = None,
        sale_active: bool = False,
    ) -> Gathered[list[WebResult]]:
        out: Gathered[list[WebResult]] = Gathered(value=[])
        if not self.web:
            out.errors.append("web search is not configured (set TAVILY_API_KEY)")
            return out
        args = [query, max_results, sorted(include_domains or []), recent_days]
        for p in self.web:
            try:
                value, cached = await self._call(
                    p.name,
                    "web",
                    args,
                    DataKind.SEARCH,
                    sale_active,
                    _WEB,
                    lambda p=p: p.search(  # type: ignore[misc]
                        query,
                        max_results=max_results,
                        include_domains=include_domains,
                        recent_days=recent_days,
                    ),
                )
            except (ShoppingAgentError, TimeoutError) as exc:
                out.errors.append(f"{p.name}: {_describe(exc)}")
                continue
            out.providers.append(p.name)
            out.value, out.cached = value, cached
            if value:
                break
        return out

    async def fetch_page(self, url: str) -> Gathered[FetchedPage | None]:
        key = _cache_key("fetch", "page", url)
        if (hit := await self._cache_get(key)) is not None:
            return Gathered(value=_PAGE.validate_json(hit), providers=["fetch"], cached=True)
        try:
            page = await self.fetcher.fetch(url)
        except (ShoppingAgentError, TimeoutError) as exc:
            return Gathered(value=None, errors=[_describe(exc)])
        await self._cache_set(key, _PAGE.dump_json(page).decode(), ttl_seconds(DataKind.PAGE))
        return Gathered(value=page, providers=["fetch"])


def _describe(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "timed out"
    if isinstance(exc, ShoppingAgentError):
        return str(exc)
    return f"unexpected error ({type(exc).__name__})"
