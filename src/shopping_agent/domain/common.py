"""Shared value types: money, verification, confidence, source types."""

from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

INR = "INR"
_PAISE = Decimal("0.01")


def money(value: Decimal | int | str) -> Decimal:
    """Normalise an amount to 2 decimal places (half-up)."""
    return Decimal(value).quantize(_PAISE, rounding=ROUND_HALF_UP)


ZERO = money(0)


class Verification(StrEnum):
    """How a fact or offer was established. Ordered strongest to weakest."""

    VERIFIED = "verified"  # official / primary source
    SOURCE_REPORTED = "source_reported"  # secondary source (news, aggregator, LLM extraction)
    USER_PROVIDED = "user_provided"  # pasted or typed by the user
    UNVERIFIED = "unverified"


_VERIFICATION_RANK = {
    Verification.VERIFIED: 3,
    Verification.SOURCE_REPORTED: 2,
    Verification.USER_PROVIDED: 1,
    Verification.UNVERIFIED: 0,
}


def weakest(levels: list[Verification]) -> Verification:
    if not levels:
        return Verification.VERIFIED
    return min(levels, key=_VERIFICATION_RANK.__getitem__)


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class SourceType(StrEnum):
    MANUFACTURER = "manufacturer"
    MARKETPLACE_LISTING = "marketplace_listing"
    MARKETPLACE_OFFICIAL = "marketplace_official"  # platform's own sale/offer pages
    BANK_OFFICIAL = "bank_official"
    REVIEW_SITE = "review_site"
    USER_REVIEW = "user_review"
    NEWS = "news"
    SEARCH_RESULT = "search_result"
    PRICE_TRACKING = "price_tracking"  # our own observations
    USER_INPUT = "user_input"
    COMPUTED = "computed"
