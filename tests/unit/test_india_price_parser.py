import pytest

from shopping_agent.services.india_price_parser import parse_amount, parse_budget
from tests.factories import D


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("₹1,00,000", "100000"),
        ("Rs. 75,999/-", "75999"),
        ("80k", "80000"),
        ("1.2L", "120000"),
        ("1.5 lakh", "150000"),
        ("40 hazaar", "40000"),
        ("INR 59,990", "59990"),
        ("2 crore", "20000000"),
        ("50000 rupees", "50000"),
    ],
)
def test_parse_amount(text: str, expected: str) -> None:
    assert parse_amount(text) == D(expected)


@pytest.mark.parametrize("text", ["16GB RAM", "RTX 4060", "i7 13700H", "144Hz display", "74999"])
def test_parse_amount_ignores_non_money(text: str) -> None:
    assert parse_amount(text) is None


def test_parse_amount_plain_numbers_when_allowed() -> None:
    assert parse_amount("74999", allow_plain=True) == D(74999)


@pytest.mark.parametrize(
    ("text", "minimum", "maximum"),
    [
        ("Find a laptop under 80k for programming", None, "80000"),
        ("phone under ₹40,000 with a good camera", None, "40000"),
        ("upto Rs 25,000", None, "25000"),
        ("budget 75000 for a laptop", None, "75000"),
        ("80k tak ka laptop chahiye", None, "80000"),
        ("50 hazaar se kam ka phone", None, "50000"),
        ("laptop with RTX 4060 under 1 lakh", None, "100000"),
        ("above 30k", "30000", None),
        ("between 40k and 50k", "40000", "50000"),
        ("40-50k phone", "40000", "50000"),
        ("1-1.5 lakh laptop", "100000", "150000"),
        ("40k se 50k ke beech", "40000", "50000"),
        ("from ₹60,000 to ₹70,000", "60000", "70000"),
    ],
)
def test_parse_budget_limits(text: str, minimum: str | None, maximum: str | None) -> None:
    budget = parse_budget(text)
    assert budget.minimum == (D(minimum) if minimum else None)
    assert budget.maximum == (D(maximum) if maximum else None)


@pytest.mark.parametrize(
    ("text", "target"),
    [
        ("around 1L", "100000"),
        ("I need a ₹60,000 phone", "60000"),
        ("lagbhag 70k", "70000"),
        ("80k ke aas paas", "80000"),
    ],
)
def test_parse_budget_targets(text: str, target: str) -> None:
    budget = parse_budget(text)
    assert budget.target == D(target)
    assert budget.approximate
    assert budget.maximum is None


@pytest.mark.parametrize("text", ["16GB RAM 512GB SSD laptop", "a tablet for note-taking", ""])
def test_parse_budget_nothing_explicit(text: str) -> None:
    budget = parse_budget(text)
    assert budget.minimum is budget.maximum is budget.target is None
