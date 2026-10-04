from datetime import UTC, datetime
from decimal import Decimal

from shopping_agent.agent.analyzers import (
    budget_text,
    find_links,
    merge_profile,
    to_offer,
    to_requirements,
)
from shopping_agent.agent.facts import ground_answer
from shopping_agent.agent.schemas import (
    ExtractedOffer,
    FinalAnswer,
    Recommendation,
    RequestAnalysis,
)
from shopping_agent.agent.state import AgentState
from shopping_agent.domain.common import Confidence, SourceType, Verification
from shopping_agent.domain.evidence import Evidence
from shopping_agent.domain.offer import CardType, EmiRule, OfferType, PaymentMethod, UserProfile
from shopping_agent.domain.product import Availability, Condition, Marketplace, VariantAttributes
from shopping_agent.tools.common import ListingSummary
from tests.fakes import analysis_json

NOW = datetime(2026, 10, 3, tzinfo=UTC)


def analysis(**overrides: object) -> RequestAnalysis:
    return RequestAnalysis.model_validate_json(analysis_json(**overrides))


def test_links_are_found() -> None:
    text = "compare https://www.amazon.in/dp/B0ABCDEF12?tag=x and flipkart.com/x/p/itm1?pid=A, ok"
    assert find_links(text) == [
        "https://www.amazon.in/dp/B0ABCDEF12?tag=x",
        "flipkart.com/x/p/itm1?pid=A",
    ]


def test_budget_comes_from_users_words_not_offer_text() -> None:
    req = "laptop under 80k. Page says 'Flat Rs 3,000 off on HDFC cards above Rs 40,000'"
    assert "3,000" not in budget_text(req)
    requirements = to_requirements(analysis(), req)
    assert requirements.budget.maximum == Decimal("80000.00")


def test_no_budget_when_model_finds_none() -> None:
    req = "Is this a good deal? The offer is 'up to Rs 3,000 off'"
    result = to_requirements(
        analysis(budget={"minimum": None, "maximum": None, "target": None, "approximate": False}),
        req,
    )
    assert result.budget.maximum is None


def test_model_budget_used_when_text_has_no_amount() -> None:
    result = to_requirements(analysis(), "a good laptop within eighty thousand")
    assert result.budget.maximum == Decimal("80000.00")


def test_alert_target_is_not_a_budget() -> None:
    req = "Track the Nova Swift 16 and alert me if it drops below 60k"
    tracked = to_requirements(
        analysis(
            intent="track",
            target_price=60000,
            budget={"minimum": None, "maximum": 60000, "target": None, "approximate": False},
        ),
        req,
    )
    assert tracked.budget.maximum is None
    both = to_requirements(
        analysis(target_price=60000),  # budget 80000 from the model differs from the target
        "laptop under 80k, alert me below 60k",
    )
    assert both.budget.maximum is not None


def test_constraints_converted() -> None:
    result = to_requirements(
        analysis(
            hard_constraints=[{"attribute": "ram_gb", "op": "gte", "value": 16, "note": None}],
            strong_preferences=[
                {"attribute": "battery", "op": "qualitative", "value": "long", "note": None}
            ],
        ),
        "x",
    )
    assert result.hard_constraints[0].attribute == "ram_gb"
    assert result.strong_preferences[0].op.value == "qualitative"
    assert result.category == "laptop"


def test_profile_merge() -> None:
    base = UserProfile(payment_methods=[PaymentMethod(issuer="HDFC", card_type=CardType.CREDIT)])
    merged = merge_profile(
        base,
        analysis(
            payment_methods=[
                {"issuer": "hdfc", "card_type": "credit"},
                {"issuer": "SBI", "card_type": "debit"},
                {"issuer": "Axis", "card_type": "any"},
            ],
            memberships=["Flipkart_Plus"],
            prefers_emi=True,
        ),
    )
    assert [m.describe() for m in merged.payment_methods] == ["HDFC credit card", "SBI debit card"]
    assert merged.memberships == {"flipkart_plus"}
    assert merged.prefers_emi is True


def extracted(**overrides: object) -> ExtractedOffer:
    data: dict[str, object] = {
        "type": "bank",
        "title": "10% off ICICI",
        "provider": "ICICI",
        "flat_amount": None,
        "percent": 10,
        "max_discount": 3000,
        "min_order_value": 40000,
        "issuer": "ICICI",
        "card_type": "credit",
        "network": None,
        "emi_rule": "emi_only",
        "membership_required": None,
        "exchange_max_value": None,
        "exchange_min_value": None,
        "exchange_bonus": None,
        "combinable_with": [],
        "exclusive": False,
        "cashback_deferred": False,
        "valid_from": None,
        "valid_to": "2026-10-10",
    }
    data.update(overrides)
    return ExtractedOffer.model_validate(data)


def test_offer_conversion() -> None:
    offer = to_offer(
        extracted(), platform=Marketplace.FLIPKART, verification=Verification.USER_PROVIDED
    )
    assert offer is not None
    assert offer.percent == Decimal("10.00")
    assert offer.payment is not None
    assert offer.payment.issuer == "ICICI"
    assert offer.emi_rule is EmiRule.EMI_ONLY
    assert offer.valid_to is not None
    assert offer.valid_to.tzinfo is not None
    assert offer.verification is Verification.USER_PROVIDED


def test_percent_offer_without_cap_is_unverified() -> None:
    offer = to_offer(
        extracted(max_discount=None), platform=None, verification=Verification.USER_PROVIDED
    )
    assert offer is not None
    assert offer.verification is Verification.UNVERIFIED


def test_incomplete_exchange_offer_is_dropped() -> None:
    item = extracted(type="exchange", percent=None, max_discount=None, issuer=None)
    assert to_offer(item, platform=None, verification=Verification.USER_PROVIDED) is None
    item = extracted(
        type="exchange", percent=None, issuer=None, exchange_max_value=15000, exchange_bonus=2000
    )
    offer = to_offer(item, platform=None, verification=Verification.USER_PROVIDED)
    assert offer is not None
    assert offer.type is OfferType.EXCHANGE
    assert offer.exchange is not None
    assert offer.exchange.max_value == Decimal("15000.00")


def _state() -> AgentState:
    state = AgentState(created_at=NOW, request="x")
    state.listings["L1"] = ListingSummary(
        listing_id="L1",
        product_id=None,
        variant_id=None,
        marketplace=Marketplace.FLIPKART,
        seller="S",
        title="Nova Swift 16 RTX 4060",
        price=Decimal("68990.00"),
        mrp=None,
        availability=Availability.IN_STOCK,
        condition=Condition.NEW,
        attributes=VariantAttributes(),
        rating=None,
        rating_count=None,
        url=None,
        observed_at=NOW,
        stale=False,
        source="t",
        verification=Verification.SOURCE_REPORTED,
    )
    state.evidence["E1"] = Evidence(
        id="E1",
        claim="c",
        source_type=SourceType.MARKETPLACE_LISTING,
        source_name="s",
        retrieved_at=NOW,
        verification=Verification.SOURCE_REPORTED,
        confidence=Confidence.MEDIUM,
    )
    return state


def _answer(summary: str, listing_id: str = "L1") -> FinalAnswer:
    return FinalAnswer(
        summary=summary,
        recommendations=[
            Recommendation(
                listing_id=listing_id,
                product="Nova Swift 16",
                verdict="top_pick",
                price_summary="₹68,990 listed",
                why=["RTX 4060 graphics"],
                trade_offs=[],
                evidence_ids=["E1", "E-unknown"],
            )
        ],
        deal_insights=[],
        timing=None,
        risks_and_conflicts=[],
        uncertainties=[],
        questions_for_user=[],
        suggested_actions=[],
    )


def test_grounding_flags_invented_prices_only() -> None:
    state = _state()
    answer, report = ground_answer(
        state,
        _answer("Get it for ₹59,999 today."),
        facts_text='{"product": "Nova Swift 16 RTX 4060"}',
    )
    assert [m.raw for m in report.ungrounded] == ["₹59,999"]
    assert "[unverified]" in answer.summary
    assert (
        "RTX 4060 graphics" in answer.recommendations[0].why[0]
    )  # model numbers from facts are fine
    assert answer.recommendations[0].evidence_ids == ["E1"]
    assert any("[unverified]" in u for u in answer.uncertainties)


def test_grounding_drops_unknown_listings() -> None:
    state = _state()
    answer, report = ground_answer(state, _answer("ok", listing_id="made-up"))
    assert answer.recommendations == []
    assert report.ok
    assert any("unknown listing id" in w for w in state.warnings)
