from datetime import date
from enum import StrEnum

from pydantic import BaseModel, Field


class ThemeKind(StrEnum):
    POSITIVE = "positive"
    COMPLAINT = "complaint"
    RECURRING_ISSUE = "recurring_issue"  # reported across sources / over time
    USE_CASE_ISSUE = "use_case_issue"


class ReviewCoverage(StrEnum):
    LIMITED = "limited"
    MODERATE = "moderate"
    BROAD = "broad"


class ReviewTheme(BaseModel):
    kind: ThemeKind
    theme: str
    mentions: int  # reviews mentioning it, within the analysed sample
    sample_size: int
    use_case: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class ReviewFindings(BaseModel):
    """Customer opinions and independent review findings — never presented as verified fact."""

    variant_id: str
    themes: list[ReviewTheme] = Field(default_factory=list)
    reviews_analysed: int
    sources: list[str] = Field(default_factory=list)
    earliest: date | None = None
    latest: date | None = None
    coverage: ReviewCoverage
