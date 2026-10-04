"""Conflict detection across sources, with an explicit source-priority policy.

Both observations are always kept. A preferred value is suggested only when a strictly
higher-priority source supports it; otherwise the conflict stays unresolved.
"""

from collections import defaultdict
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from pydantic import BaseModel

from shopping_agent.domain.common import SourceType
from shopping_agent.domain.evidence import Evidence, EvidenceValue


class ClaimKind(StrEnum):
    SPEC = "spec"  # official specifications
    OFFER = "offer"  # offers, sale dates, terms
    PRICE = "price"


_PRIORITY: dict[ClaimKind, dict[SourceType, int]] = {
    ClaimKind.SPEC: {
        SourceType.MANUFACTURER: 100,
        SourceType.MARKETPLACE_OFFICIAL: 60,
        SourceType.REVIEW_SITE: 55,
        SourceType.MARKETPLACE_LISTING: 50,
        SourceType.USER_INPUT: 45,
        SourceType.NEWS: 40,
        SourceType.SEARCH_RESULT: 30,
        SourceType.USER_REVIEW: 10,
    },
    ClaimKind.OFFER: {
        SourceType.MARKETPLACE_OFFICIAL: 100,
        SourceType.BANK_OFFICIAL: 100,
        SourceType.USER_INPUT: 70,  # the user is looking at the product page
        SourceType.MARKETPLACE_LISTING: 60,
        SourceType.NEWS: 40,
        SourceType.SEARCH_RESULT: 30,
    },
    ClaimKind.PRICE: {
        SourceType.MARKETPLACE_LISTING: 100,
        SourceType.PRICE_TRACKING: 90,
        SourceType.USER_INPUT: 70,
        SourceType.SEARCH_RESULT: 40,
        SourceType.NEWS: 20,
    },
}


def source_priority(source_type: SourceType, kind: ClaimKind) -> int:
    return _PRIORITY[kind].get(source_type, 0)


class Conflict(BaseModel):
    subject: str | None
    attribute: str
    kind: ClaimKind
    evidence_ids: list[str]
    values: list[str]
    preferred_value: str | None
    preferred_evidence_id: str | None
    resolved: bool
    explanation: str


def _normalise(value: EvidenceValue) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int | float | Decimal):
        return str(Decimal(str(value)).normalize())
    text = str(value).strip().lower()
    try:
        return str(Decimal(text.replace(",", "")).normalize())
    except InvalidOperation:
        return " ".join(text.split())


def detect_conflicts(evidence: list[Evidence], kind: ClaimKind = ClaimKind.SPEC) -> list[Conflict]:
    by_key: dict[tuple[str | None, str], list[Evidence]] = defaultdict(list)
    for item in evidence:
        if item.attribute and item.value is not None:
            by_key[(item.subject, item.attribute)].append(item)

    conflicts: list[Conflict] = []
    for (subject, attribute), items in by_key.items():
        values = {_normalise(i.value) for i in items}
        if len(values) < 2:
            continue
        best_by_value: dict[str, tuple[int, Evidence]] = {}
        for item in items:
            value, prio = _normalise(item.value), source_priority(item.source_type, kind)
            if value not in best_by_value or prio > best_by_value[value][0]:
                best_by_value[value] = (prio, item)
        ranked = sorted(best_by_value.items(), key=lambda kv: kv[1][0], reverse=True)
        (top_value, (top_prio, top_item)), (_, (second_prio, _)) = ranked[0], ranked[1]
        resolved = top_prio > second_prio
        sources = ", ".join(
            f"{i.source_name} ({i.source_type.value}) says {_normalise(i.value)}" for i in items
        )
        explanation = f"Sources disagree on {attribute}: {sources}. " + (
            f"Preferring {top_item.source_name} as the higher-priority source for {kind.value} "
            "claims; the exact variant should still be confirmed."
            if resolved
            else "No source has clear priority; treat this value as unconfirmed."
        )
        conflicts.append(
            Conflict(
                subject=subject,
                attribute=attribute,
                kind=kind,
                evidence_ids=[i.id for i in items],
                values=sorted(values),
                preferred_value=top_value if resolved else None,
                preferred_evidence_id=top_item.id if resolved else None,
                resolved=resolved,
                explanation=explanation,
            )
        )
    return conflicts
