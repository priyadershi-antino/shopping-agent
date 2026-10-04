from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from shopping_agent.domain.common import Verification
from shopping_agent.domain.evidence import new_id
from shopping_agent.domain.product import Marketplace


class SaleState(StrEnum):
    NORMAL = "normal"
    PRE_SALE = "pre_sale"
    EARLY_ACCESS = "early_access"
    ACTIVE = "active"
    POST_SALE = "post_sale"


class PhaseKind(StrEnum):
    EARLY_ACCESS = "early_access"
    MAIN = "main"
    OTHER = "other"


class SalePhase(BaseModel):
    name: str
    kind: PhaseKind
    start: datetime
    end: datetime
    eligibility: str | None = None  # e.g. "flipkart_plus", "amazon_prime"


class SaleEvent(BaseModel):
    """A sale event as verified from current sources. Never hard-coded."""

    id: str = Field(default_factory=new_id)
    name: str
    platform: Marketplace
    start: datetime
    end: datetime
    phases: list[SalePhase] = Field(default_factory=list)
    partner_banks: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    official_sources: list[str] = Field(default_factory=list)
    verification: Verification
    last_verified_at: datetime
