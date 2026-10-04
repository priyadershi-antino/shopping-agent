from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from shopping_agent.domain.common import Verification
from shopping_agent.domain.offer import (
    CardType,
    EligibilityStatus,
    EmiRule,
    Offer,
    OfferType,
    PairStatus,
    PaymentRequirement,
    StackingTerms,
    UserProfile,
)
from shopping_agent.domain.product import Marketplace
from shopping_agent.services.offer_engine import (
    OfferContext,
    build_scenarios,
    build_stacking_matrix,
    card_matrix,
    check_eligibility,
    no_cost_emi_true_cost,
    offer_benefit,
    pair_status,
)
from tests.factories import NOW, D, bank_offer, cashback, coupon, exchange, listing, profile

HDFC_CREDIT = ("HDFC", CardType.CREDIT)
SBI_DEBIT = ("SBI", CardType.DEBIT)
CTX = OfferContext(
    platform=Marketplace.FLIPKART, listing_price=D(79999), now=NOW, category="laptop"
)


# --- benefit maths ---


def test_percent_benefit_is_capped() -> None:
    assert offer_benefit(bank_offer(), D(79999)) == (D("4000.00"), D("4000.00"))


def test_percent_benefit_without_cap() -> None:
    assert offer_benefit(bank_offer(cap=None, percent="5"), D(79999)) == (D("3999.95"),) * 2


def test_minimum_order_value() -> None:
    offer = bank_offer(percent=None, flat="3000", cap=None, min_order_value=D(50000))
    assert offer_benefit(offer, D(79999)) == (D(3000), D(3000))
    assert offer_benefit(offer, D(40000)) is None


def test_exchange_benefit_is_a_range() -> None:
    assert offer_benefit(exchange(), D(79999)) == (D(5000), D(17000))


def test_benefit_never_exceeds_price() -> None:
    assert offer_benefit(coupon("5000"), D(3000)) == (D(3000), D(3000))


# --- offer validation ---


def test_offer_requires_exactly_one_benefit_kind() -> None:
    with pytest.raises(ValidationError):
        Offer(type=OfferType.COUPON, title="x", verification=Verification.VERIFIED)
    with pytest.raises(ValidationError):
        Offer(
            type=OfferType.COUPON,
            title="x",
            flat_amount=D(1),
            percent=D(1),
            verification=Verification.VERIFIED,
        )
    with pytest.raises(ValidationError):
        Offer(type=OfferType.EXCHANGE, title="x", verification=Verification.VERIFIED)


# --- eligibility ---


def test_matching_card_is_eligible() -> None:
    result = check_eligibility(bank_offer(), CTX, profile(HDFC_CREDIT))
    assert result.status is EligibilityStatus.ELIGIBLE
    assert result.payment_method is not None
    assert any("HDFC" in c for c in result.conditions)


def test_issuer_names_are_normalised() -> None:
    offer = bank_offer(
        payment=PaymentRequirement(issuer="HDFC Bank Ltd", card_type=CardType.CREDIT)
    )
    assert check_eligibility(offer, CTX, profile(("hdfc", CardType.CREDIT))).status is (
        EligibilityStatus.ELIGIBLE
    )


def test_no_matching_card_is_ineligible() -> None:
    result = check_eligibility(bank_offer(), CTX, profile(SBI_DEBIT))
    assert result.status is EligibilityStatus.INELIGIBLE
    assert "none of your saved cards" in result.reasons[0]


def test_debit_card_does_not_satisfy_credit_offer() -> None:
    assert check_eligibility(bank_offer(), CTX, profile(("HDFC", CardType.DEBIT))).status is (
        EligibilityStatus.INELIGIBLE
    )


def test_unknown_payment_profile_is_conditional() -> None:
    result = check_eligibility(bank_offer(), CTX, UserProfile())
    assert result.status is EligibilityStatus.CONDITIONAL
    assert "requires HDFC credit card" in result.conditions


def test_network_requirement_unconfirmed_is_conditional() -> None:
    offer = bank_offer(
        payment=PaymentRequirement(issuer="HDFC", card_type=CardType.CREDIT, network="RuPay")
    )
    assert (
        check_eligibility(offer, CTX, profile(HDFC_CREDIT)).status is EligibilityStatus.CONDITIONAL
    )


def test_expired_and_future_offers() -> None:
    expired = bank_offer(valid_to=NOW - timedelta(days=1))
    future = bank_offer(valid_from=NOW + timedelta(days=5))
    assert (
        check_eligibility(expired, CTX, profile(HDFC_CREDIT)).status is EligibilityStatus.INELIGIBLE
    )
    assert (
        check_eligibility(future, CTX, profile(HDFC_CREDIT)).status is EligibilityStatus.CONDITIONAL
    )


def test_platform_category_and_min_order_rules() -> None:
    p = profile(HDFC_CREDIT)
    assert check_eligibility(bank_offer(platform=Marketplace.AMAZON), CTX, p).status is (
        EligibilityStatus.INELIGIBLE
    )
    assert check_eligibility(bank_offer(category_exclusions=["laptop"]), CTX, p).status is (
        EligibilityStatus.INELIGIBLE
    )
    assert check_eligibility(bank_offer(applicable_categories=["mobile"]), CTX, p).status is (
        EligibilityStatus.INELIGIBLE
    )
    assert check_eligibility(bank_offer(min_order_value=D(90000)), CTX, p).status is (
        EligibilityStatus.INELIGIBLE
    )


def test_emi_only_offer_depends_on_preference() -> None:
    offer = bank_offer(emi_rule=EmiRule.EMI_ONLY)
    assert (
        check_eligibility(offer, CTX, profile(HDFC_CREDIT)).status is EligibilityStatus.CONDITIONAL
    )
    assert check_eligibility(offer, CTX, profile(HDFC_CREDIT, prefers_emi=True)).status is (
        EligibilityStatus.ELIGIBLE
    )


def test_membership_requirement() -> None:
    offer = coupon(membership_required="flipkart_plus")
    assert check_eligibility(offer, CTX, UserProfile()).status is EligibilityStatus.CONDITIONAL
    assert check_eligibility(offer, CTX, UserProfile(memberships={"flipkart_plus"})).status is (
        EligibilityStatus.ELIGIBLE
    )


def test_exchange_and_unverified_offers_are_conditional() -> None:
    assert check_eligibility(exchange(), CTX, UserProfile()).status is EligibilityStatus.CONDITIONAL
    assert check_eligibility(
        coupon(verification=Verification.UNVERIFIED), CTX, UserProfile()
    ).status is (EligibilityStatus.CONDITIONAL)


def test_user_provided_offer_is_usable() -> None:
    assert check_eligibility(
        coupon(verification=Verification.USER_PROVIDED), CTX, UserProfile()
    ).status is (EligibilityStatus.ELIGIBLE)


# --- stacking ---


def test_two_bank_offers_never_stack() -> None:
    status, reason = pair_status(bank_offer("HDFC"), bank_offer("HDFC", percent="5"))
    assert status is PairStatus.INCOMPATIBLE
    assert "one bank offer" in reason


def test_exclusive_offer_is_incompatible() -> None:
    status, _ = pair_status(coupon(stacking=StackingTerms(exclusive=True)), bank_offer())
    assert status is PairStatus.INCOMPATIBLE


def test_declared_combinable() -> None:
    status, _ = pair_status(bank_offer(combinable_with=frozenset({OfferType.COUPON})), coupon())
    assert status is PairStatus.COMBINABLE


def test_silence_means_unknown() -> None:
    status, reason = pair_status(bank_offer(), coupon())
    assert status is PairStatus.UNKNOWN
    assert "not combined" in reason


def test_different_platforms_incompatible() -> None:
    status, _ = pair_status(
        coupon(platform=Marketplace.AMAZON), coupon(platform=Marketplace.FLIPKART)
    )
    assert status is PairStatus.INCOMPATIBLE


def test_matrix_covers_every_pair() -> None:
    matrix = build_stacking_matrix([bank_offer(), coupon(), exchange()])
    assert len(matrix.pairs) == 3


# --- scenarios ---


def test_full_scenario_set() -> None:
    bank = bank_offer(combinable_with=frozenset({OfferType.COUPON}))
    result = build_scenarios(
        listing(),
        [bank, coupon(), exchange(), cashback()],
        profile(HDFC_CREDIT),
        now=NOW,
        category="laptop",
    )
    by_label = {s.label: s for s in result.scenarios}
    assert list(by_label) == ["A", "B", "C", "D"]

    assert by_label["A"].upfront_conservative == D(79999)
    assert by_label["B"].upfront_conservative == D("75999.00")  # bank offer alone
    # Coupon first (2,000), then 10% bank on 77,999 capped at 4,000.
    c = by_label["C"]
    assert c.upfront_conservative == D("73999.00")
    assert [a.type for a in c.applied] == [OfferType.COUPON, OfferType.BANK]
    assert not c.conditional

    d = by_label["D"]  # exchange: range, conditional, conditions listed
    assert d.conditional
    assert d.upfront_best == D("62999.00")
    assert d.upfront_conservative == D("74999.00")
    assert d.is_range
    assert any("exchang" in cond for cond in d.conditions)


def test_unknown_stackability_prevents_combining() -> None:
    result = build_scenarios(listing(), [bank_offer(), coupon()], profile(HDFC_CREDIT), now=NOW)
    assert all(len(s.applied) <= 1 for s in result.scenarios)
    assert result.best.upfront_conservative == D("75999.00")


def test_minimum_order_value_checked_at_point_of_application() -> None:
    bank = bank_offer(min_order_value=D(78000), combinable_with=frozenset({OfferType.COUPON}))
    result = build_scenarios(listing(), [bank, coupon()], profile(HDFC_CREDIT), now=NOW)
    # After the 2,000 coupon the order is 77,999 < 78,000, so the combination is invalid.
    assert all(len(s.applied) <= 1 for s in result.scenarios)


def test_deferred_cashback_is_not_deducted_upfront() -> None:
    result = build_scenarios(listing(), [cashback()], UserProfile(), now=NOW)
    b = next(s for s in result.scenarios if s.label == "B")
    assert b.upfront_conservative == D(79999)
    assert b.deferred_cashback == D("1500.00")
    assert b.net_after_cashback == D("78499.00")


def test_ineligible_offers_are_excluded_with_reasons() -> None:
    result = build_scenarios(listing(), [bank_offer("ICICI")], profile(HDFC_CREDIT), now=NOW)
    assert [s.label for s in result.scenarios] == ["A"]
    assert result.excluded[0].reasons


def test_conditional_bank_offer_shows_as_scenario_d_only() -> None:
    result = build_scenarios(listing(), [bank_offer()], UserProfile(), now=NOW)
    labels = [s.label for s in result.scenarios]
    assert labels == ["A", "D"]
    assert "requires HDFC credit card" in result.scenarios[1].conditions


def test_scenario_verification_is_weakest_used() -> None:
    result = build_scenarios(
        listing(), [coupon(verification=Verification.USER_PROVIDED)], UserProfile(), now=NOW
    )
    assert result.scenarios[-1].verification is Verification.USER_PROVIDED


def test_card_matrix_rows() -> None:
    offers = [
        bank_offer("HDFC"),
        bank_offer("SBI", percent="5", cap="1000", card_type=CardType.DEBIT),
    ]
    rows = card_matrix(listing(), offers, profile(HDFC_CREDIT, SBI_DEBIT), now=NOW)
    prices = {r.payment: r.best.upfront_conservative for r in rows}
    assert prices == {
        "Without card offers": D(79999),
        "HDFC credit card": D("75999.00"),
        "SBI debit card": D("78999.00"),
    }


# --- EMI ---


def test_no_cost_emi_true_cost() -> None:
    cost = no_cost_emi_true_cost(
        D(60000), 6, D(15), processing_fee=D(199), discount_forgone=D(1500)
    )
    assert D(2500) < cost.interest_component < D(2600)
    assert cost.gst_on_interest == (cost.interest_component * D("0.18")).quantize(D("0.01"))
    assert cost.processing_fee_total == D("234.82")
    assert cost.monthly_installment == D("10000.00")
    expected_extra = cost.gst_on_interest + cost.processing_fee_total + D(1500)
    assert abs(cost.extra_vs_full_payment - expected_extra) <= D("0.01")
    assert cost.true_cost == D(60000) + cost.extra_vs_full_payment


def test_zero_rate_emi_has_no_interest() -> None:
    cost = no_cost_emi_true_cost(D(60000), 12, Decimal(0))
    assert cost.interest_component == D(0)
    assert cost.true_cost == D(60000)


def test_emi_rejects_invalid_tenure() -> None:
    with pytest.raises(ValueError, match="tenure"):
        no_cost_emi_true_cost(D(60000), 0, D(15))


def test_issuer_matching_handles_co_brands_and_aliases() -> None:
    from shopping_agent.services.offer_engine import issuer_matches

    assert issuer_matches("Axis", "Flipkart Axis Bank")  # co-branded card counts for its bank
    assert issuer_matches("ICICI Bank", "Amazon Pay ICICI")
    assert not issuer_matches("Flipkart Axis", "Axis Bank")  # co-brand-only offer, plain card
    assert issuer_matches("SBI", "State Bank of India")
    assert issuer_matches("HDFC", "hdfc bank credit card")
    assert not issuer_matches("HDFC", "ICICI")
    assert issuer_matches(None, "anything")
