from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

from shopping_agent.domain.common import INR
from shopping_agent.domain.evidence import new_id


class Marketplace(StrEnum):
    AMAZON = "amazon"
    FLIPKART = "flipkart"
    CROMA = "croma"
    RELIANCE_DIGITAL = "reliance_digital"
    VIJAY_SALES = "vijay_sales"
    TATA_CLIQ = "tata_cliq"
    MANUFACTURER = "manufacturer"
    OTHER = "other"


class Condition(StrEnum):
    NEW = "new"
    RENEWED = "renewed"
    REFURBISHED = "refurbished"
    OPEN_BOX = "open_box"
    USED = "used"
    UNKNOWN = "unknown"


class Connectivity(StrEnum):
    WIFI = "wifi"
    CELLULAR = "cellular"


class Availability(StrEnum):
    IN_STOCK = "in_stock"
    LIMITED = "limited"
    OUT_OF_STOCK = "out_of_stock"
    PREORDER = "preorder"
    UNKNOWN = "unknown"


class VariantAttributes(BaseModel):
    """Attributes that distinguish variants. `None` means unknown, never "doesn't matter"."""

    ram_gb: int | None = None
    storage_gb: int | None = None
    connectivity: Connectivity | None = None
    color: str | None = None
    region: str | None = None  # "IN" for India units, "international" for imports
    model_year: int | None = None
    generation: str | None = None
    extra: dict[str, str | int | float | bool] = Field(default_factory=dict)


class Product(BaseModel):
    id: str = Field(default_factory=new_id)
    brand: str
    model_number: str | None = None
    canonical_name: str
    category: str


class Variant(BaseModel):
    id: str = Field(default_factory=new_id)
    product_id: str
    attributes: VariantAttributes = Field(default_factory=VariantAttributes)
    identity_key: str


class Listing(BaseModel):
    """One offer to sell, by one seller, on one marketplace. Price is what the page charges."""

    id: str = Field(default_factory=new_id)
    variant_id: str | None = None
    marketplace: Marketplace
    seller_name: str | None = None
    url: str | None = None
    external_id: str | None = None
    title_raw: str
    brand: str | None = None
    model_number: str | None = None
    price: Decimal
    mrp: Decimal | None = None
    currency: str = INR
    availability: Availability = Availability.UNKNOWN
    condition: Condition = Condition.UNKNOWN
    warranty: str | None = None
    attributes: VariantAttributes = Field(default_factory=VariantAttributes)
    observed_at: datetime
    evidence_ids: list[str] = Field(default_factory=list)
