"""Grounding guard: every price/percentage in a generated answer must trace to evidence or a
computed value. Ungrounded figures are reported so the caller can flag or remove them.
"""

import re
from collections.abc import Iterable
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

from shopping_agent.services.india_price_parser import find_amounts

_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_ROUNDED_TOLERANCE = Decimal("0.01")  # "₹75k" style mentions may round by up to 1%
_EXACT_TOLERANCE = Decimal(1)  # ₹1 for rounding to whole rupees
_PERCENT_TOLERANCE = Decimal("0.5")


class MentionKind(StrEnum):
    CURRENCY = "currency"
    PERCENT = "percent"
    NUMBER = "number"


class NumberMention(BaseModel):
    raw: str
    value: Decimal
    kind: MentionKind
    start: int
    end: int
    rounded: bool = False


class GroundingReport(BaseModel):
    checked: int
    ungrounded: list[NumberMention] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.ungrounded


def extract_mentions(text: str) -> list[NumberMention]:
    mentions: list[NumberMention] = []
    percent_spans = []
    for m in _PERCENT.finditer(text):
        percent_spans.append((m.start(), m.end()))
        mentions.append(
            NumberMention(
                raw=m.group(0),
                value=Decimal(m.group(1)),
                kind=MentionKind.PERCENT,
                start=m.start(),
                end=m.end(),
            )
        )
    for a in find_amounts(text):
        if any(s <= a.start < e for s, e in percent_spans):
            continue
        is_year = not a.strong and 2000 <= a.value <= 2039 and a.value == int(a.value)
        if a.strong:
            kind = MentionKind.CURRENCY
        elif a.value >= 1000 and not is_year:
            kind = MentionKind.NUMBER
        else:
            continue
        mentions.append(
            NumberMention(
                raw=text[a.start : a.end].strip(),
                value=a.value,
                kind=kind,
                start=a.start,
                end=a.end,
                rounded=a.has_suffix,
            )
        )
    return sorted(mentions, key=lambda m: m.start)


def _grounded(mention: NumberMention, allowed: list[Decimal]) -> bool:
    for value in allowed:
        if mention.rounded:
            if value and abs(mention.value - value) <= abs(value) * _ROUNDED_TOLERANCE:
                return True
        elif abs(mention.value - value) <= _EXACT_TOLERANCE:
            return True
    return False


def check_grounding(
    text: str, *, amounts: Iterable[Decimal], percents: Iterable[Decimal] = ()
) -> GroundingReport:
    allowed_amounts = [Decimal(a) for a in amounts]
    allowed_percents = [Decimal(p) for p in percents]
    mentions = extract_mentions(text)
    ungrounded = []
    for m in mentions:
        if m.kind is MentionKind.PERCENT:
            if not any(abs(m.value - p) <= _PERCENT_TOLERANCE for p in allowed_percents):
                ungrounded.append(m)
        elif not _grounded(m, allowed_amounts):
            ungrounded.append(m)
    return GroundingReport(checked=len(mentions), ungrounded=ungrounded)


def mark_ungrounded(text: str, report: GroundingReport) -> str:
    """Annotate ungrounded figures in place so they are never shown as verified."""
    out = text
    for m in sorted(report.ungrounded, key=lambda m: m.start, reverse=True):
        out = out[: m.end] + " [unverified]" + out[m.end :]
    return out
