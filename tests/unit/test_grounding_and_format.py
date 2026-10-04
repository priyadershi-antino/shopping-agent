import pytest

from shopping_agent.services.formatting import format_inr
from shopping_agent.services.grounding import (
    MentionKind,
    check_grounding,
    extract_mentions,
    mark_ungrounded,
)
from tests.factories import D


@pytest.mark.parametrize(
    ("amount", "text"),
    [
        (100000, "₹1,00,000"),
        (74999, "₹74,999"),
        (999, "₹999"),
        (12345678, "₹1,23,45,678"),
        (-5000, "-₹5,000"),
    ],
)
def test_format_inr(amount: int, text: str) -> None:
    assert format_inr(amount) == text


def test_format_inr_paise() -> None:
    assert format_inr(D("3999.95"), paise=True) == "₹3,999.95"
    assert format_inr(D("4000.00"), paise=True) == "₹4,000"


def test_extract_mentions_skips_specs_and_years() -> None:
    kinds = [
        (m.kind, m.value)
        for m in extract_mentions(
            "2024 model, 16GB RAM, RTX 4060, ₹73,999 after 10% off, was 79999"
        )
    ]
    assert (MentionKind.CURRENCY, D(73999)) in kinds
    assert (MentionKind.PERCENT, D(10)) in kinds
    assert (MentionKind.NUMBER, D(79999)) in kinds
    assert all(v not in {D(2024), D(16)} for _, v in kinds)


def test_grounded_answer_passes() -> None:
    report = check_grounding(
        "Best price is ₹73,999 with the HDFC offer (10% instant discount).",
        amounts=[D(73999)],
        percents=[D(10)],
    )
    assert report.ok
    assert report.checked == 2


def test_invented_price_is_flagged() -> None:
    report = check_grounding("You can get it for ₹71,999.", amounts=[D(73999)])
    assert not report.ok
    assert report.ungrounded[0].value == D(71999)
    assert mark_ungrounded("You can get it for ₹71,999.", report) == (
        "You can get it for ₹71,999 [unverified]."
    )


def test_rounded_mentions_get_one_percent_tolerance() -> None:
    assert check_grounding("about ₹74k", amounts=[D(73999)]).ok
    assert not check_grounding("about ₹74k", amounts=[D(73000)]).ok


def test_invented_percentage_is_flagged() -> None:
    assert not check_grounding("save 25%", amounts=[], percents=[D(10)]).ok
