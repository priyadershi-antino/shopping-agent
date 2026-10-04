"""Transparent requirement matching (meets / partial / fails / unknown) — no hidden scores."""

import re
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

from shopping_agent.domain.requirements import Budget, Constraint, ConstraintOp, Requirements
from shopping_agent.services.formatting import format_inr

FactValue = str | int | float | bool | Decimal | list[str] | None


class MatchStatus(StrEnum):
    MEETS = "meets"
    PARTIAL = "partial"
    FAILS = "fails"
    UNKNOWN = "unknown"


class Level(StrEnum):
    HARD = "hard"
    STRONG = "strong"
    NICE = "nice"


class RequirementCheck(BaseModel):
    requirement: str
    level: Level
    status: MatchStatus
    expected: str
    actual: str | None
    note: str | None = None


_UNIT_NUMBER = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*(gb|tb|mah|hz|w|inch|inches|in|\")?\s*$", re.IGNORECASE
)
# Phrases that mean the same thing in requirements and listings.
_SYNONYMS = (
    (r"\bbrand new\b|\bsealed\b|\bunused\b", "new"),
    (r"\bcertified refurbished\b|\brefurb\b", "refurbished"),
    (r"\bpre ?owned\b|\bsecond ?hand\b", "used"),
    (r"\bwi ?fi\b", "wifi"),
)


def _num(value: FactValue) -> float | None:
    """A number, also from text such as "16GB", "6000 mAh" or "1 TB" (in GB)."""
    if isinstance(value, bool) or value is None or isinstance(value, list):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    match = _UNIT_NUMBER.match(str(value))
    if match is None:
        return None
    number = float(match.group(1))
    return number * 1024 if (match.group(2) or "").lower() == "tb" else number


def _norm_text(value: object) -> list[str]:
    text = re.sub(r"[^a-z0-9.]+", " ", str(value).lower())
    text = re.sub(r"(\d)([a-z])", r"\1 \2", text)  # "16gb" → "16 gb"
    for pattern, replacement in _SYNONYMS:
        text = re.sub(pattern, replacement, text)
    return text.split()


def text_matches(actual: object, expected: object) -> bool:
    """Every word of the requirement appears in the fact: "RTX 4060" matches "NVIDIA GeForce
    RTX 4060 8GB", "brand new" matches "new", "Nimbus 15" matches the product title."""
    wanted = _norm_text(expected)
    have = set(_norm_text(actual))
    return bool(wanted) and all(w in have for w in wanted)


def _describe(c: Constraint) -> str:
    symbols = {
        ConstraintOp.EQ: "=",
        ConstraintOp.GTE: "≥",
        ConstraintOp.LTE: "≤",
        ConstraintOp.IN: "one of",
        ConstraintOp.NOT_IN: "not",
        ConstraintOp.CONTAINS: "includes",
        ConstraintOp.QUALITATIVE: "",
    }
    return f"{c.attribute} {symbols[c.op]} {c.value}".replace("  ", " ")


def check_constraint(c: Constraint, facts: dict[str, FactValue], level: Level) -> RequirementCheck:
    actual = facts.get(c.attribute)
    base = {"requirement": c.attribute, "level": level, "expected": _describe(c)}
    if c.op is ConstraintOp.QUALITATIVE:
        return RequirementCheck(
            **base,
            status=MatchStatus.UNKNOWN,
            actual=None,
            note="qualitative; assessed from specs and reviews, not machine-checked",
        )
    if actual is None:
        return RequirementCheck(**base, status=MatchStatus.UNKNOWN, actual=None, note="not stated")

    actual_text = ", ".join(actual) if isinstance(actual, list) else str(actual)
    ok: bool
    if c.op in {ConstraintOp.GTE, ConstraintOp.LTE}:
        a, e = _num(actual), _num(c.value)
        if a is None or e is None:
            return RequirementCheck(
                **base, status=MatchStatus.UNKNOWN, actual=actual_text, note="not comparable"
            )
        ok = a >= e if c.op is ConstraintOp.GTE else a <= e
    elif c.op is ConstraintOp.EQ:
        a_num, e_num = _num(actual), _num(c.value)
        ok = (
            a_num == e_num
            if a_num is not None and e_num is not None
            else text_matches(actual_text, c.value)
        )
    elif c.op in {ConstraintOp.IN, ConstraintOp.NOT_IN}:
        options = c.value if isinstance(c.value, list) else [c.value]
        inside = any(text_matches(actual_text, option) for option in options)
        ok = inside if c.op is ConstraintOp.IN else not inside
    else:  # CONTAINS
        needle = str(c.value).lower()
        ok = (
            any(needle == str(v).lower() for v in actual)
            if isinstance(actual, list)
            else needle in actual_text.lower()
        )
    return RequirementCheck(
        **base, status=MatchStatus.MEETS if ok else MatchStatus.FAILS, actual=actual_text
    )


def check_budget(
    budget: Budget,
    *,
    listing_price: Decimal,
    best_eligible: Decimal,
    best_conditional: Decimal | None = None,
) -> RequirementCheck | None:
    limit = budget.maximum
    approximate = False
    if limit is None and budget.target is not None:
        limit, approximate = budget.target, True
    if limit is None:
        return None
    expected = f"{'about' if approximate else '≤'} {format_inr(limit)}"
    actual = format_inr(best_eligible)

    def result(status: MatchStatus, note: str | None = None) -> RequirementCheck:
        return RequirementCheck(
            requirement="budget",
            level=Level.HARD,
            status=status,
            expected=expected,
            actual=actual,
            note=note,
        )

    if listing_price <= limit:
        return result(MatchStatus.MEETS)
    if best_eligible <= limit:
        return result(MatchStatus.PARTIAL, "within budget only with your eligible offers")
    if best_conditional is not None and best_conditional <= limit:
        return result(MatchStatus.PARTIAL, "within budget only if offer conditions are met")
    if approximate and best_eligible <= limit * Decimal("1.10"):
        over = (best_eligible - limit) / limit * 100
        return result(MatchStatus.PARTIAL, f"{over:.0f}% above your approximate budget")
    return result(MatchStatus.FAILS)


def match_requirements(
    requirements: Requirements,
    facts: dict[str, FactValue],
    *,
    listing_price: Decimal,
    best_eligible: Decimal,
    best_conditional: Decimal | None = None,
) -> list[RequirementCheck]:
    checks: list[RequirementCheck] = []
    if budget := check_budget(
        requirements.budget,
        listing_price=listing_price,
        best_eligible=best_eligible,
        best_conditional=best_conditional,
    ):
        checks.append(budget)
    for level, constraints in (
        (Level.HARD, requirements.hard_constraints),
        (Level.STRONG, requirements.strong_preferences),
        (Level.NICE, requirements.nice_to_have),
    ):
        checks.extend(check_constraint(c, facts, level) for c in constraints)
    return checks


class CandidateRow(BaseModel):
    variant_id: str
    best_eligible: Decimal
    checks: list[RequirementCheck] = Field(default_factory=list)
    price_suspect: bool = False  # far below the rest of the shortlist: verify before trusting

    def count(self, level: Level, status: MatchStatus) -> int:
        return sum(1 for c in self.checks if c.level is level and c.status is status)

    @property
    def passes_hard(self) -> bool:
        return self.count(Level.HARD, MatchStatus.FAILS) == 0


def order_candidates(
    rows: list[CandidateRow], *, examined: set[str] | None = None
) -> list[CandidateRow]:
    """Stated ordering rule: hard fails, hard unknowns, strong fails, suspiciously cheap items,
    then items examined in detail during research before ones only seen in search results, then
    effective price."""
    examined = examined or set()
    return sorted(
        rows,
        key=lambda r: (
            r.count(Level.HARD, MatchStatus.FAILS),
            r.count(Level.HARD, MatchStatus.UNKNOWN),
            r.count(Level.STRONG, MatchStatus.FAILS),
            r.price_suspect,
            r.variant_id not in examined,
            r.best_eligible,
        ),
    )
