"""Priority-based monitoring with per-provider quota budgets.

Polling frequency depends on sale state and how interesting an item is right now, instead
of polling every watched item on a fixed timer.
"""

from decimal import Decimal
from enum import StrEnum

from shopping_agent.domain.sale import SaleState


class Priority(StrEnum):
    URGENT = "urgent"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


class UserPriority(StrEnum):
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


_SALE = {Priority.URGENT: 10, Priority.HIGH: 15, Priority.NORMAL: 30, Priority.LOW: 60}
_NEAR_SALE = {Priority.URGENT: 30, Priority.HIGH: 60, Priority.NORMAL: 180, Priority.LOW: 360}
_QUIET = {Priority.URGENT: 60, Priority.HIGH: 180, Priority.NORMAL: 360, Priority.LOW: 720}

INTERVAL_MINUTES: dict[SaleState, dict[Priority, int]] = {
    SaleState.ACTIVE: _SALE,
    SaleState.EARLY_ACCESS: _SALE,
    SaleState.PRE_SALE: _NEAR_SALE,
    SaleState.POST_SALE: _NEAR_SALE,
    SaleState.NORMAL: _QUIET,
}

_DEGRADE_ORDER = [Priority.LOW, Priority.NORMAL, Priority.HIGH, Priority.URGENT]
MAX_INTERVAL_MINUTES = 24 * 60


def monitor_priority(
    *,
    user_priority: UserPriority = UserPriority.NORMAL,
    distance_to_target_pct: Decimal | None = None,
    recent_change_pct: Decimal | None = None,
) -> Priority:
    """Score: user priority (0–2) + closeness to target (0–2) + recent volatility (0–1)."""
    score = {UserPriority.HIGH: 2, UserPriority.NORMAL: 1, UserPriority.LOW: 0}[user_priority]
    if distance_to_target_pct is not None:
        if distance_to_target_pct <= 3:
            score += 2
        elif distance_to_target_pct <= 10:
            score += 1
    if recent_change_pct is not None and abs(recent_change_pct) >= 5:
        score += 1
    if score >= 4:
        return Priority.URGENT
    if score == 3:
        return Priority.HIGH
    if score >= 1:
        return Priority.NORMAL
    return Priority.LOW


def plan_intervals(
    items: dict[str, Priority], state: SaleState, *, budget_per_hour: int
) -> dict[str, int]:
    """Poll interval (minutes) per item, slowing low-priority items first to fit the quota."""
    intervals = {item: INTERVAL_MINUTES[state][p] for item, p in items.items()}

    def demand() -> float:
        return sum(60 / m for m in intervals.values())

    for priority in _DEGRADE_ORDER:
        members = [i for i, p in items.items() if p is priority]
        while demand() > budget_per_hour and members:
            if all(intervals[i] >= MAX_INTERVAL_MINUTES for i in members):
                break
            for i in members:
                intervals[i] = min(intervals[i] * 2, MAX_INTERVAL_MINUTES)
        if demand() <= budget_per_hour:
            break
    return intervals
