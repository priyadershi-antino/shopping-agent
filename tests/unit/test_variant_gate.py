from shopping_agent.domain.product import Condition, VariantAttributes
from shopping_agent.services.variant_gate import (
    GateDecision,
    RiskKind,
    check_variant,
    listing_risk_flags,
    suspiciously_cheap,
)
from tests.factories import D, listing

TARGET = VariantAttributes(ram_gb=16, storage_gb=512)


def test_exact_match_passes() -> None:
    assert check_variant(listing(), TARGET, target_model_number="15-FA1099TX").decision is (
        GateDecision.PASS
    )


def test_ram_mismatch_is_rejected() -> None:
    result = check_variant(listing(attributes=VariantAttributes(ram_gb=8, storage_gb=512)), TARGET)
    assert result.decision is GateDecision.REJECT
    assert result.mismatches[0].attribute == "ram_gb"
    assert "different variant" in result.reasons[0]


def test_unstated_attribute_passes_unverified() -> None:
    result = check_variant(listing(attributes=VariantAttributes(storage_gb=512)), TARGET)
    assert result.decision is GateDecision.PASS_UNVERIFIED
    assert result.unverified == ["ram_gb"]


def test_renewed_unit_rejected_by_default() -> None:
    result = check_variant(listing(condition=Condition.RENEWED), TARGET)
    assert result.decision is GateDecision.REJECT
    assert result.mismatches[0].attribute == "condition"


def test_renewed_allowed_when_user_accepts_it() -> None:
    result = check_variant(
        listing(condition=Condition.RENEWED),
        TARGET,
        allowed_conditions=frozenset({Condition.NEW, Condition.RENEWED}),
    )
    assert result.decision is GateDecision.PASS


def test_unknown_condition_is_unverified() -> None:
    result = check_variant(listing(condition=Condition.UNKNOWN), TARGET)
    assert result.decision is GateDecision.PASS_UNVERIFIED
    assert "condition" in result.unverified


def test_international_unit_rejected() -> None:
    attrs = VariantAttributes(ram_gb=16, storage_gb=512, region="international")
    assert check_variant(listing(attributes=attrs), TARGET).decision is GateDecision.REJECT


def test_model_number_mismatch_rejected() -> None:
    result = check_variant(
        listing(model_number="15-fb0000AX"), TARGET, target_model_number="15-fa1099TX"
    )
    assert result.decision is GateDecision.REJECT


def test_risk_flags() -> None:
    risky = listing(
        condition=Condition.REFURBISHED,
        seller_name=None,
        attributes=VariantAttributes(region="international"),
        price=D(40000),
    )
    kinds = {f.kind for f in listing_risk_flags(risky, peer_prices=[D(75000), D(76000), D(74000)])}
    assert kinds == {
        RiskKind.NOT_NEW,
        RiskKind.INTERNATIONAL_UNIT,
        RiskKind.UNKNOWN_SELLER,
        RiskKind.PRICE_ANOMALY,
    }


def test_unauthorised_seller_flag() -> None:
    flags = listing_risk_flags(
        listing(seller_name="Random Traders"), authorised_sellers={"RetailNet"}
    )
    assert [f.kind for f in flags] == [RiskKind.UNAUTHORISED_SELLER]
    assert listing_risk_flags(listing(), authorised_sellers={"RetailNet Pvt Ltd"}) == []


def test_no_anomaly_with_too_few_peers() -> None:
    assert listing_risk_flags(listing(price=D(10000)), peer_prices=[D(75000)]) == []


def test_suspiciously_cheap_compares_with_the_rest_of_the_shortlist() -> None:
    prices = {"a": D(47000), "b": D(60990), "c": D(65000), "d": D(70000), "bait": D(15500)}
    assert suspiciously_cheap(prices) == {"bait"}
    assert suspiciously_cheap({"a": D(47000), "bait": D(15500)}) == set()  # too few to judge
    assert suspiciously_cheap({k: D(50000) for k in "abcd"}) == set()
