"""Alert rules for watched items. Pure functions: compare the previous check with the current
one and return what is worth telling the user.

Price alerts fire on a *transition* (the price crosses the target), never on every check while
it stays below. Offer alerts compare the offers usable by the user between two checks.
"""

from datetime import datetime, time
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from shopping_agent.domain.common import money
from shopping_agent.domain.product import Availability
from shopping_agent.services.formatting import format_inr
from shopping_agent.services.price_baseline import IST


class AlertKind(StrEnum):
    TARGET_PRICE = "target_price"
    TARGET_EFFECTIVE = "target_effective"
    PRICE_DROP = "price_drop"
    BACK_IN_STOCK = "back_in_stock"
    OFFER_NEW = "offer_new"
    OFFER_CHANGED = "offer_changed"
    OFFER_ENDED = "offer_ended"
    SALE_UPCOMING = "sale_upcoming"
    SALE_START = "sale_start"


PRICE_KINDS = frozenset({AlertKind.TARGET_PRICE, AlertKind.TARGET_EFFECTIVE, AlertKind.PRICE_DROP})
IN_STOCK = frozenset({Availability.IN_STOCK, Availability.LIMITED})


class WatchRules(BaseModel):
    label: str
    target_price: Decimal | None = None
    target_effective: Decimal | None = None
    target_drop_pct: Decimal | None = None
    reference_price: Decimal
    notify_back_in_stock: bool = True
    notify_offer_changes: bool = True


class CheckSnapshot(BaseModel):
    """What one check saw. `offers` maps an offer identity to a description of its terms,
    and only includes offers the user can use (eligible or conditional)."""

    listing_price: Decimal
    best_effective: Decimal
    effective_how: str | None = None  # e.g. "HDFC credit card + coupon"
    availability: Availability
    offers: dict[str, str] = Field(default_factory=dict)


class AlertCandidate(BaseModel):
    kind: AlertKind
    key: str  # distinguishes events of the same kind (used in the dedup key)
    title: str
    message: str
    data: dict[str, Any] = Field(default_factory=dict)


def _crossed(previous: Decimal | None, current: Decimal, target: Decimal) -> bool:
    return current <= target and (previous is None or previous > target)


def drop_pct(reference: Decimal, current: Decimal) -> Decimal:
    return money((reference - current) / reference * 100) if reference else Decimal(0)


def evaluate(
    rules: WatchRules, previous: CheckSnapshot | None, current: CheckSnapshot
) -> list[AlertCandidate]:
    out: list[AlertCandidate] = []
    name = rules.label
    listed = format_inr(current.listing_price)
    yours = format_inr(current.best_effective)
    how = f" ({current.effective_how})" if current.effective_how else ""

    if rules.target_price is not None and _crossed(
        previous.listing_price if previous else None, current.listing_price, rules.target_price
    ):
        out.append(
            AlertCandidate(
                kind=AlertKind.TARGET_PRICE,
                key=str(rules.target_price),
                title=f"{name}: listed at {listed}",
                message=(
                    f"{name} is listed at {listed}, at or below your target "
                    f"{format_inr(rules.target_price)}. Best price for you: {yours}{how}."
                ),
                data={
                    "listing_price": str(current.listing_price),
                    "target": str(rules.target_price),
                },
            )
        )
    if rules.target_effective is not None and _crossed(
        previous.best_effective if previous else None,
        current.best_effective,
        rules.target_effective,
    ):
        out.append(
            AlertCandidate(
                kind=AlertKind.TARGET_EFFECTIVE,
                key=str(rules.target_effective),
                title=f"{name}: {yours} for you",
                message=(
                    f"{name} now costs {yours} for you{how}, at or below your target "
                    f"{format_inr(rules.target_effective)}. Listed price {listed}."
                ),
                data={
                    "best_effective": str(current.best_effective),
                    "target": str(rules.target_effective),
                },
            )
        )
    if rules.target_drop_pct is not None:
        now_drop = drop_pct(rules.reference_price, current.listing_price)
        before_drop = drop_pct(rules.reference_price, previous.listing_price) if previous else None
        if now_drop >= rules.target_drop_pct and (
            before_drop is None or before_drop < rules.target_drop_pct
        ):
            out.append(
                AlertCandidate(
                    kind=AlertKind.PRICE_DROP,
                    key=str(rules.target_drop_pct),
                    title=f"{name}: down {now_drop}%",
                    message=(
                        f"{name} is listed at {listed}, {now_drop}% below "
                        f"{format_inr(rules.reference_price)} when you started tracking. "
                        f"Best price for you: {yours}{how}."
                    ),
                    data={"drop_pct": str(now_drop), "listing_price": str(current.listing_price)},
                )
            )
    if (
        rules.notify_back_in_stock
        and previous is not None
        and previous.availability is Availability.OUT_OF_STOCK
        and current.availability in IN_STOCK
    ):
        out.append(
            AlertCandidate(
                kind=AlertKind.BACK_IN_STOCK,
                key=current.availability.value,
                title=f"{name} is back in stock",
                message=f"{name} is available again at {listed} (best price for you: {yours}).",
            )
        )
    if rules.notify_offer_changes and previous is not None:
        before, now = previous.offers, current.offers
        for key in sorted(now.keys() - before.keys()):
            out.append(
                AlertCandidate(
                    kind=AlertKind.OFFER_NEW,
                    key=f"{key}={now[key]}",
                    title=f"New offer on {name}",
                    message=f"New offer on {name}: {now[key]}. Best price for you: {yours}{how}.",
                    data={"offer": key},
                )
            )
        for key in sorted(before.keys() & now.keys()):
            if before[key] != now[key]:
                out.append(
                    AlertCandidate(
                        kind=AlertKind.OFFER_CHANGED,
                        key=f"{key}={now[key]}",
                        title=f"Offer changed on {name}",
                        message=(
                            f"An offer on {name} changed: was {before[key]}, now {now[key]}. "
                            f"Best price for you: {yours}{how}."
                        ),
                        data={"offer": key},
                    )
                )
        for key in sorted(before.keys() - now.keys()):
            out.append(
                AlertCandidate(
                    kind=AlertKind.OFFER_ENDED,
                    key=f"{key}={before[key]}",
                    title=f"Offer ended on {name}",
                    message=(
                        f"An offer on {name} is no longer shown: {before[key]}. "
                        f"Best price for you is now {yours}{how}."
                    ),
                    data={"offer": key},
                )
            )
    return out


def parse_quiet_hours(spec: str) -> tuple[time, time] | None:
    """'23-7' or '23:30-07:00' (IST) → (start, end). Empty or invalid → None."""
    spec = spec.strip()
    if not spec or "-" not in spec:
        return None
    try:
        parts = []
        for raw in spec.split("-", 1):
            hh, _, mm = raw.strip().partition(":")
            parts.append(time(int(hh) % 24, int(mm or 0)))
    except ValueError:
        return None
    return parts[0], parts[1]


def in_quiet_hours(now: datetime, spec: str) -> bool:
    window = parse_quiet_hours(spec)
    if window is None:
        return False
    start, end = window
    local = now.astimezone(IST).time()
    if start <= end:
        return start <= local < end
    return local >= start or local < end  # wraps midnight
