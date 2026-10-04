"""Multi-item budget planning ("₹1,00,000 for a phone + tablet"), fully deterministic."""

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

from shopping_agent.domain.common import ZERO, money


class SlotOption(BaseModel):
    id: str
    name: str
    unit_price: Decimal  # use the effective price the user can actually get


class Slot(BaseModel):
    name: str
    quantity: int = Field(default=1, ge=1)
    required: bool = True
    options: list[SlotOption]


class Pick(BaseModel):
    slot: str
    option_id: str
    name: str
    unit_price: Decimal
    quantity: int
    line_total: Decimal


class Combination(BaseModel):
    picks: list[Pick]
    total: Decimal
    remaining: Decimal


class SortOrder(StrEnum):
    USE_MOST_BUDGET = "use_most_budget"
    SAVE_MOST = "save_most"


class BudgetPlan(BaseModel):
    budget: Decimal
    combinations: list[Combination]
    feasible_count: int
    cheapest_total: Decimal | None
    shortfall: Decimal | None  # how much over budget the cheapest plan is, if none fit
    truncated: bool = False


def plan_budget(
    slots: list[Slot],
    budget: Decimal,
    *,
    sort: SortOrder = SortOrder.USE_MOST_BUDGET,
    limit: int = 20,
    max_explored: int = 200_000,
) -> BudgetPlan:
    budget = money(budget)
    choices: list[list[Pick | None]] = []
    for slot in slots:
        picks: list[Pick | None] = [
            Pick(
                slot=slot.name,
                option_id=o.id,
                name=o.name,
                unit_price=o.unit_price,
                quantity=slot.quantity,
                line_total=money(o.unit_price * slot.quantity),
            )
            for o in sorted(slot.options, key=lambda o: o.unit_price)
        ]
        if not slot.required:
            picks.insert(0, None)
        choices.append(picks)

    min_rest = [ZERO] * (len(choices) + 1)
    for i in range(len(choices) - 1, -1, -1):
        prices = [p.line_total if p else ZERO for p in choices[i]] or [ZERO]
        min_rest[i] = min_rest[i + 1] + min(prices)

    cheapest_total = min_rest[0] if all(choices) else None
    if any(not c for c in choices):  # a required slot with no options
        return BudgetPlan(
            budget=budget, combinations=[], feasible_count=0, cheapest_total=None, shortfall=None
        )

    found: list[Combination] = []
    explored = 0
    truncated = False

    def walk(i: int, total: Decimal, chosen: list[Pick]) -> None:
        nonlocal explored, truncated
        if explored >= max_explored:
            truncated = True
            return
        if total + min_rest[i] > budget:
            return
        if i == len(choices):
            explored += 1
            found.append(Combination(picks=list(chosen), total=total, remaining=budget - total))
            return
        for pick in choices[i]:
            if pick is None:
                walk(i + 1, total, chosen)
            else:
                chosen.append(pick)
                walk(i + 1, total + pick.line_total, chosen)
                chosen.pop()

    walk(0, ZERO, [])
    found.sort(key=lambda c: c.total, reverse=sort is SortOrder.USE_MOST_BUDGET)
    shortfall = None
    if not found and cheapest_total is not None:
        shortfall = cheapest_total - budget
    return BudgetPlan(
        budget=budget,
        combinations=found[:limit],
        feasible_count=len(found),
        cheapest_total=cheapest_total,
        shortfall=shortfall,
        truncated=truncated,
    )


class ItemParseError(ValueError):
    pass


def parse_items(text: str) -> list[Slot]:
    """Parse lines like "phone: Nova Pixelon 9 = 33,999" (one option per line; several lines
    per slot). "phone x2: …" asks for two units; "case?: …" makes the slot optional.
    Prices accept 33999, 34k, ₹33,999 or 1.2L."""
    from shopping_agent.services.india_price_parser import parse_amount

    slots: dict[str, Slot] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        head, sep, rest = line.partition(":")
        name, eq, price_text = rest.rpartition("=")
        if not sep or not eq or not name.strip():
            raise ItemParseError(f"line {number}: use 'slot: product name = price'")
        price = parse_amount(price_text, allow_plain=True)
        if price is None or price <= 0:
            raise ItemParseError(f"line {number}: '{price_text.strip()}' is not a price")
        slot_name = head.strip()
        optional = slot_name.endswith("?")
        slot_name = slot_name.rstrip("?").strip()
        quantity = 1
        if (parts := slot_name.rsplit(" x", 1)) and len(parts) == 2 and parts[1].isdigit():
            slot_name, quantity = parts[0].strip(), int(parts[1])
        slot = slots.setdefault(
            slot_name.lower(),
            Slot(name=slot_name, quantity=quantity, required=not optional, options=[]),
        )
        slot.options.append(
            SlotOption(
                id=f"{slot_name.lower()}-{len(slot.options) + 1}",
                name=name.strip(),
                unit_price=price,
            )
        )
    if not slots:
        raise ItemParseError("add at least one line: 'slot: product name = price'")
    return list(slots.values())
