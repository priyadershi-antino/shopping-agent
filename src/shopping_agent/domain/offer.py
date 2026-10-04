"""Offers, the user's payment profile, and computed price scenarios."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, Field, model_validator

from shopping_agent.domain.common import ZERO, Verification
from shopping_agent.domain.evidence import new_id
from shopping_agent.domain.product import Marketplace


class OfferType(StrEnum):
    SELLER = "seller"  # special price from seller beyond the listed price
    COUPON = "coupon"
    MEMBERSHIP = "membership"
    BANK = "bank"
    CASHBACK = "cashback"  # platform/wallet cashback
    EXCHANGE = "exchange"
    OTHER = "other"


# Order in which instant benefits are applied. Each percentage is computed on the order
# value after the earlier discounts — the conservative assumption (smaller base).
APPLICATION_ORDER: dict[OfferType, int] = {
    OfferType.SELLER: 0,
    OfferType.COUPON: 1,
    OfferType.MEMBERSHIP: 2,
    OfferType.OTHER: 3,
    OfferType.BANK: 4,
    OfferType.CASHBACK: 5,
    OfferType.EXCHANGE: 6,
}

# At most one offer of these types can apply to an order.
ONE_PER_ORDER = frozenset({OfferType.BANK, OfferType.COUPON, OfferType.EXCHANGE})


class CardType(StrEnum):
    CREDIT = "credit"
    DEBIT = "debit"
    ANY = "any"


class EmiRule(StrEnum):
    EMI_ONLY = "emi_only"
    NON_EMI_ONLY = "non_emi_only"
    BOTH = "both"
    UNKNOWN = "unknown"


class BenefitTiming(StrEnum):
    INSTANT = "instant"
    DEFERRED = "deferred"  # cashback credited later


class PaymentRequirement(BaseModel):
    issuer: str | None = None  # e.g. "HDFC"; None = any issuer
    card_type: CardType = CardType.ANY
    network: str | None = None  # e.g. "rupay"

    def describe(self) -> str:
        parts = [self.issuer or "any bank"]
        if self.card_type is not CardType.ANY:
            parts.append(f"{self.card_type.value} card")
        else:
            parts.append("card")
        if self.network:
            parts.append(f"({self.network})")
        return " ".join(parts)


class StackingTerms(BaseModel):
    """What the offer's terms say about combining. Silence means unknown, not allowed."""

    exclusive: bool = False  # "cannot be clubbed with any other offer"
    combinable_with: frozenset[OfferType] = frozenset()


class ExchangeTerms(BaseModel):
    max_value: Decimal  # the advertised "up to" value
    min_value: Decimal | None = None
    bonus: Decimal = ZERO
    device_categories: list[str] = Field(default_factory=list)


class Offer(BaseModel):
    id: str = Field(default_factory=new_id)
    type: OfferType
    title: str
    platform: Marketplace | None = None  # None = not platform-specific
    provider: str | None = None  # bank or platform running the offer
    flat_amount: Decimal | None = None
    percent: Decimal | None = None  # 10 means 10%
    max_discount: Decimal | None = None
    min_order_value: Decimal | None = None
    payment: PaymentRequirement | None = None
    emi_rule: EmiRule = EmiRule.UNKNOWN
    membership_required: str | None = None
    exchange: ExchangeTerms | None = None
    applicable_categories: list[str] | None = None
    category_exclusions: list[str] = Field(default_factory=list)
    stacking: StackingTerms = Field(default_factory=StackingTerms)
    timing: BenefitTiming = BenefitTiming.INSTANT
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    verification: Verification
    verified_at: datetime | None = None
    stale: bool = False
    terms_text: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_benefit(self) -> Self:
        if self.type is OfferType.EXCHANGE:
            if self.exchange is None:
                raise ValueError("exchange offers need exchange terms")
            return self
        if (self.flat_amount is None) == (self.percent is None):
            raise ValueError("set exactly one of flat_amount or percent")
        if self.percent is not None and not (0 < self.percent <= 100):
            raise ValueError("percent must be in (0, 100]")
        return self


# --- User profile (high-level eligibility only; never card numbers or credentials) ---


class PaymentMethod(BaseModel):
    issuer: str
    card_type: CardType
    network: str | None = None
    label: str | None = None

    def describe(self) -> str:
        return self.label or f"{self.issuer} {self.card_type.value} card"


class ExchangeDevice(BaseModel):
    category: str
    model: str | None = None
    condition: str | None = None


class UserProfile(BaseModel):
    payment_methods: list[PaymentMethod] = Field(default_factory=list)
    memberships: set[str] = Field(default_factory=set)
    exchange_devices: list[ExchangeDevice] = Field(default_factory=list)
    prefers_emi: bool | None = None


# --- Results ---


class EligibilityStatus(StrEnum):
    ELIGIBLE = "eligible"  # all terms satisfied by what we know
    CONDITIONAL = "conditional"  # achievable, but depends on something unconfirmed or an action
    INELIGIBLE = "ineligible"


class Eligibility(BaseModel):
    offer_id: str
    status: EligibilityStatus
    reasons: list[str] = Field(default_factory=list)  # why ineligible
    conditions: list[str] = Field(default_factory=list)  # what must hold
    payment_method: PaymentMethod | None = None  # matching user method, if any


class PairStatus(StrEnum):
    COMBINABLE = "combinable"
    INCOMPATIBLE = "incompatible"
    UNKNOWN = "unknown"


class StackingPair(BaseModel):
    a: str
    b: str
    status: PairStatus
    reason: str


class StackingMatrix(BaseModel):
    offer_ids: list[str]
    pairs: list[StackingPair]

    def status(self, a: str, b: str) -> PairStatus:
        for p in self.pairs:
            if {p.a, p.b} == {a, b}:
                return p.status
        raise KeyError((a, b))


class AppliedOffer(BaseModel):
    offer_id: str
    type: OfferType
    title: str
    benefit_low: Decimal  # conservative benefit
    benefit_high: Decimal  # best case (differs only for exchange ranges)
    timing: BenefitTiming
    verification: Verification


class PriceScenario(BaseModel):
    label: str
    name: str
    listing_id: str
    base_price: Decimal
    applied: list[AppliedOffer] = Field(default_factory=list)
    upfront_best: Decimal  # paid at checkout, best case
    upfront_conservative: Decimal  # paid at checkout, conservative
    deferred_cashback: Decimal = ZERO
    net_after_cashback: Decimal  # conservative upfront minus deferred cashback
    conditional: bool = False
    conditions: list[str] = Field(default_factory=list)
    verification: Verification = Verification.VERIFIED
    stale: bool = False

    @property
    def is_range(self) -> bool:
        return self.upfront_best != self.upfront_conservative
