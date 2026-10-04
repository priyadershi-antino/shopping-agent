import pytest

from shopping_agent.domain.sale import SaleState
from shopping_agent.services.monitor_priority import (
    INTERVAL_MINUTES,
    Priority,
    UserPriority,
    monitor_priority,
    plan_intervals,
)
from tests.factories import D


@pytest.mark.parametrize(
    ("user", "distance", "change", "expected"),
    [
        (UserPriority.HIGH, D(2), None, Priority.URGENT),
        (UserPriority.NORMAL, D(2), None, Priority.HIGH),
        (UserPriority.NORMAL, D(8), D(6), Priority.HIGH),
        (UserPriority.NORMAL, None, None, Priority.NORMAL),
        (UserPriority.LOW, D(50), None, Priority.LOW),
        (UserPriority.LOW, D(5), None, Priority.NORMAL),
    ],
)
def test_priority_scoring(
    user: UserPriority, distance: object, change: object, expected: Priority
) -> None:
    assert (
        monitor_priority(
            user_priority=user,
            distance_to_target_pct=distance,  # type: ignore[arg-type]
            recent_change_pct=change,  # type: ignore[arg-type]
        )
        is expected
    )


def test_sale_polls_faster_than_normal() -> None:
    for p in Priority:
        assert INTERVAL_MINUTES[SaleState.ACTIVE][p] < INTERVAL_MINUTES[SaleState.NORMAL][p]


def test_within_budget_keeps_default_intervals() -> None:
    items = {"a": Priority.URGENT, "b": Priority.LOW}
    assert plan_intervals(items, SaleState.ACTIVE, budget_per_hour=100) == {"a": 10, "b": 60}


def test_over_budget_slows_low_priority_first() -> None:
    items = {f"low{i}": Priority.LOW for i in range(10)} | {"urgent": Priority.URGENT}
    intervals = plan_intervals(items, SaleState.ACTIVE, budget_per_hour=10)
    assert intervals["urgent"] == 10
    assert all(intervals[f"low{i}"] == 240 for i in range(10))
    assert sum(60 / m for m in intervals.values()) <= 10
