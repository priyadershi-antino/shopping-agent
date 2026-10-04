from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from shopping_agent.domain.common import Confidence, SourceType, Verification

EvidenceValue = str | int | float | Decimal | bool | None


def new_id() -> str:
    return uuid4().hex[:16]


class Evidence(BaseModel):
    """A sourced observation. Every important external fact carries one."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=new_id)
    claim: str
    subject: str | None = None  # e.g. variant id the claim is about
    attribute: str | None = None  # e.g. "ram_gb" — used for conflict detection
    value: EvidenceValue = None
    source_type: SourceType
    source_name: str
    url: str | None = None
    retrieved_at: datetime
    verification: Verification
    confidence: Confidence
