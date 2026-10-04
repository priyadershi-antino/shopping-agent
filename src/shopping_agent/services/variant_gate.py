"""Variant validation (a hard gate) and listing risk flags.

A listing enters price comparison only if it matches the target variant on every critical
attribute the target specifies. Mismatches are rejected with a reason; attributes the
listing doesn't state are reported as unverified rather than assumed.
"""

from decimal import Decimal
from enum import StrEnum
from statistics import median

from pydantic import BaseModel, Field

from shopping_agent.domain.product import Condition, Listing, VariantAttributes
from shopping_agent.services.normalization import normalize_model_number, normalize_seller

CRITICAL_ATTRIBUTES = ("ram_gb", "storage_gb", "connectivity", "region", "model_year", "generation")
NON_NEW = frozenset({Condition.RENEWED, Condition.REFURBISHED, Condition.OPEN_BOX, Condition.USED})


class GateDecision(StrEnum):
    PASS = "pass"  # noqa: S105  (not a password)
    PASS_UNVERIFIED = "pass_unverified"  # noqa: S105
    REJECT = "reject"


class AttributeMismatch(BaseModel):
    attribute: str
    expected: str
    actual: str


class GateResult(BaseModel):
    listing_id: str
    decision: GateDecision
    mismatches: list[AttributeMismatch] = Field(default_factory=list)
    unverified: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


def check_variant(
    listing: Listing,
    target: VariantAttributes,
    *,
    target_model_number: str | None = None,
    allowed_conditions: frozenset[Condition] = frozenset({Condition.NEW, Condition.UNKNOWN}),
) -> GateResult:
    mismatches: list[AttributeMismatch] = []
    unverified: list[str] = []

    expected_model = normalize_model_number(target_model_number)
    actual_model = normalize_model_number(listing.model_number)
    if expected_model and actual_model and expected_model != actual_model:
        mismatches.append(
            AttributeMismatch(
                attribute="model_number", expected=expected_model, actual=actual_model
            )
        )

    if listing.condition not in allowed_conditions:
        mismatches.append(
            AttributeMismatch(attribute="condition", expected="new", actual=listing.condition.value)
        )
    elif listing.condition is Condition.UNKNOWN:
        unverified.append("condition")

    for name in CRITICAL_ATTRIBUTES:
        expected = getattr(target, name)
        actual = getattr(listing.attributes, name)
        if name == "region":
            # Most listings don't state region; only an explicit import is a mismatch.
            if actual == "international" and expected != "international":
                mismatches.append(
                    AttributeMismatch(attribute=name, expected=str(expected or "IN"), actual=actual)
                )
            continue
        if expected is None:
            continue
        if actual is None:
            unverified.append(name)
        elif actual != expected:
            mismatches.append(
                AttributeMismatch(attribute=name, expected=str(expected), actual=str(actual))
            )

    if mismatches:
        decision = GateDecision.REJECT
        reasons = [
            f"different variant: {m.attribute} is {m.actual}, expected {m.expected}"
            for m in mismatches
        ]
    elif unverified:
        decision = GateDecision.PASS_UNVERIFIED
        reasons = [f"{name} not stated on listing" for name in unverified]
    else:
        decision, reasons = GateDecision.PASS, []
    return GateResult(
        listing_id=listing.id,
        decision=decision,
        mismatches=mismatches,
        unverified=unverified,
        reasons=reasons,
    )


class RiskKind(StrEnum):
    NOT_NEW = "not_new"
    INTERNATIONAL_UNIT = "international_unit"
    UNKNOWN_SELLER = "unknown_seller"
    UNAUTHORISED_SELLER = "unauthorised_seller"
    PRICE_ANOMALY = "price_anomaly"


class RiskFlag(BaseModel):
    kind: RiskKind
    message: str


def suspiciously_cheap(
    prices: dict[str, Decimal], *, ratio: Decimal = Decimal("0.4"), min_items: int = 4
) -> set[str]:
    """Ids priced far below the rest of a shortlist (below `ratio` of the median).

    Different products legitimately differ in price, so this is only a warning to verify the
    seller, condition and variant: it demotes the item in the ranking, it never hides it."""
    values = [p for p in prices.values() if p > 0]
    if len(values) < min_items:
        return set()
    cutoff = Decimal(median(values)) * ratio
    return {key for key, price in prices.items() if 0 < price < cutoff}


def listing_risk_flags(
    listing: Listing,
    *,
    peer_prices: list[Decimal] | None = None,
    authorised_sellers: set[str] | None = None,
    anomaly_ratio: Decimal = Decimal("0.6"),
) -> list[RiskFlag]:
    flags: list[RiskFlag] = []
    if listing.condition in NON_NEW:
        flags.append(
            RiskFlag(kind=RiskKind.NOT_NEW, message=f"listed as {listing.condition.value}")
        )
    if listing.attributes.region == "international":
        flags.append(
            RiskFlag(
                kind=RiskKind.INTERNATIONAL_UNIT,
                message="international/imported unit — India warranty may not apply",
            )
        )
    if not listing.seller_name:
        flags.append(RiskFlag(kind=RiskKind.UNKNOWN_SELLER, message="seller not stated"))
    elif authorised_sellers is not None and normalize_seller(listing.seller_name) not in {
        normalize_seller(s) for s in authorised_sellers
    }:
        flags.append(
            RiskFlag(
                kind=RiskKind.UNAUTHORISED_SELLER,
                message=f"seller '{listing.seller_name}' is not on the known authorised list",
            )
        )
    peers = [p for p in (peer_prices or []) if p > 0]
    if len(peers) >= 2:
        mid = Decimal(median(peers))
        if listing.price < mid * anomaly_ratio:
            flags.append(
                RiskFlag(
                    kind=RiskKind.PRICE_ANOMALY,
                    message=(
                        f"price is far below other listings (median {mid:.0f}); "
                        "verify seller, condition and variant"
                    ),
                )
            )
    return flags
