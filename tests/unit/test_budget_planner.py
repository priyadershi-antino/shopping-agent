import pytest

from shopping_agent.services.budget_planner import Slot, SlotOption, SortOrder, plan_budget
from tests.factories import D


def phones() -> Slot:
    return Slot(
        name="phone",
        options=[
            SlotOption(id="p1", name="Phone A", unit_price=D(45000)),
            SlotOption(id="p2", name="Phone B", unit_price=D(60000)),
        ],
    )


def tablets() -> Slot:
    return Slot(
        name="tablet",
        options=[
            SlotOption(id="t1", name="Tab A", unit_price=D(30000)),
            SlotOption(id="t2", name="Tab B", unit_price=D(50000)),
        ],
    )


def test_feasible_combinations_use_most_budget_first() -> None:
    plan = plan_budget([phones(), tablets()], D(100000))
    assert plan.feasible_count == 3
    assert [c.total for c in plan.combinations] == [D(95000), D(90000), D(75000)]
    assert plan.combinations[0].remaining == D(5000)
    assert plan.shortfall is None


def test_save_most_ordering() -> None:
    plan = plan_budget([phones(), tablets()], D(100000), sort=SortOrder.SAVE_MOST)
    assert plan.combinations[0].total == D(75000)


def test_optional_slot_can_be_skipped() -> None:
    case = Slot(
        name="case", required=False, options=[SlotOption(id="c", name="Case", unit_price=D(8000))]
    )
    plan = plan_budget([phones(), tablets(), case], D(80000))
    totals = sorted(c.total for c in plan.combinations)
    assert totals == [D(75000)]  # 45k + 30k; adding the case would exceed 80k


def test_quantity_multiplies_line_total() -> None:
    slot = Slot(
        name="monitor", quantity=2, options=[SlotOption(id="m", name="Mon", unit_price=D(12000))]
    )
    plan = plan_budget([slot], D(30000))
    assert plan.combinations[0].picks[0].line_total == D(24000)


def test_infeasible_reports_shortfall() -> None:
    plan = plan_budget([phones(), tablets()], D(70000))
    assert plan.combinations == []
    assert plan.cheapest_total == D(75000)
    assert plan.shortfall == D(5000)


def test_required_slot_without_options() -> None:
    plan = plan_budget([Slot(name="x", options=[])], D(1000))
    assert plan.feasible_count == 0 and plan.cheapest_total is None


def test_parse_items_text() -> None:
    from shopping_agent.services.budget_planner import ItemParseError, parse_items

    slots = parse_items(
        "phone: Nova Pixelon 9 = 33,999\nphone: Acme Volt 5G = 19k\n"
        "tablet x2: Zenith Tab S = ₹27,999\n# comment\ncase?: Rugged case = 999\n"
    )
    assert [(s.name, s.quantity, s.required, len(s.options)) for s in slots] == [
        ("phone", 1, True, 2),
        ("tablet", 2, True, 1),
        ("case", 1, False, 1),
    ]
    assert slots[0].options[1].unit_price == 19000
    plan = plan_budget(slots, D(100000))
    assert plan.feasible_count > 0
    with pytest.raises(ItemParseError):
        parse_items("phone Nova 30000")
    with pytest.raises(ItemParseError):
        parse_items("phone: Nova = cheap")
