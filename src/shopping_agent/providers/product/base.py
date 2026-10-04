"""Product provider abstraction. Providers return raw-but-typed data; normalization is ours."""

from datetime import datetime
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, Field

from shopping_agent.domain.common import SourceType, Verification
from shopping_agent.domain.offer import Offer
from shopping_agent.domain.product import Availability, Condition, Marketplace

SpecValue = str | int | float | bool


class ProductQuery(BaseModel):
    query: str
    category: str | None = None
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    limit: int = Field(default=20, ge=1, le=50)


class ProductRef(BaseModel):
    """What we know about a product, so a provider can find it."""

    name: str | None = None
    brand: str | None = None
    model_number: str | None = None
    category: str | None = None
    marketplace: Marketplace | None = None
    external_id: str | None = None
    provider_token: str | None = None  # provider-specific follow-up token


class ProviderListing(BaseModel):
    provider: str
    source_key: str  # stable identity for this listing across calls
    marketplace: Marketplace
    external_id: str | None = None
    url: str | None = None
    title: str
    brand: str | None = None
    model_number: str | None = None
    category: str | None = None
    seller_name: str | None = None
    price: Decimal
    mrp: Decimal | None = None
    availability: Availability = Availability.UNKNOWN
    condition: Condition = Condition.UNKNOWN
    rating: float | None = None
    rating_count: int | None = None
    specs: dict[str, SpecValue] = Field(default_factory=dict)
    offers: list[Offer] = Field(default_factory=list)  # structured offers, if the provider has them
    offer_texts: list[str] = Field(default_factory=list)  # raw offer text (untrusted)
    provider_token: str | None = None
    source_type: SourceType
    source_name: str
    verification: Verification
    observed_at: datetime


class ProductDetails(BaseModel):
    provider: str
    name: str
    brand: str | None = None
    model_number: str | None = None
    category: str | None = None
    specs: dict[str, SpecValue] = Field(default_factory=dict)
    official_url: str | None = None
    source_type: SourceType
    source_name: str
    url: str | None = None
    verification: Verification
    retrieved_at: datetime


class ProductProvider(Protocol):
    name: str

    async def search(self, query: ProductQuery) -> list[ProviderListing]: ...

    async def lookup(self, marketplace: Marketplace, external_id: str) -> list[ProviderListing]:
        """Find a listing by marketplace ID (ASIN, Flipkart PID). Empty if unsupported."""
        ...

    async def get_details(self, ref: ProductRef) -> ProductDetails | None: ...

    async def get_listings(self, ref: ProductRef) -> list[ProviderListing]: ...
