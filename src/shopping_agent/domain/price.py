from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel

from shopping_agent.domain.common import INR
from shopping_agent.domain.product import Availability


class PriceObservation(BaseModel):
    listing_id: str
    price: Decimal
    mrp: Decimal | None = None
    currency: str = INR
    availability: Availability = Availability.UNKNOWN
    source: str
    observed_at: datetime


class CoverageTier(StrEnum):
    INSUFFICIENT = "insufficient"  # < 14 days
    MINIMUM = "minimum"  # 14–29 days
    GOOD = "good"  # 30–59 days
    STRONG = "strong"  # 60+ days


class Trend(StrEnum):
    RISING = "rising"
    FALLING = "falling"
    FLAT = "flat"
    UNKNOWN = "unknown"


class PriceBaseline(BaseModel):
    """Observed-history summary. Describes *our observations*, not the whole market."""

    listing_id: str
    current: Decimal
    current_observed_at: datetime
    previous: Decimal | None  # last different daily price before the current one
    minimum: Decimal
    maximum: Decimal
    average: Decimal  # mean of daily closing prices
    median: Decimal
    first_observed_at: datetime
    coverage_days: int
    days_with_data: int
    observation_count: int
    tier: CoverageTier
    trend: Trend
    change_vs_average_pct: Decimal
    change_vs_previous_pct: Decimal | None
    at_or_below_observed_min: bool
    mrp: Decimal | None
    discount_vs_mrp_pct: Decimal | None


class PreSaleHike(BaseModel):
    detected: bool
    recent_median: Decimal | None
    prior_median: Decimal | None
    change_pct: Decimal | None
    reason: str
