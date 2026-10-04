"""SerpApi provider: Google Shopping (India) search, immersive product pages (per-store prices
and features) and Amazon product lookup by ASIN.

Prices from Google Shopping are aggregator observations, so they are labelled
`search_result` / `source_reported`. All parsing is defensive: missing fields stay unknown.
"""

import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from shopping_agent.domain.common import SourceType, Verification
from shopping_agent.domain.product import Marketplace
from shopping_agent.errors import ProviderError, QuotaExhaustedError
from shopping_agent.providers.http import ProviderHttp
from shopping_agent.providers.product.base import (
    ProductDetails,
    ProductQuery,
    ProductRef,
    ProviderListing,
    SpecValue,
)
from shopping_agent.providers.product.marketplaces import (
    availability_from_text,
    marketplace_from_store,
    slug,
)
from shopping_agent.services.normalization import detect_brand, detect_condition

log = logging.getLogger(__name__)

SERPAPI_URL = "https://serpapi.com/search"
_FOREIGN_CURRENCY = re.compile(r"[$€£¥]|\bUSD\b|\bEUR\b")
# Google Shopping lists many sellers; naming the wanted store in the query makes it return
# that store's listings (without it a laptop search can contain no Flipkart result at all).
_STORE_WORDS = {
    Marketplace.FLIPKART: "flipkart",
    Marketplace.CROMA: "croma",
    Marketplace.RELIANCE_DIGITAL: "reliance digital",
    Marketplace.VIJAY_SALES: "vijay sales",
    Marketplace.TATA_CLIQ: "tata cliq",
}
_MODEL_KEYS = ("item model number", "model number", "model name", "model")


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value).replace(",", ""))
    except InvalidOperation:
        return None
    return result if result > 0 else None


def _int(value: Any) -> int | None:
    try:
        return int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_brand(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = re.sub(r"^(visit the|brand:)\s*", "", value.strip(), flags=re.I)
    return re.sub(r"\s*store$", "", text, flags=re.I).strip() or None


def _flatten_specs(value: Any) -> dict[str, SpecValue]:
    specs: dict[str, SpecValue] = {}
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(v, str | int | float | bool):
                specs[str(k)] = v
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                key = item.get("title") or item.get("name") or item.get("key")
                val = item.get("value")
                if key and isinstance(val, str | int | float | bool):
                    specs[str(key)] = val
    return specs


def _model_from_specs(specs: dict[str, SpecValue]) -> str | None:
    lowered = {k.lower(): v for k, v in specs.items()}
    for key in _MODEL_KEYS:
        if key in lowered and isinstance(lowered[key], str):
            return str(lowered[key]).strip() or None
    return None


class SerpApiProductProvider:
    name = "serpapi"

    def __init__(
        self,
        http: ProviderHttp,
        api_key: str,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        memo_ttl_s: float = 600,
        acquire: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._http = http
        self._api_key = api_key
        self._clock = clock
        self._memo_ttl_s = memo_ttl_s
        self._memo: dict[str, tuple[float, dict[str, Any]]] = {}
        # Counts an extra SerpApi request against the daily budget (the registry counts one).
        self._acquire = acquire

    async def _get(self, params: dict[str, str]) -> dict[str, Any]:
        data = await self._http.request_json(
            "GET", SERPAPI_URL, params={**params, "api_key": self._api_key, "output": "json"}
        )
        if not isinstance(data, dict):
            return {}
        if data.get("error"):
            # SerpApi reports "no results" as an error string; treat as empty.
            return {}
        return data

    # --- search ---

    async def search(self, query: ProductQuery) -> list[ProviderListing]:
        """Amazon.in search (real marketplace listings) and Google Shopping (per-store prices,
        which include Flipkart, Croma and others). Only what the query's platforms need is run:
        each is one SerpApi search against the daily budget."""
        wanted = set(query.marketplaces) if query.marketplaces else None
        use_google = wanted is None or bool(wanted - {Marketplace.AMAZON})
        use_amazon = wanted is None or Marketplace.AMAZON in wanted
        amazon: list[ProviderListing] = []
        if use_google and use_amazon:
            # The registry counted one request; the second must be counted here.
            amazon = await self._search_amazon_counted(query)
        elif use_amazon:
            amazon = await self._search_amazon(query)
        google = await self._search_google(query, wanted) if use_google else []
        return [*amazon, *google][: max(query.limit, len(amazon))]

    async def _search_amazon_counted(self, query: ProductQuery) -> list[ProviderListing]:
        try:
            if self._acquire is not None:
                await self._acquire()
            return await self._search_amazon(query)
        except (QuotaExhaustedError, ProviderError) as exc:
            log.warning("Amazon.in search skipped: %s", exc)
            return []

    async def _search_amazon(self, query: ProductQuery) -> list[ProviderListing]:
        params = {
            "engine": "amazon",
            "k": query.query,
            "amazon_domain": "amazon.in",
            "language": "en_IN",
        }
        if query.min_price is not None:
            params["low_price"] = str(int(query.min_price))
        if query.max_price is not None:
            params["high_price"] = str(int(query.max_price))
        data = await self._get(params)
        listings: list[ProviderListing] = []
        for item in data.get("organic_results") or []:
            if isinstance(item, dict) and (parsed := self._from_amazon(item, query.category)):
                listings.append(parsed)
            if len(listings) >= query.limit:
                break
        return listings

    def _from_amazon(self, item: dict[str, Any], category: str | None) -> ProviderListing | None:
        title, asin = item.get("title"), item.get("asin")
        price = _decimal(item.get("extracted_price"))
        if not isinstance(title, str) or not isinstance(asin, str) or price is None:
            return None
        if item.get("sponsored") or _FOREIGN_CURRENCY.search(str(item.get("price", ""))):
            return None
        return ProviderListing(
            provider=self.name,
            source_key=f"amazon:{asin}",
            marketplace=Marketplace.AMAZON,
            external_id=asin,
            url=item.get("link_clean") or f"https://www.amazon.in/dp/{asin}",
            title=title,
            brand=detect_brand(title),
            category=category,
            price=price,
            mrp=_decimal(item.get("extracted_old_price")),
            availability=availability_from_text(item.get("delivery")),
            condition=detect_condition(title),
            rating=_float(item.get("rating")),
            rating_count=_int(item.get("reviews")),
            source_type=SourceType.MARKETPLACE_LISTING,
            source_name="Amazon.in search via SerpApi",
            verification=Verification.SOURCE_REPORTED,
            observed_at=self._clock(),
        )

    async def _search_google(
        self, query: ProductQuery, wanted: set[Marketplace] | None
    ) -> list[ProviderListing]:
        stores = [_STORE_WORDS[m] for m in sorted(wanted or ()) if m in _STORE_WORDS]
        text = f"{query.query} {' '.join(stores)}" if wanted and len(stores) <= 2 else query.query
        params = {
            "engine": "google_shopping",
            "q": text,
            "gl": "in",
            "hl": "en",
            "google_domain": "google.co.in",
            "location": "India",
        }
        if query.min_price is not None:
            params["min_price"] = str(int(query.min_price))
        if query.max_price is not None:
            params["max_price"] = str(int(query.max_price))
        data = await self._get(params)
        items: list[Any] = list(data.get("shopping_results") or [])
        for group in data.get("categorized_shopping_results") or []:
            if isinstance(group, dict):
                items.extend(group.get("shopping_results") or [])
        listings: list[ProviderListing] = []
        for item in items:
            parsed = self._from_shopping(item, query.category) if isinstance(item, dict) else None
            if parsed is not None and (wanted is None or parsed.marketplace in wanted):
                listings.append(parsed)
            if len(listings) >= query.limit:
                break
        return listings

    def _from_shopping(self, item: dict[str, Any], category: str | None) -> ProviderListing | None:
        title = item.get("title")
        price = _decimal(item.get("extracted_price"))
        if not isinstance(title, str) or price is None:
            return None
        if _FOREIGN_CURRENCY.search(str(item.get("price", ""))):
            return None
        store = item.get("source") if isinstance(item.get("source"), str) else None
        product_id = item.get("product_id")
        identity = str(product_id) if product_id else slug(title)
        return ProviderListing(
            provider=self.name,
            source_key=f"serpapi:gs:{identity}:{slug(store or 'unknown')}",
            marketplace=marketplace_from_store(store),
            url=item.get("product_link") or item.get("link"),
            title=title,
            brand=detect_brand(title),
            category=category,
            seller_name=store,
            price=price,
            mrp=_decimal(item.get("extracted_old_price")),
            availability=availability_from_text(item.get("delivery")),
            condition=detect_condition(title),
            rating=_float(item.get("rating")),
            rating_count=_int(item.get("reviews")),
            provider_token=item.get("immersive_product_page_token"),
            source_type=SourceType.SEARCH_RESULT,
            source_name=f"Google Shopping via SerpApi ({store or 'unknown store'})",
            verification=Verification.SOURCE_REPORTED,
            observed_at=self._clock(),
        )

    # --- immersive product page (stores + features), memoised to save quota ---

    async def _immersive(self, token: str) -> dict[str, Any]:
        now = time.monotonic()
        cached = self._memo.get(token)
        if cached and now - cached[0] < self._memo_ttl_s:
            return cached[1]
        data = await self._get(
            {"engine": "google_immersive_product", "page_token": token, "more_stores": "true"}
        )
        result = (
            data.get("product_results") if isinstance(data.get("product_results"), dict) else {}
        )
        self._memo[token] = (now, result or {})
        return result or {}

    async def get_listings(self, ref: ProductRef) -> list[ProviderListing]:
        if not ref.provider_token:
            return []
        product = await self._immersive(ref.provider_token)
        title = product.get("title") or ref.name or "unknown product"
        brand = _clean_brand(product.get("brand")) or ref.brand
        listings: list[ProviderListing] = []
        for store in product.get("stores") or []:
            if not isinstance(store, dict):
                continue
            price = _decimal(store.get("extracted_price"))
            name = store.get("name") if isinstance(store.get("name"), str) else None
            if price is None or _FOREIGN_CURRENCY.search(str(store.get("price", ""))):
                continue
            details = [d for d in store.get("details_and_offers") or [] if isinstance(d, str)]
            listings.append(
                ProviderListing(
                    provider=self.name,
                    source_key=f"serpapi:gp:{slug(title)}:{slug(name or 'unknown')}",
                    marketplace=marketplace_from_store(name),
                    url=store.get("link"),
                    title=str(title),
                    brand=brand,
                    model_number=ref.model_number,
                    category=ref.category,
                    seller_name=name,
                    price=price,
                    mrp=_decimal(store.get("extracted_original_price")),
                    availability=availability_from_text(details),
                    condition=detect_condition(str(title)),
                    rating=_float(store.get("rating")),
                    rating_count=_int(store.get("reviews")),
                    offer_texts=[d for d in details if "stock" not in d.lower()],
                    provider_token=ref.provider_token,
                    source_type=SourceType.SEARCH_RESULT,
                    source_name=f"Google Shopping via SerpApi ({name or 'unknown store'})",
                    verification=Verification.SOURCE_REPORTED,
                    observed_at=self._clock(),
                )
            )
        return listings

    async def get_details(self, ref: ProductRef) -> ProductDetails | None:
        if not ref.provider_token:
            return None
        product = await self._immersive(ref.provider_token)
        if not product:
            return None
        about = (
            product.get("about_the_product")
            if isinstance(product.get("about_the_product"), dict)
            else {}
        )
        specs = _flatten_specs((about or {}).get("features"))
        title = product.get("title") or (about or {}).get("title") or ref.name
        if not title:
            return None
        return ProductDetails(
            provider=self.name,
            name=str(title),
            brand=_clean_brand(product.get("brand")) or ref.brand,
            model_number=_model_from_specs(specs) or ref.model_number,
            category=ref.category,
            specs=specs,
            source_type=SourceType.SEARCH_RESULT,
            source_name="Google product page via SerpApi",
            verification=Verification.SOURCE_REPORTED,
            retrieved_at=self._clock(),
        )

    # --- marketplace lookup ---

    async def lookup(self, marketplace: Marketplace, external_id: str) -> list[ProviderListing]:
        if marketplace is not Marketplace.AMAZON:
            return []
        data = await self._get(
            {"engine": "amazon_product", "asin": external_id, "amazon_domain": "amazon.in"}
        )
        product = (
            data.get("product_results") if isinstance(data.get("product_results"), dict) else None
        )
        if not product:
            return []
        title = product.get("title")
        price = _decimal(product.get("extracted_price"))
        if not isinstance(title, str) or price is None:
            return []
        specs = _flatten_specs(product.get("product_details"))
        return [
            ProviderListing(
                provider=self.name,
                source_key=f"amazon:{external_id}",
                marketplace=Marketplace.AMAZON,
                external_id=external_id,
                url=f"https://www.amazon.in/dp/{external_id}",
                title=title,
                brand=_clean_brand(product.get("brand")) or detect_brand(title),
                model_number=_model_from_specs(specs),
                price=price,
                mrp=_decimal(product.get("extracted_old_price")),
                availability=availability_from_text(product.get("stock")),
                condition=detect_condition(title),
                rating=_float(product.get("rating")),
                rating_count=_int(product.get("reviews")),
                specs=specs,
                source_type=SourceType.MARKETPLACE_LISTING,
                source_name="Amazon.in product page via SerpApi",
                verification=Verification.SOURCE_REPORTED,
                observed_at=self._clock(),
            )
        ]
