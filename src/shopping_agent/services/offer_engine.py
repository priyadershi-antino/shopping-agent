"""Deterministic deal engine.

Rules this module enforces:
- An offer is applied only when its terms support the user's eligibility. Anything unconfirmed
  makes the scenario *conditional*, and the conditions are listed.
- Offers are combined only when the terms say so. Unknown stackability means not combined.
- Upfront (checkout) cost is kept separate from deferred cashback.
- Exchange values are ranges ("up to"), never presented as the maximum.
- Percentages apply to the order value after earlier discounts (conservative base).
"""

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from itertools import combinations

from pydantic import BaseModel, Field

from shopping_agent.domain.common import ZERO, money, weakest
from shopping_agent.domain.offer import (
    APPLICATION_ORDER,
    ONE_PER_ORDER,
    AppliedOffer,
    BenefitTiming,
    CardType,
    Eligibility,
    EligibilityStatus,
    EmiRule,
    Offer,
    OfferType,
    PairStatus,
    PaymentMethod,
    PriceScenario,
    StackingMatrix,
    StackingPair,
    UserProfile,
)
from shopping_agent.domain.product import Listing, Marketplace

_HUNDRED = Decimal(100)


_ISSUER_ALIASES = {
    "state bank of india": "sbi",
    "american express": "amex",
    "kotak mahindra": "kotak",
    "idfc first": "idfc",
    "bank of baroda": "bob",
    "bobcard": "bob",
    "au small finance": "au",
}


def normalize_issuer(name: str | None) -> str:
    if not name:
        return ""
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", name.lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    for long_name, short in _ISSUER_ALIASES.items():
        cleaned = re.sub(rf"\b{long_name}\b", short, cleaned)
    cleaned = re.sub(r"\b(bank|ltd|limited|of india|card|cards|credit|debit)\b", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def issuer_matches(required: str | None, held: str | None) -> bool:
    """Does a card from `held` satisfy an offer for `required`? Co-branded cards count for
    their bank ("Flipkart Axis Bank" satisfies "Axis"), but not the other way round."""
    req, have = normalize_issuer(required), normalize_issuer(held)
    if not req:
        return True
    return req == have or (bool(have) and set(req.split()) <= set(have.split()))


@dataclass(frozen=True)
class OfferContext:
    platform: Marketplace
    listing_price: Decimal
    now: datetime
    category: str | None = None


def _method_matches(offer: Offer, method: PaymentMethod) -> tuple[bool, list[str]]:
    """Return (matches, extra conditions)."""
    req = offer.payment
    assert req is not None
    if req.issuer and not issuer_matches(req.issuer, method.issuer):
        return False, []
    if req.card_type is not CardType.ANY and req.card_type is not method.card_type:
        return False, []
    conditions: list[str] = []
    if req.network:
        if method.network is None:
            conditions.append(f"card network must be {req.network} (not confirmed for your card)")
        elif method.network.lower() != req.network.lower():
            return False, []
    return True, conditions


def check_eligibility(offer: Offer, ctx: OfferContext, profile: UserProfile) -> Eligibility:
    reasons: list[str] = []
    blocking: list[str] = []  # conditions that make the offer conditional
    notes: list[str] = []  # informational conditions
    matched: PaymentMethod | None = None

    if offer.platform is not None and offer.platform is not ctx.platform:
        reasons.append(f"offer is for {offer.platform.value}, not {ctx.platform.value}")
    if offer.valid_to is not None and offer.valid_to < ctx.now:
        reasons.append(f"offer expired on {offer.valid_to:%d %b %Y}")
    if offer.valid_from is not None and offer.valid_from > ctx.now:
        blocking.append(f"offer starts {offer.valid_from:%d %b %Y %H:%M}")
    if offer.applicable_categories is not None:
        cats = {c.lower() for c in offer.applicable_categories}
        if ctx.category is None:
            blocking.append("category eligibility not confirmed")
        elif ctx.category.lower() not in cats:
            reasons.append(f"category {ctx.category} is not covered")
    if ctx.category and ctx.category.lower() in {c.lower() for c in offer.category_exclusions}:
        reasons.append(f"category {ctx.category} is excluded")
    if offer.min_order_value is not None and ctx.listing_price < offer.min_order_value:
        reasons.append(f"minimum order value {offer.min_order_value:.0f} not met")

    if offer.payment is not None:
        needed = offer.payment.describe()
        if not profile.payment_methods:
            blocking.append(f"requires {needed}")
        else:
            for method in profile.payment_methods:
                ok, extra = _method_matches(offer, method)
                if ok:
                    matched = method
                    blocking.extend(extra)
                    notes.append(f"pay with your {method.describe()}")
                    break
            if matched is None:
                reasons.append(f"none of your saved cards match (requires {needed})")

    if offer.emi_rule is EmiRule.EMI_ONLY:
        (notes if profile.prefers_emi else blocking).append("requires an EMI transaction")
    elif offer.emi_rule is EmiRule.NON_EMI_ONLY:
        (blocking if profile.prefers_emi else notes).append("full payment only (not on EMI)")
    elif offer.emi_rule is EmiRule.UNKNOWN and offer.payment is not None:
        notes.append("EMI / full-payment applicability not stated")

    if offer.membership_required:
        if offer.membership_required in profile.memberships:
            notes.append(f"requires {offer.membership_required} (you have it)")
        else:
            blocking.append(f"requires {offer.membership_required} membership")

    if offer.type is OfferType.EXCHANGE:
        blocking.append("requires exchanging an eligible device; value depends on its condition")
    if offer.timing is BenefitTiming.DEFERRED:
        notes.append("cashback is credited later, not deducted at checkout")
    if offer.verification.value == "unverified":
        blocking.append("offer not verified from a reliable source")
    if offer.stale:
        notes.append("offer information may be out of date")

    if reasons:
        status = EligibilityStatus.INELIGIBLE
    elif blocking:
        status = EligibilityStatus.CONDITIONAL
    else:
        status = EligibilityStatus.ELIGIBLE
    return Eligibility(
        offer_id=offer.id,
        status=status,
        reasons=reasons,
        conditions=blocking + notes,
        payment_method=matched,
    )


def offer_benefit(offer: Offer, base: Decimal) -> tuple[Decimal, Decimal] | None:
    """(conservative, best-case) benefit on `base`, or None if the minimum order isn't met."""
    if offer.min_order_value is not None and base < offer.min_order_value:
        return None
    if offer.type is OfferType.EXCHANGE:
        assert offer.exchange is not None
        low = (offer.exchange.min_value or ZERO) + offer.exchange.bonus
        high = offer.exchange.max_value + offer.exchange.bonus
        return money(min(low, base)), money(min(high, base))
    if offer.flat_amount is not None:
        amount = offer.flat_amount
    else:
        assert offer.percent is not None
        amount = base * offer.percent / _HUNDRED
    if offer.max_discount is not None:
        amount = min(amount, offer.max_discount)
    amount = money(min(amount, base))
    return amount, amount


def pair_status(a: Offer, b: Offer) -> tuple[PairStatus, str]:
    if a.stacking.exclusive or b.stacking.exclusive:
        which = a if a.stacking.exclusive else b
        return PairStatus.INCOMPATIBLE, f"'{which.title}' cannot be combined with other offers"
    if a.type is b.type and a.type in ONE_PER_ORDER:
        return PairStatus.INCOMPATIBLE, f"only one {a.type.value} offer applies per order"
    if a.platform and b.platform and a.platform is not b.platform:
        return PairStatus.INCOMPATIBLE, "offers are on different platforms"
    if (
        a.payment
        and b.payment
        and a.payment.issuer
        and b.payment.issuer
        and normalize_issuer(a.payment.issuer) != normalize_issuer(b.payment.issuer)
    ):
        return PairStatus.INCOMPATIBLE, "offers need different payment cards"
    if b.type in a.stacking.combinable_with or a.type in b.stacking.combinable_with:
        return PairStatus.COMBINABLE, "terms state these can be combined"
    return PairStatus.UNKNOWN, "terms don't say whether these combine; not combined"


def build_stacking_matrix(offers: list[Offer]) -> StackingMatrix:
    pairs = []
    for a, b in combinations(offers, 2):
        status, reason = pair_status(a, b)
        pairs.append(StackingPair(a=a.id, b=b.id, status=status, reason=reason))
    return StackingMatrix(offer_ids=[o.id for o in offers], pairs=pairs)


class ExcludedOffer(BaseModel):
    offer_id: str
    title: str
    reasons: list[str]


class ScenarioSet(BaseModel):
    listing_id: str
    scenarios: list[PriceScenario]
    eligibility: list[Eligibility]
    matrix: StackingMatrix
    excluded: list[ExcludedOffer] = Field(default_factory=list)

    @property
    def best(self) -> PriceScenario:
        return min(self.scenarios, key=lambda s: (s.net_after_cashback, s.upfront_conservative))


def _apply(
    listing: Listing, subset: tuple[Offer, ...], elig: dict[str, Eligibility]
) -> PriceScenario | None:
    ordered = sorted(subset, key=lambda o: APPLICATION_ORDER[o.type])
    order_value = listing.price
    exchange_low = exchange_high = deferred = ZERO
    applied: list[AppliedOffer] = []
    for offer in ordered:
        if offer.type is OfferType.EXCHANGE:
            benefit = offer_benefit(offer, order_value)
            if benefit is None:
                return None
            exchange_low, exchange_high = benefit
        else:
            benefit = offer_benefit(offer, order_value)
            if benefit is None:
                return None
            if offer.timing is BenefitTiming.DEFERRED:
                deferred += benefit[0]
            else:
                order_value -= benefit[0]
        applied.append(
            AppliedOffer(
                offer_id=offer.id,
                type=offer.type,
                title=offer.title,
                benefit_low=benefit[0],
                benefit_high=benefit[1],
                timing=offer.timing,
                verification=offer.verification,
            )
        )
    conservative = money(order_value - exchange_low)
    conditions: list[str] = []
    for offer in ordered:
        for c in elig[offer.id].conditions:
            if c not in conditions:
                conditions.append(c)
    return PriceScenario(
        label="",
        name="",
        listing_id=listing.id,
        base_price=listing.price,
        applied=applied,
        upfront_best=money(order_value - exchange_high),
        upfront_conservative=conservative,
        deferred_cashback=money(deferred),
        net_after_cashback=money(conservative - deferred),
        conditional=any(elig[o.id].status is EligibilityStatus.CONDITIONAL for o in ordered),
        conditions=conditions,
        verification=weakest([o.verification for o in ordered]),
        stale=any(o.stale for o in ordered),
    )


def _key(s: PriceScenario) -> tuple[Decimal, Decimal]:
    return s.net_after_cashback, s.upfront_conservative


def _best_case_key(s: PriceScenario) -> tuple[Decimal, Decimal]:
    return s.upfront_best - s.deferred_cashback, s.net_after_cashback


def build_scenarios(
    listing: Listing,
    offers: list[Offer],
    profile: UserProfile,
    *,
    now: datetime,
    category: str | None = None,
    max_offers: int = 10,
) -> ScenarioSet:
    ctx = OfferContext(
        platform=listing.marketplace, listing_price=listing.price, now=now, category=category
    )
    eligibility = [check_eligibility(o, ctx, profile) for o in offers]
    elig = {e.offer_id: e for e in eligibility}
    excluded = [
        ExcludedOffer(offer_id=o.id, title=o.title, reasons=elig[o.id].reasons)
        for o in offers
        if elig[o.id].status is EligibilityStatus.INELIGIBLE
    ]
    usable = [o for o in offers if elig[o.id].status is not EligibilityStatus.INELIGIBLE]

    def standalone(o: Offer) -> Decimal:
        b = offer_benefit(o, listing.price)
        return b[1] if b else ZERO

    usable = sorted(usable, key=standalone, reverse=True)[:max_offers]
    matrix = build_stacking_matrix(usable)

    candidates: list[PriceScenario] = []
    for size in range(1, len(usable) + 1):
        for subset in combinations(usable, size):
            if any(
                matrix.status(a.id, b.id) is not PairStatus.COMBINABLE
                for a, b in combinations(subset, 2)
            ):
                continue
            scenario = _apply(listing, subset, elig)
            if scenario is not None:
                candidates.append(scenario)

    base = PriceScenario(
        label="A",
        name="Standard price",
        listing_id=listing.id,
        base_price=listing.price,
        upfront_best=listing.price,
        upfront_conservative=listing.price,
        net_after_cashback=listing.price,
    )
    scenarios = [base]
    best_so_far = _key(base)

    unconditional = [c for c in candidates if not c.conditional]
    singles = [c for c in unconditional if len(c.applied) == 1]
    combos = [c for c in unconditional if len(c.applied) > 1]
    conditional = [c for c in candidates if c.conditional]

    for label, name, pool in (
        ("B", "Best single eligible offer", singles),
        ("C", "Best eligible combination", combos),
    ):
        if not pool:
            continue
        top = min(pool, key=_key)
        if _key(top) < best_so_far:
            scenarios.append(top.model_copy(update={"label": label, "name": name}))
            best_so_far = _key(top)

    # Conditional scenarios (exchange, EMI, membership, unconfirmed eligibility) are shown when
    # their best case beats the best unconditional price — always with their range and conditions.
    if conditional:
        top = min(conditional, key=_best_case_key)
        if _best_case_key(top)[0] < best_so_far[0]:
            scenarios.append(
                top.model_copy(update={"label": "D", "name": "Best price with conditions"})
            )

    return ScenarioSet(
        listing_id=listing.id,
        scenarios=scenarios,
        eligibility=eligibility,
        matrix=matrix,
        excluded=excluded,
    )


class CardRow(BaseModel):
    payment: str
    best: PriceScenario


def card_matrix(
    listing: Listing,
    offers: list[Offer],
    profile: UserProfile,
    *,
    now: datetime,
    category: str | None = None,
) -> list[CardRow]:
    """Best *eligible* (non-conditional) effective price for each of the user's payment methods."""
    rows: list[CardRow] = []
    no_card = [o for o in offers if o.payment is None]
    plain = build_scenarios(
        listing,
        no_card,
        profile.model_copy(update={"payment_methods": []}),
        now=now,
        category=category,
    )
    rows.append(CardRow(payment="Without card offers", best=_best_unconditional(plain)))
    for method in profile.payment_methods:
        single = profile.model_copy(update={"payment_methods": [method]})
        relevant = no_card + [
            o for o in offers if o.payment is not None and _method_matches(o, method)[0]
        ]
        result = build_scenarios(listing, relevant, single, now=now, category=category)
        rows.append(CardRow(payment=method.describe(), best=_best_unconditional(result)))
    return rows


def _best_unconditional(result: ScenarioSet) -> PriceScenario:
    pool = [s for s in result.scenarios if not s.conditional]
    return min(pool, key=_key)


class EmiCost(BaseModel):
    price: Decimal
    tenure_months: int
    annual_rate_pct: Decimal
    monthly_installment: Decimal
    interest_component: Decimal  # given back by the merchant as an upfront discount
    gst_on_interest: Decimal
    processing_fee_total: Decimal
    discount_forgone: Decimal
    true_cost: Decimal
    extra_vs_full_payment: Decimal


def no_cost_emi_true_cost(
    price: Decimal,
    tenure_months: int,
    annual_rate_pct: Decimal,
    *,
    processing_fee: Decimal = ZERO,
    gst_rate_pct: Decimal = Decimal(18),
    discount_forgone: Decimal = ZERO,
) -> EmiCost:
    """Real cost of a 'No-Cost EMI'.

    The merchant discounts the interest upfront so installments total `price`, but the bank
    still charges GST on the interest portion and a processing fee (+GST). Any instant
    discount available only on full payment is also lost.
    """
    if tenure_months <= 0:
        raise ValueError("tenure_months must be positive")
    r = annual_rate_pct / Decimal(1200)
    n = tenure_months
    if r == 0:
        factor = Decimal(1) / n
    else:
        growth = (1 + r) ** n
        factor = r * growth / (growth - 1)
    principal = price / (n * factor)
    interest = price - principal
    gst = interest * gst_rate_pct / _HUNDRED
    fee_total = processing_fee * (1 + gst_rate_pct / _HUNDRED)
    extra = gst + fee_total + discount_forgone
    return EmiCost(
        price=money(price),
        tenure_months=n,
        annual_rate_pct=annual_rate_pct,
        monthly_installment=money(price / n),
        interest_component=money(interest),
        gst_on_interest=money(gst),
        processing_fee_total=money(fee_total),
        discount_forgone=money(discount_forgone),
        true_cost=money(price + extra),
        extra_vs_full_payment=money(extra),
    )
