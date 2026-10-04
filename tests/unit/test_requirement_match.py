import pytest

from shopping_agent.domain.requirements import Budget, Constraint, ConstraintOp, Requirements
from shopping_agent.services.requirement_match import (
    CandidateRow,
    FactValue,
    Level,
    MatchStatus,
    RequirementCheck,
    check_budget,
    check_constraint,
    match_requirements,
    order_candidates,
)
from tests.factories import D

FACTS: dict[str, FactValue] = {
    "ram_gb": 16,
    "brand": "HP",
    "gpu": "RTX 4050",
    "ports": ["usb-c", "hdmi"],
    "weight_kg": 2.3,
}


@pytest.mark.parametrize(
    ("constraint", "status"),
    [
        (Constraint(attribute="ram_gb", op=ConstraintOp.GTE, value=16), MatchStatus.MEETS),
        (Constraint(attribute="ram_gb", op=ConstraintOp.GTE, value=32), MatchStatus.FAILS),
        (Constraint(attribute="weight_kg", op=ConstraintOp.LTE, value=2.0), MatchStatus.FAILS),
        (Constraint(attribute="brand", op=ConstraintOp.EQ, value="hp"), MatchStatus.MEETS),
        (
            Constraint(attribute="brand", op=ConstraintOp.IN, value=["Dell", "HP"]),
            MatchStatus.MEETS,
        ),
        (Constraint(attribute="brand", op=ConstraintOp.NOT_IN, value=["HP"]), MatchStatus.FAILS),
        (Constraint(attribute="ports", op=ConstraintOp.CONTAINS, value="HDMI"), MatchStatus.MEETS),
        (Constraint(attribute="gpu", op=ConstraintOp.CONTAINS, value="rtx"), MatchStatus.MEETS),
        (Constraint(attribute="battery_wh", op=ConstraintOp.GTE, value=70), MatchStatus.UNKNOWN),
        (
            Constraint(attribute="battery", op=ConstraintOp.QUALITATIVE, value="long"),
            MatchStatus.UNKNOWN,
        ),
        (Constraint(attribute="brand", op=ConstraintOp.GTE, value=3), MatchStatus.UNKNOWN),
    ],
)
def test_constraints(constraint: Constraint, status: MatchStatus) -> None:
    assert check_constraint(constraint, FACTS, Level.HARD).status is status


@pytest.mark.parametrize(
    ("listing", "eligible", "conditional", "status"),
    [
        (79000, 79000, None, MatchStatus.MEETS),
        (82000, 78000, None, MatchStatus.PARTIAL),
        (85000, 84000, 79000, MatchStatus.PARTIAL),
        (90000, 88000, 85000, MatchStatus.FAILS),
    ],
)
def test_budget_max(
    listing: int, eligible: int, conditional: int | None, status: MatchStatus
) -> None:
    check = check_budget(
        Budget(maximum=D(80000)),
        listing_price=D(listing),
        best_eligible=D(eligible),
        best_conditional=D(conditional) if conditional else None,
    )
    assert check is not None and check.status is status


def test_approximate_budget_allows_small_overrun_with_note() -> None:
    check = check_budget(
        Budget(target=D(80000), approximate=True), listing_price=D(84000), best_eligible=D(84000)
    )
    assert check is not None
    assert check.status is MatchStatus.PARTIAL
    assert check.note == "5% above your approximate budget"


def test_no_budget_no_check() -> None:
    assert check_budget(Budget(), listing_price=D(1), best_eligible=D(1)) is None


def test_match_requirements_levels() -> None:
    req = Requirements(
        budget=Budget(maximum=D(80000)),
        hard_constraints=[Constraint(attribute="ram_gb", op=ConstraintOp.GTE, value=16)],
        strong_preferences=[Constraint(attribute="weight_kg", op=ConstraintOp.LTE, value=2.0)],
    )
    checks = match_requirements(req, FACTS, listing_price=D(78000), best_eligible=D(76000))
    assert [(c.requirement, c.level, c.status) for c in checks] == [
        ("budget", Level.HARD, MatchStatus.MEETS),
        ("ram_gb", Level.HARD, MatchStatus.MEETS),
        ("weight_kg", Level.STRONG, MatchStatus.FAILS),
    ]


def _row(vid: str, price: int, *statuses: tuple[Level, MatchStatus]) -> CandidateRow:
    return CandidateRow(
        variant_id=vid,
        best_eligible=D(price),
        checks=[
            RequirementCheck(requirement=f"r{i}", level=lvl, status=st, expected="", actual=None)
            for i, (lvl, st) in enumerate(statuses)
        ],
    )


def test_ordering_rule() -> None:
    rows = [
        _row("cheap_but_fails", 50000, (Level.HARD, MatchStatus.FAILS)),
        _row("unknown_hard", 60000, (Level.HARD, MatchStatus.UNKNOWN)),
        _row("pricier_ok", 75000, (Level.HARD, MatchStatus.MEETS)),
        _row(
            "cheaper_ok", 70000, (Level.HARD, MatchStatus.MEETS), (Level.STRONG, MatchStatus.FAILS)
        ),
        _row("best", 72000, (Level.HARD, MatchStatus.MEETS)),
    ]
    assert [r.variant_id for r in order_candidates(rows)] == [
        "best",
        "pricier_ok",
        "cheaper_ok",
        "unknown_hard",
        "cheap_but_fails",
    ]
    assert not rows[0].passes_hard


def test_suspiciously_cheap_items_are_not_the_top_pick() -> None:
    cheap = _row("too_good", 15500, (Level.HARD, MatchStatus.MEETS))
    cheap.price_suspect = True
    rows = [cheap, _row("a", 47000, (Level.HARD, MatchStatus.MEETS)), _row("b", 60000)]
    assert [r.variant_id for r in order_candidates(rows)] == ["a", "b", "too_good"]


def test_text_requirements_match_like_a_person_would() -> None:
    from shopping_agent.services.requirement_match import text_matches

    assert text_matches("NVIDIA GeForce RTX 4060 8GB", "RTX 4060")
    assert text_matches("new", "brand new")
    assert text_matches("Acme Nimbus 15 NB15-G4 Gaming Laptop", "Nimbus 15")
    assert text_matches("Acme Nimbus 15 NB15-G4 Gaming Laptop", "nb15-g4")
    assert text_matches("Wi-Fi + Cellular", "wifi")
    assert not text_matches("NVIDIA GeForce RTX 4050", "RTX 4060")
    assert not text_matches("renewed", "new")
    assert not text_matches("Zenith ProBook 14", "Pro 14")


def test_unit_numbers_compare_numerically() -> None:
    gpu = Constraint(attribute="gpu", op=ConstraintOp.EQ, value="RTX 4060")
    ram = Constraint(attribute="ram_gb", op=ConstraintOp.GTE, value="16GB")
    storage = Constraint(attribute="storage_gb", op=ConstraintOp.GTE, value="1 TB")
    facts: dict[str, FactValue] = {
        "gpu": "NVIDIA GeForce RTX 4060 8GB",
        "ram_gb": 16,
        "storage_gb": 512,
    }
    assert check_constraint(gpu, facts, Level.HARD).status is MatchStatus.MEETS
    assert check_constraint(ram, facts, Level.HARD).status is MatchStatus.MEETS
    assert check_constraint(storage, facts, Level.HARD).status is MatchStatus.FAILS
