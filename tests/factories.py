"""Small builders for domain objects used across tests."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from shopping_agent.domain.common import Verification
from shopping_agent.domain.evidence import new_id
from shopping_agent.domain.offer import (
    BenefitTiming,
    CardType,
    ExchangeTerms,
    Offer,
    OfferType,
    PaymentMethod,
    PaymentRequirement,
    StackingTerms,
    UserProfile,
)
from shopping_agent.domain.price import PriceObservation
from shopping_agent.domain.product import (
    Availability,
    Condition,
    Listing,
    Marketplace,
    VariantAttributes,
)
from shopping_agent.tools.common import ListingSummary

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


def D(value: str | int | Decimal) -> Decimal:
    return Decimal(str(value))


def listing(
    price: str | int | Decimal = "79999",
    *,
    marketplace: Marketplace = Marketplace.FLIPKART,
    **kwargs: Any,
) -> Listing:
    data: dict[str, Any] = {
        "marketplace": marketplace,
        "title_raw": "HP Victus 15 16GB 512GB",
        "brand": "HP",
        "model_number": "15-fa1099TX",
        "price": D(price),
        "condition": Condition.NEW,
        "seller_name": "RetailNet",
        "attributes": VariantAttributes(ram_gb=16, storage_gb=512),
        "observed_at": NOW,
    }
    data.update(kwargs)
    return Listing(**data)


def bank_offer(
    issuer: str = "HDFC",
    *,
    percent: str | None = "10",
    flat: str | None = None,
    cap: str | None = "4000",
    card_type: CardType = CardType.CREDIT,
    combinable_with: frozenset[OfferType] = frozenset(),
    **kwargs: Any,
) -> Offer:
    data: dict[str, Any] = {
        "type": OfferType.BANK,
        "title": f"{issuer} {card_type.value} card offer",
        "platform": Marketplace.FLIPKART,
        "provider": issuer,
        "percent": D(percent) if percent else None,
        "flat_amount": D(flat) if flat else None,
        "max_discount": D(cap) if cap else None,
        "payment": PaymentRequirement(issuer=issuer, card_type=card_type),
        "stacking": StackingTerms(combinable_with=combinable_with),
        "verification": Verification.VERIFIED,
        "verified_at": NOW,
    }
    data.update(kwargs)
    return Offer(**data)


def coupon(amount: str = "2000", **kwargs: Any) -> Offer:
    data: dict[str, Any] = {
        "type": OfferType.COUPON,
        "title": f"Coupon {amount} off",
        "platform": Marketplace.FLIPKART,
        "flat_amount": D(amount),
        "verification": Verification.VERIFIED,
    }
    data.update(kwargs)
    return Offer(**data)


def exchange(max_value: str = "15000", min_value: str = "3000", bonus: str = "2000") -> Offer:
    return Offer(
        type=OfferType.EXCHANGE,
        title="Exchange your old laptop",
        platform=Marketplace.FLIPKART,
        exchange=ExchangeTerms(max_value=D(max_value), min_value=D(min_value), bonus=D(bonus)),
        verification=Verification.VERIFIED,
    )


def cashback(percent: str = "5", cap: str = "1500") -> Offer:
    return Offer(
        type=OfferType.CASHBACK,
        title="Platform cashback",
        platform=Marketplace.FLIPKART,
        percent=D(percent),
        max_discount=D(cap),
        timing=BenefitTiming.DEFERRED,
        verification=Verification.VERIFIED,
    )


def profile(*methods: tuple[str, CardType], **kwargs: Any) -> UserProfile:
    return UserProfile(
        payment_methods=[PaymentMethod(issuer=i, card_type=t) for i, t in methods], **kwargs
    )


def daily_prices(
    prices: list[int], *, end: datetime = NOW, listing_id: str = "L1", mrp: int | None = None
) -> list[PriceObservation]:
    """One observation per day, the last one on `end`."""
    start = end - timedelta(days=len(prices) - 1)
    return [
        PriceObservation(
            listing_id=listing_id,
            price=D(p),
            mrp=D(mrp) if mrp else None,
            source="test",
            observed_at=start + timedelta(days=i),
        )
        for i, p in enumerate(prices)
    ]


def listing_summary(
    *,
    title: str,
    marketplace: Marketplace = Marketplace.AMAZON,
    price: Decimal = Decimal("50000"),
    condition: Condition = Condition.NEW,
) -> ListingSummary:
    return ListingSummary(
        listing_id=new_id(),
        product_id=None,
        variant_id=None,
        marketplace=marketplace,
        seller="Test Seller",
        title=title,
        price=price,
        mrp=None,
        availability=Availability.IN_STOCK,
        condition=condition,
        attributes=VariantAttributes(),
        rating=None,
        rating_count=None,
        url=None,
        observed_at=NOW,
        stale=False,
        source="test",
        verification=Verification.SOURCE_REPORTED,
    )
