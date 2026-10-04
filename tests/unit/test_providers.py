"""Provider parsing and HTTP behaviour against mocked APIs (no network)."""

import json
from collections.abc import AsyncIterator
from decimal import Decimal

import httpx
import pytest
import respx

from shopping_agent.domain.common import SourceType
from shopping_agent.domain.product import Availability, Condition, Marketplace
from shopping_agent.errors import ProviderError, RateLimitedError, UnsafeUrlError
from shopping_agent.providers.http import ProviderHttp
from shopping_agent.providers.product.base import ProductQuery, ProductRef
from shopping_agent.providers.product.marketplaces import (
    availability_from_text,
    marketplace_from_store,
)
from shopping_agent.providers.product.serpapi import SERPAPI_URL, SerpApiProductProvider
from shopping_agent.providers.web.fetcher import SafeFetcher
from shopping_agent.providers.web.tavily import TAVILY_SEARCH_URL, TavilySearchProvider
from tests.factories import NOW


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as c:
        yield c


def http(client: httpx.AsyncClient, name: str = "serpapi") -> ProviderHttp:
    return ProviderHttp(name, client, attempts=2, max_wait_s=0)


def serpapi(client: httpx.AsyncClient) -> SerpApiProductProvider:
    return SerpApiProductProvider(http(client), "secret-key", clock=lambda: NOW)


SHOPPING = {
    "shopping_results": [
        {
            "title": "HP Victus 15 Gaming Laptop 16GB RAM 512GB SSD RTX 4050",
            "product_id": "123",
            "source": "Amazon.in",
            "price": "₹74,990.00",
            "extracted_price": 74990.0,
            "extracted_old_price": 89990.0,
            "rating": 4.3,
            "reviews": 1520,
            "delivery": "Free delivery",
            "product_link": "https://www.google.com/shopping/product/123",
            "immersive_product_page_token": "tok123",
        },
        {"title": "US import", "source": "eBay", "price": "$799.00", "extracted_price": 799.0},
        {"title": "No price item", "source": "Croma"},
    ],
    "categorized_shopping_results": [
        {
            "shopping_results": [
                {
                    "title": "(Renewed) HP Victus 15",
                    "source": "Flipkart",
                    "price": "₹58,990",
                    "extracted_price": 58990,
                }
            ]
        }
    ],
}


@respx.mock
async def test_serpapi_search_parsing(client: httpx.AsyncClient) -> None:
    route = respx.get(SERPAPI_URL).mock(return_value=httpx.Response(200, json=SHOPPING))
    listings = await serpapi(client).search(
        ProductQuery(query="gaming laptop", category="laptop", max_price=Decimal("80000"))
    )
    params = route.calls.last.request.url.params
    assert params["engine"] == "google_shopping"
    assert params["gl"] == "in"
    assert params["max_price"] == "80000"
    assert len(listings) == 2  # foreign currency and price-less items dropped
    first, renewed = listings
    assert first.marketplace is Marketplace.AMAZON
    assert first.price == Decimal("74990.0")
    assert first.mrp == Decimal("89990.0")
    assert first.brand == "hp"
    assert first.provider_token == "tok123"
    assert first.source_type is SourceType.SEARCH_RESULT
    assert first.source_key == "serpapi:gs:123:amazon-in"
    assert renewed.marketplace is Marketplace.FLIPKART
    assert renewed.condition is Condition.RENEWED


@respx.mock
async def test_serpapi_immersive_stores_and_details(client: httpx.AsyncClient) -> None:
    respx.get(SERPAPI_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "product_results": {
                    "title": "HP Victus 15",
                    "brand": "HP",
                    "stores": [
                        {
                            "name": "Flipkart",
                            "link": "https://www.flipkart.com/x",
                            "price": "₹73,490",
                            "extracted_price": 73490,
                            "extracted_original_price": 99990,
                            "details_and_offers": ["In stock online", "10% off with HDFC cards"],
                        },
                        {"name": "Croma", "price": "₹76,990", "extracted_price": 76990},
                    ],
                    "about_the_product": {
                        "features": [
                            {"title": "RAM", "value": "16 GB"},
                            {"title": "Model Number", "value": "15-fa1099TX"},
                        ]
                    },
                }
            },
        )
    )
    provider = serpapi(client)
    ref = ProductRef(name="HP Victus 15", provider_token="tok")
    stores = await provider.get_listings(ref)
    assert [s.marketplace for s in stores] == [Marketplace.FLIPKART, Marketplace.CROMA]
    assert stores[0].availability is Availability.IN_STOCK
    assert stores[0].offer_texts == ["10% off with HDFC cards"]
    details = await provider.get_details(ref)
    assert details is not None
    assert details.specs["RAM"] == "16 GB"
    assert details.model_number == "15-fa1099TX"
    assert respx.calls.call_count == 1  # immersive page memoised across both calls


@respx.mock
async def test_serpapi_amazon_lookup(client: httpx.AsyncClient) -> None:
    respx.get(SERPAPI_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "product_results": {
                    "title": "HP Victus 15",
                    "brand": "Visit the HP Store",
                    "extracted_price": 74990,
                    "extracted_old_price": 99990,
                    "stock": "In stock",
                    "product_details": {"Item model number": "15-fa1099TX", "RAM": "16 GB"},
                }
            },
        )
    )
    (listing,) = await serpapi(client).lookup(Marketplace.AMAZON, "B0CXYZ1234")
    assert listing.source_key == "amazon:B0CXYZ1234"
    assert listing.brand == "HP"
    assert listing.model_number == "15-fa1099TX"
    assert listing.url == "https://www.amazon.in/dp/B0CXYZ1234"
    assert await serpapi(client).lookup(Marketplace.FLIPKART, "X") == []


@respx.mock
async def test_serpapi_error_payload_means_no_results(client: httpx.AsyncClient) -> None:
    respx.get(SERPAPI_URL).mock(
        return_value=httpx.Response(200, json={"error": "Google hasn't returned any results"})
    )
    assert await serpapi(client).search(ProductQuery(query="xyz")) == []


@respx.mock
async def test_auth_failure_is_not_retried_and_hides_key(client: httpx.AsyncClient) -> None:
    route = respx.get(SERPAPI_URL).mock(return_value=httpx.Response(401))
    with pytest.raises(ProviderError) as info:
        await serpapi(client).search(ProductQuery(query="x"))
    assert route.call_count == 1
    assert "secret-key" not in str(info.value)
    assert not info.value.transient


@respx.mock
async def test_rate_limit_and_server_errors_are_retried(client: httpx.AsyncClient) -> None:
    route = respx.get(SERPAPI_URL).mock(return_value=httpx.Response(429))
    with pytest.raises(RateLimitedError):
        await serpapi(client).search(ProductQuery(query="x"))
    assert route.call_count == 2

    respx.get(SERPAPI_URL).mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json={"shopping_results": []})]
    )
    assert await serpapi(client).search(ProductQuery(query="x")) == []


@respx.mock
async def test_timeouts_become_transient_provider_errors(client: httpx.AsyncClient) -> None:
    respx.get(SERPAPI_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(ProviderError, match="timed out"):
        await serpapi(client).search(ProductQuery(query="x"))


@respx.mock
async def test_tavily_search(client: httpx.AsyncClient) -> None:
    route = respx.post(TAVILY_SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Review",
                        "url": "https://www.notebookcheck.net/x.html",
                        "content": "Good laptop",
                        "score": 0.8,
                        "published_date": "2026-06-01",
                    },
                    {"title": "no url"},
                ]
            },
        )
    )
    provider = TavilySearchProvider(http(client, "tavily"), "tvly-key")
    results = await provider.search(
        "victus review", max_results=3, include_domains=["notebookcheck.net"], recent_days=30
    )
    request = route.calls.last.request
    body = json.loads(request.content)
    assert request.headers["Authorization"] == "Bearer tvly-key"
    assert body["include_domains"] == ["notebookcheck.net"]
    assert body["time_range"] == "month"
    assert body["max_results"] == 3
    assert len(results) == 1
    assert results[0].published_at is not None


def test_marketplace_and_availability_mapping() -> None:
    assert marketplace_from_store("Amazon.in - Seller") is Marketplace.AMAZON
    assert marketplace_from_store("Reliance Digital") is Marketplace.RELIANCE_DIGITAL
    assert marketplace_from_store("Some Shop") is Marketplace.OTHER
    assert availability_from_text(["Out of stock"]) is Availability.OUT_OF_STOCK
    assert availability_from_text("Only 2 left in stock") is Availability.LIMITED
    assert availability_from_text(None) is Availability.UNKNOWN


# --- SafeFetcher ---


async def public_resolver(host: str) -> list[str]:
    return ["93.184.216.34"]


async def private_resolver(host: str) -> list[str]:
    return ["192.168.1.10"]


def fetcher(client: httpx.AsyncClient, **kwargs: object) -> SafeFetcher:
    return SafeFetcher(
        client,
        {"gsmarena.com", "rtings.com"},
        resolver=kwargs.get("resolver", public_resolver),  # type: ignore[arg-type]
        max_bytes=kwargs.get("max_bytes", 10_000),  # type: ignore[arg-type]
    )


@respx.mock
async def test_fetch_html_page(client: httpx.AsyncClient) -> None:
    respx.get("https://www.gsmarena.com/a").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text="<title>T</title><p>Hello</p>",
        )
    )
    page = await fetcher(client).fetch("https://www.gsmarena.com/a")
    assert page.title == "T"
    assert page.text == "Hello"


@respx.mock
async def test_fetch_follows_allowed_redirect_and_blocks_others(client: httpx.AsyncClient) -> None:
    respx.get("https://www.gsmarena.com/old").mock(
        return_value=httpx.Response(301, headers={"location": "/new"})
    )
    respx.get("https://www.gsmarena.com/new").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/plain"}, text="moved")
    )
    page = await fetcher(client).fetch("https://www.gsmarena.com/old")
    assert page.final_url == "https://www.gsmarena.com/new"
    assert page.text == "moved"

    respx.get("https://rtings.com/r").mock(
        return_value=httpx.Response(302, headers={"location": "https://169.254.169.254/latest"})
    )
    with pytest.raises(UnsafeUrlError):
        await fetcher(client).fetch("https://rtings.com/r")


async def test_fetch_blocks_private_resolution(client: httpx.AsyncClient) -> None:
    with pytest.raises(UnsafeUrlError, match="non-public"):
        await fetcher(client, resolver=private_resolver).fetch("https://www.gsmarena.com/a")


@respx.mock
async def test_fetch_rejects_binary_and_oversized(client: httpx.AsyncClient) -> None:
    respx.get("https://www.gsmarena.com/img").mock(
        return_value=httpx.Response(200, headers={"content-type": "image/png"}, content=b"x")
    )
    with pytest.raises(UnsafeUrlError, match="content type"):
        await fetcher(client).fetch("https://www.gsmarena.com/img")

    respx.get("https://www.gsmarena.com/big").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/html"}, text="x" * 500)
    )
    with pytest.raises(UnsafeUrlError, match="too large"):
        await fetcher(client, max_bytes=100).fetch("https://www.gsmarena.com/big")
