"""Offline demo providers backed by a FICTIONAL catalog (see demo_catalog.json).

Everything they return is labelled "DEMO" so it can never be mistaken for real market data.
"""

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from functools import lru_cache
from importlib import resources
from typing import Any

from shopping_agent.domain.common import SourceType, Verification
from shopping_agent.domain.offer import Offer
from shopping_agent.domain.product import Marketplace
from shopping_agent.errors import UnsafeUrlError
from shopping_agent.providers.product.base import (
    ProductDetails,
    ProductQuery,
    ProductRef,
    ProviderListing,
)
from shopping_agent.providers.web.base import WebResult
from shopping_agent.providers.web.fetcher import FetchedPage
from shopping_agent.services.normalization import normalize_brand, normalize_model_number

DEMO_SOURCE = "DEMO data (fictional)"


@lru_cache
def load_catalog() -> dict[str, Any]:
    text = resources.files(__package__).joinpath("demo_catalog.json").read_text(encoding="utf-8")
    data: dict[str, Any] = json.loads(text)
    return data


def demo_offers() -> dict[str, Offer]:
    return {o["id"]: Offer.model_validate(o) for o in load_catalog()["offers"]}


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) > 1}


def _same_model(item: dict[str, Any], brand: str | None, model: str | None) -> bool:
    return (
        normalize_brand(item.get("brand")) == normalize_brand(brand)
        and normalize_model_number(item.get("model_number")) == normalize_model_number(model)
        and model is not None
    )


class FixtureProductProvider:
    name = "fixture"

    def __init__(self, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._clock = clock
        self._offers = demo_offers()

    def _listing(self, item: dict[str, Any]) -> ProviderListing:
        data = {k: v for k, v in item.items() if k not in {"offer_ids"}}
        return ProviderListing.model_validate(
            {
                **data,
                "provider": self.name,
                "offers": [self._offers[i] for i in item.get("offer_ids", []) if i in self._offers],
                "source_type": SourceType.MARKETPLACE_LISTING,
                "source_name": f"{DEMO_SOURCE}: {item['marketplace']}",
                "verification": Verification.SOURCE_REPORTED,
                "observed_at": self._clock(),
            }
        )

    def all_listings(self) -> list[ProviderListing]:
        return [self._listing(item) for item in load_catalog()["listings"]]

    async def search(self, query: ProductQuery) -> list[ProviderListing]:
        wanted = _tokens(query.query)
        scored: list[tuple[int, dict[str, Any]]] = []
        for item in load_catalog()["listings"]:
            if query.category and item.get("category") != query.category:
                continue
            price = float(item["price"])
            if query.max_price is not None and price > float(query.max_price):
                continue
            if query.min_price is not None and price < float(query.min_price):
                continue
            haystack = _tokens(f"{item['title']} {item.get('category', '')}")
            score = len(wanted & haystack)
            if score or (query.category and not wanted - {query.category}):
                scored.append((score, item))
        scored.sort(key=lambda s: -s[0])
        return [self._listing(item) for _, item in scored[: query.limit]]

    async def lookup(self, marketplace: Marketplace, external_id: str) -> list[ProviderListing]:
        return [
            self._listing(item)
            for item in load_catalog()["listings"]
            if item["marketplace"] == marketplace.value
            and str(item.get("external_id", "")).lower() == external_id.lower()
        ]

    async def get_details(self, ref: ProductRef) -> ProductDetails | None:
        for item in load_catalog()["details"]:
            if _same_model(item, ref.brand, ref.model_number) or (
                ref.name and _tokens(item["name"]) <= _tokens(ref.name) | {"5g"}
            ):
                return ProductDetails(
                    provider=self.name,
                    name=item["name"],
                    brand=item["brand"],
                    model_number=item["model_number"],
                    category=item["category"],
                    specs=item["specs"],
                    official_url=item.get("official_url"),
                    source_type=SourceType(item.get("source_type", "manufacturer")),
                    source_name=f"{DEMO_SOURCE}: {item['brand']} official specifications",
                    url=item.get("official_url"),
                    verification=Verification.VERIFIED,
                    retrieved_at=self._clock(),
                )
        return None

    async def get_listings(self, ref: ProductRef) -> list[ProviderListing]:
        return [
            self._listing(item)
            for item in load_catalog()["listings"]
            if _same_model(item, ref.brand, ref.model_number)
        ]


class FixtureWebProvider:
    name = "fixture"

    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        include_domains: list[str] | None = None,
        recent_days: int | None = None,
    ) -> list[WebResult]:
        q = query.lower()
        scored: list[tuple[int, dict[str, Any]]] = []
        for item in load_catalog()["web"]:
            hits = sum(1 for word in item["match"] if word in q)
            if hits >= 2 or (hits == 1 and len(item["match"]) == 1):
                scored.append((hits, item))
        scored.sort(key=lambda s: -s[0])
        return [
            WebResult(
                title=item["title"],
                url=item["url"],
                snippet=item["snippet"],
                published_at=datetime.fromisoformat(item["published_at"]).replace(tzinfo=UTC),
                provider=self.name,
            )
            for _, item in scored[:max_results]
        ]


class FixturePageFetcher:
    """Serves the canned demo pages; refuses everything else (no network in demo mode)."""

    allowlist: frozenset[str] = frozenset({"demo.invalid"})

    async def fetch(self, url: str) -> FetchedPage:
        page = load_catalog()["pages"].get(url)
        if page is None:
            raise UnsafeUrlError("demo mode: page not available offline")
        return FetchedPage(
            url=url,
            final_url=url,
            title=page["title"],
            text=page["text"],
            fetched_at=datetime.now(UTC),
        )
