"""Deterministic parsing of Indian price and budget expressions.

Handles "₹1,00,000", "80k", "1.2L", "1.5 lakh", "40 hazaar", "Rs. 75,999/-", ranges
("40-50k", "between 40k and 50k", "40k se 50k") and English/Hinglish qualifiers
("under", "upto", "tak", "ke andar", "se kam", "above", "around", "lagbhag").

Bare numbers without a currency marker, magnitude suffix or budget keyword are ignored,
so "RTX 4060" or "16GB" are never read as money.
"""

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from itertools import pairwise

from shopping_agent.domain.common import money
from shopping_agent.domain.requirements import Budget

_MULTIPLIERS: dict[str, int] = {
    "k": 1_000,
    "thousand": 1_000,
    "hazar": 1_000,
    "hazaar": 1_000,
    "hajar": 1_000,
    "l": 100_000,
    "lac": 100_000,
    "lacs": 100_000,
    "lakh": 100_000,
    "lakhs": 100_000,
    "cr": 10_000_000,
    "crore": 10_000_000,
    "crores": 10_000_000,
}

_AMOUNT = re.compile(
    r"""
    (?P<cur>₹|\brs\.?|\binr\b|\brupees?\b)?\s*
    (?P<num>\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)
    \s*(?P<suf>k|thousand|hazaa?r|hajar|lakhs?|lacs?|l|crores?|cr)?
    (?P<cur2>\s*(?:rs\.?|rupees?|inr|/-))?
    (?![a-z0-9])
    """,
    re.IGNORECASE | re.VERBOSE,
)

_MAX_BEFORE = re.compile(
    r"(under|below|less than|upto|up to|within|max(?:imum)?|not more than|no more than|"
    r"at most|cheaper than|<=?|budget(?: is| of)?)\s*$",
    re.IGNORECASE,
)
_MIN_BEFORE = re.compile(
    r"(above|over|more than|at ?least|min(?:imum)?|starting|from|>=?)\s*$", re.IGNORECASE
)
_TARGET_BEFORE = re.compile(
    r"(around|approx(?:imately)?|about|roughly|near|~|lagbhag|kareeb)\s*$", re.IGNORECASE
)
_MAX_AFTER = re.compile(r"^\s*(tak|ke andar|ke under|se kam|or less|max\b|budget\b)", re.IGNORECASE)
_MIN_AFTER = re.compile(r"^\s*(se (?:zyada|upar|jyada)|ke upar|or more|plus\b|\+)", re.IGNORECASE)
_TARGET_AFTER = re.compile(r"^\s*(ke aas ?paas|ish\b|approx)", re.IGNORECASE)
_RANGE_JOIN = re.compile(r"^\s*(?:-|–|to|se|and)\s*$", re.IGNORECASE)
_BETWEEN_BEFORE = re.compile(r"between\s*$", re.IGNORECASE)

_CONTEXT = 30


@dataclass(frozen=True)
class AmountMatch:
    value: Decimal
    start: int
    end: int
    strong: bool  # has a currency marker or magnitude suffix
    has_suffix: bool
    raw_number: Decimal


def _to_decimal(num: str) -> Decimal | None:
    try:
        return Decimal(num.replace(",", ""))
    except InvalidOperation:
        return None


def find_amounts(text: str) -> list[AmountMatch]:
    matches: list[AmountMatch] = []
    for m in _AMOUNT.finditer(text):
        number = _to_decimal(m.group("num"))
        if number is None:
            continue
        suffix = (m.group("suf") or "").lower()
        multiplier = _MULTIPLIERS.get(suffix, 1)
        has_currency = bool(m.group("cur") or m.group("cur2"))
        matches.append(
            AmountMatch(
                value=money(number * multiplier),
                start=m.start("num") if not m.group("cur") else m.start(),
                end=m.end(),
                strong=has_currency or bool(suffix),
                has_suffix=bool(suffix),
                raw_number=number,
            )
        )
    return matches


def parse_amount(text: str, *, allow_plain: bool = False) -> Decimal | None:
    """First explicit money amount in the text, or None.

    Plain numbers (no ₹/Rs/k/lakh) count only with `allow_plain`, for text already known
    to be about prices (e.g. a price field).
    """
    for match in find_amounts(text):
        if match.strong or (allow_plain and match.value >= 100):
            return match.value
    return None


def _qualifier(text: str, match: AmountMatch) -> str | None:
    before = text[max(0, match.start - _CONTEXT) : match.start]
    after = text[match.end : match.end + _CONTEXT]
    if _MAX_BEFORE.search(before) or _MAX_AFTER.search(after):
        return "max"
    if _MIN_BEFORE.search(before) or _MIN_AFTER.search(after):
        return "min"
    if _TARGET_BEFORE.search(before) or _TARGET_AFTER.search(after):
        return "target"
    return None


def parse_budget(text: str) -> Budget:
    """Extract a budget from free text. Returns an empty Budget if nothing is explicit."""
    amounts = find_amounts(text)

    # Ranges: "40-50k", "between 40k and 50k", "40k se 50k".
    for first, second in pairwise(amounts):
        joiner = text[first.end : second.start]
        if not _RANGE_JOIN.match(joiner):
            continue
        is_between = bool(_BETWEEN_BEFORE.search(text[max(0, first.start - 12) : first.start]))
        if joiner.strip().lower() == "and" and not is_between:
            continue
        if not (first.strong or second.strong):
            continue
        low = first.value
        if not first.has_suffix and second.has_suffix:
            # "40-50k" — the suffix applies to both ends.
            low = money(first.raw_number * (second.value / second.raw_number))
        high = second.value
        if low > high:
            low, high = high, low
        return Budget(minimum=low, maximum=high)

    budget = Budget()
    for match in amounts:
        kind = _qualifier(text, match)
        if kind is None:
            # Only an explicit money amount with no qualifier: treat as an approximate target.
            if match.strong and budget.target is None and budget.maximum is None:
                budget.target = match.value
                budget.approximate = True
            continue
        if not match.strong and match.value < 1000:
            continue
        if kind == "max" and budget.maximum is None:
            budget.maximum = match.value
        elif kind == "min" and budget.minimum is None:
            budget.minimum = match.value
        elif kind == "target" and budget.target is None:
            budget.target = match.value
            budget.approximate = True

    if budget.maximum is not None and budget.target is not None and not _has_target_word(text):
        budget.target, budget.approximate = None, False
    return budget


def _has_target_word(text: str) -> bool:
    return bool(re.search(r"around|approx|about|roughly|lagbhag|aas ?paas|kareeb", text, re.I))
