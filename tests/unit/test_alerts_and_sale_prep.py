from datetime import UTC, datetime, timedelta
from decimal import Decimal

from shopping_agent.domain.common import Verification
from shopping_agent.domain.offer import CardType, PaymentMethod, UserProfile
from shopping_agent.domain.product import Availability, Marketplace
from shopping_agent.domain.sale import PhaseKind, SaleEvent, SalePhase, SaleState
from shopping_agent.services.alerts import (
    AlertKind,
    CheckSnapshot,
    WatchRules,
    evaluate,
    in_quiet_hours,
    parse_quiet_hours,
)
from shopping_agent.services.sale_prep import WatchSummary, build_sale_brief, sale_prep_plan
from tests.factories import NOW

D = Decimal


def snap(price: int, effective: int, *, stock: str = "in_stock", **offers: str) -> CheckSnapshot:
    return CheckSnapshot(
        listing_price=D(price),
        best_effective=D(effective),
        effective_how="HDFC credit card",
        availability=Availability(stock),
        offers=offers,
    )


RULES = WatchRules(
    label="Nova Swift 16",
    target_effective=D(65000),
    target_price=D(66000),
    target_drop_pct=D(10),
    reference_price=D(72000),
)


def kinds(alerts: list) -> list[AlertKind]:  # type: ignore[type-arg]
    return [a.kind for a in alerts]


def test_targets_fire_on_crossing_only() -> None:
    first = evaluate(RULES, None, snap(70000, 68000))
    assert first == []  # above every target, no offers compared on the first check
    crossed = evaluate(RULES, snap(70000, 68000), snap(64000, 60000))
    assert set(kinds(crossed)) == {
        AlertKind.TARGET_EFFECTIVE,
        AlertKind.TARGET_PRICE,
        AlertKind.PRICE_DROP,
    }
    message = next(a for a in crossed if a.kind is AlertKind.TARGET_EFFECTIVE).message
    assert "₹60,000 for you (HDFC credit card)" in message
    assert "target ₹65,000" in message
    assert evaluate(RULES, snap(64000, 60000), snap(63000, 59000)) == []  # still below: quiet


def test_first_check_already_below_target_alerts() -> None:
    assert AlertKind.TARGET_EFFECTIVE in kinds(evaluate(RULES, None, snap(70000, 64000)))


def test_back_in_stock_and_offer_changes() -> None:
    before = snap(70000, 68000, stock="out_of_stock", bank="10% off", coupon="₹2,000 off")
    after = snap(70000, 68000, bank="12% off", cashback="5% back")
    got = evaluate(RULES, before, after)
    assert kinds(got) == [
        AlertKind.BACK_IN_STOCK,
        AlertKind.OFFER_NEW,
        AlertKind.OFFER_CHANGED,
        AlertKind.OFFER_ENDED,
    ]
    changed = got[2]
    assert "was 10% off, now 12% off" in changed.message
    quiet = RULES.model_copy(update={"notify_offer_changes": False, "notify_back_in_stock": False})
    assert evaluate(quiet, before, after) == []


def test_quiet_hours() -> None:
    assert parse_quiet_hours("23-7") is not None
    assert parse_quiet_hours("nonsense") is None
    # 18:00 UTC = 23:30 IST
    assert in_quiet_hours(datetime(2026, 10, 3, 18, 0, tzinfo=UTC), "23-7")
    assert not in_quiet_hours(datetime(2026, 10, 3, 6, 0, tzinfo=UTC), "23-7")  # 11:30 IST
    assert in_quiet_hours(datetime(2026, 10, 3, 9, 0, tzinfo=UTC), "14:00-15:00")  # 14:30 IST
    assert not in_quiet_hours(NOW, "")


def sale(start: datetime, *, verified: bool = False) -> SaleEvent:
    return SaleEvent(
        name="Festive Sale",
        platform=Marketplace.FLIPKART,
        start=start,
        end=start + timedelta(days=6),
        phases=[
            SalePhase(
                name="Early access",
                kind=PhaseKind.EARLY_ACCESS,
                start=start - timedelta(days=1),
                end=start,
                eligibility="flipkart plus",
            ),
            SalePhase(name="Main", kind=PhaseKind.MAIN, start=start, end=start + timedelta(days=6)),
        ],
        partner_banks=["HDFC", "ICICI"],
        official_sources=["https://www.flipkart.com/sale"] if verified else [],
        verification=Verification.VERIFIED if verified else Verification.UNVERIFIED,
        last_verified_at=NOW,
    )


def watched(**kw: object) -> WatchSummary:
    base: dict[str, object] = {
        "watch_id": "w1",
        "label": "Nova Swift 16",
        "marketplace": Marketplace.FLIPKART,
        "listing_price": D(68990),
        "best_effective": D(64990),
        "history_days": 40,
    }
    return WatchSummary.model_validate({**base, **kw})


def test_prep_plan_without_events() -> None:
    plan = sale_prep_plan(events=[], now=NOW, profile=UserProfile(), watch=[])
    assert plan.event is None
    assert "No upcoming sale" in plan.summary


def test_prep_plan_flags_open_steps() -> None:
    profile = UserProfile(
        payment_methods=[PaymentMethod(issuer="HDFC Bank", card_type=CardType.CREDIT)],
        memberships={"flipkart plus"},
    )
    plan = sale_prep_plan(
        events=[sale(NOW + timedelta(days=5))],
        now=NOW,
        profile=profile,
        watch=[watched(history_days=5)],
    )
    assert plan.state is SaleState.PRE_SALE
    steps = {s.title: s for s in plan.steps}
    assert steps["Confirm the sale dates"].done is False
    assert steps["Build a price baseline"].done is False
    assert "Nova Swift 16 (5 days)" in steps["Build a price baseline"].detail
    assert steps["Set target prices"].done is False
    assert steps["Check bank offers"].done is True
    assert "HDFC" in steps["Check bank offers"].detail
    assert steps["Early access"].done is True
    assert "(dates unconfirmed)" in plan.summary


def test_prep_plan_without_matching_card() -> None:
    profile = UserProfile(payment_methods=[PaymentMethod(issuer="SBI", card_type=CardType.CREDIT)])
    plan = sale_prep_plan(
        events=[sale(NOW + timedelta(days=5), verified=True)],
        now=NOW,
        profile=profile,
        watch=[watched(target_effective=D(63000))],
    )
    steps = {s.title: s for s in plan.steps}
    assert steps["Confirm the sale dates"].done is True
    assert steps["Check bank offers"].done is False
    assert "None of your saved cards match" in steps["Check bank offers"].detail
    assert steps["Early access"].done is False


def test_sale_brief_lines() -> None:
    brief = build_sale_brief(
        sale(NOW),
        [
            watched(pre_sale_median=D(68000), target_effective=D(65000), pre_sale_rise=True),
            watched(watch_id="w2", label="Acme Volt", best_effective=None, listing_price=D(18999)),
        ],
    )
    first, second = brief.items
    assert first.target_met
    assert first.change_vs_pre_sale_pct == D("1.46")  # listed ₹68,990 vs listed median ₹68,000
    assert first.line == (
        "Nova Swift 16: listed ₹68,990, 1.46% above its pre-sale median ₹68,000; the price was "
        "raised shortly before the sale. ₹64,990 for you with your cards (target met)."
    )
    assert "too little history to compare" in second.line
    assert "1 at or below your target" in brief.summary


def test_prep_plan_prefers_the_platform_you_track() -> None:
    amazon = sale(NOW + timedelta(days=2)).model_copy(
        update={"platform": Marketplace.AMAZON, "name": "Amazon Sale"}
    )
    flipkart = sale(NOW + timedelta(days=5))
    plan = sale_prep_plan(
        events=[amazon, flipkart], now=NOW, profile=UserProfile(), watch=[watched()]
    )
    assert plan.event is not None and plan.event.platform is Marketplace.FLIPKART
    assert "on Flipkart" in plan.summary
    only_amazon = sale_prep_plan(
        events=[amazon, flipkart],
        now=NOW,
        profile=UserProfile(),
        watch=[watched()],
        platform=Marketplace.AMAZON,
    )
    steps = {s.title: s for s in only_amazon.steps}
    assert steps["Shortlist and track the products"].done is False
    assert "Nothing on Amazon is tracked yet" in steps["Shortlist and track the products"].detail


def test_brief_before_the_sale_is_a_preview() -> None:
    preview = build_sale_brief(sale(NOW + timedelta(days=2)), [watched()], live=False)
    assert "starts" in preview.summary and "has started" not in preview.summary
    assert "on Flipkart" in preview.summary
