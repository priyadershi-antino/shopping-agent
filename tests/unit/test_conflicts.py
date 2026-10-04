from shopping_agent.domain.common import Confidence, SourceType, Verification
from shopping_agent.domain.evidence import Evidence, EvidenceValue
from shopping_agent.services.conflicts import ClaimKind, detect_conflicts, source_priority
from tests.factories import NOW


def ev(value: EvidenceValue, source: SourceType, name: str, attribute: str = "ram_gb") -> Evidence:
    return Evidence(
        claim=f"{attribute} is {value}",
        subject="V1",
        attribute=attribute,
        value=value,
        source_type=source,
        source_name=name,
        retrieved_at=NOW,
        verification=Verification.VERIFIED,
        confidence=Confidence.HIGH,
    )


def test_manufacturer_preferred_but_conflict_still_reported() -> None:
    conflicts = detect_conflicts(
        [
            ev(16, SourceType.MANUFACTURER, "HP India"),
            ev("8", SourceType.MARKETPLACE_LISTING, "Retailer X"),
        ]
    )
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c.resolved
    assert c.preferred_value == "16"
    assert "HP India" in c.explanation and "Retailer X" in c.explanation
    assert len(c.evidence_ids) == 2


def test_equal_priority_sources_stay_unresolved() -> None:
    (c,) = detect_conflicts(
        [
            ev(16, SourceType.MARKETPLACE_LISTING, "Amazon"),
            ev(8, SourceType.MARKETPLACE_LISTING, "Flipkart"),
        ]
    )
    assert not c.resolved
    assert c.preferred_value is None


def test_same_value_in_different_formats_is_not_a_conflict() -> None:
    assert (
        detect_conflicts(
            [ev(16, SourceType.MANUFACTURER, "HP"), ev("16", SourceType.MARKETPLACE_LISTING, "A")]
        )
        == []
    )


def test_different_attributes_do_not_conflict() -> None:
    assert (
        detect_conflicts(
            [
                ev(16, SourceType.MANUFACTURER, "HP"),
                ev(512, SourceType.MANUFACTURER, "HP", attribute="storage_gb"),
            ]
        )
        == []
    )


def test_offer_claims_prefer_official_platform_sources() -> None:
    assert source_priority(SourceType.MARKETPLACE_OFFICIAL, ClaimKind.OFFER) > source_priority(
        SourceType.NEWS, ClaimKind.OFFER
    )
    assert source_priority(SourceType.USER_REVIEW, ClaimKind.SPEC) < source_priority(
        SourceType.MANUFACTURER, ClaimKind.SPEC
    )
