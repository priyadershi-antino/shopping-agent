from datetime import datetime, timedelta

import pytest

from shopping_agent.domain.common import Verification
from shopping_agent.domain.price import PreSaleHike
from shopping_agent.domain.product import Availability, Marketplace
from shopping_agent.domain.sale import PhaseKind, SaleEvent, SalePhase, SaleState
from shopping_agent.services.price_baseline import compute_baseline
from shopping_agent.services.sale_mode import SaleStatus, sale_status
from shopping_agent.services.timing import TimingAdvice, analyze_timing
from tests.factories import NOW, D, daily_prices


def event(
    start: datetime,
    *,
    days: int = 7,
    platform: Marketplace = Marketplace.FLIPKART,
    early_access_hours: int = 24,
    verification: Verification = Verification.VERIFIED,
) -> SaleEvent:
    end = start + timedelta(days=days)
    return SaleEvent(
        name="Big Sale",
        platform=platform,
        start=start,
        end=end,
        phases=[
            SalePhase(
                name="Early access",
                kind=PhaseKind.EARLY_ACCESS,
                start=start - timedelta(hours=early_access_hours),
                end=start,
                eligibility="flipkart_plus",
            ),
            SalePhase(name="Main", kind=PhaseKind.MAIN, start=start, end=end),
        ],
        verification=verification,
        last_verified_at=NOW,
    )


@pytest.mark.parametrize(
    ("offset", "state"),
    [
        (timedelta(days=-30), SaleState.NORMAL),
        (timedelta(days=-10), SaleState.PRE_SALE),
        (timedelta(hours=-5), SaleState.EARLY_ACCESS),
        (timedelta(days=2), SaleState.ACTIVE),
        (timedelta(days=8), SaleState.POST_SALE),
        (timedelta(days=20), SaleState.NORMAL),
    ],
)
def test_sale_states(offset: timedelta, state: SaleState) -> None:
    start = NOW
    assert sale_status([event(start)], start + offset).state is state


def test_platform_filter_and_priority() -> None:
    events = [
        event(NOW + timedelta(days=5), platform=Marketplace.AMAZON),
        event(NOW - timedelta(days=1), platform=Marketplace.FLIPKART),
    ]
    assert sale_status(events, NOW).state is SaleState.ACTIVE
    amazon = sale_status(events, NOW, platform=Marketplace.AMAZON)
    assert amazon.state is SaleState.PRE_SALE
    assert amazon.event is not None and amazon.event.platform is Marketplace.AMAZON


def test_unverified_event_is_marked() -> None:
    status = sale_status(
        [event(NOW - timedelta(days=1), verification=Verification.SOURCE_REPORTED)], NOW
    )
    assert status.is_sale_window and not status.verified


def test_timing_target_met() -> None:
    result = analyze_timing(
        current_effective=D(64000), target=D(65000), baseline=None, sale=None, now=NOW
    )
    assert result.advice is TimingAdvice.TARGET_MET


def test_timing_set_alert_matches_spec_example() -> None:
    baseline = compute_baseline(daily_prices([75999] * 30 + [64999] * 15 + [69999] * 15))
    result = analyze_timing(
        current_effective=D(69999), target=D(65000), baseline=baseline, sale=None, now=NOW
    )
    assert result.advice is TimingAdvice.SET_ALERT
    assert result.suggested_alert_price == D(65000)
    for fragment in ("₹69,999", "₹65,000", "60 observed days ranged ₹64,999–₹75,999"):
        assert fragment in result.summary
    assert "will" not in result.summary  # no predictions


def test_timing_pre_sale_suggests_waiting_without_predicting() -> None:
    status = SaleStatus(
        state=SaleState.PRE_SALE, event=event(NOW + timedelta(days=7)), verified=True
    )
    result = analyze_timing(
        current_effective=D(70000), target=None, baseline=None, sale=status, now=NOW
    )
    assert result.advice is TimingAdvice.CONSIDER_WAITING_FOR_SALE
    assert "may or may not" in result.summary
    assert any(s.name == "sale_upcoming" for s in result.signals)


def test_timing_at_observed_low() -> None:
    baseline = compute_baseline(daily_prices([75000] * 30 + [72000]))
    result = analyze_timing(
        current_effective=D(72000), target=None, baseline=baseline, sale=None, now=NOW
    )
    assert result.advice is TimingAdvice.CONSIDER_BUYING


def test_timing_needs_more_data() -> None:
    baseline = compute_baseline(daily_prices([75000] * 3))
    result = analyze_timing(
        current_effective=D(75000), target=None, baseline=baseline, sale=None, now=NOW
    )
    assert result.advice is TimingAdvice.NEED_MORE_DATA
    assert any(s.name == "insufficient_history" for s in result.signals)


def test_timing_signals_for_expiry_and_stock() -> None:
    result = analyze_timing(
        current_effective=D(75000),
        target=None,
        baseline=None,
        sale=None,
        now=NOW,
        offer_expires_at=NOW + timedelta(hours=10),
        availability=Availability.LIMITED,
    )
    names = {s.name for s in result.signals}
    assert {"offer_expiring", "availability"} <= names


def test_timing_reports_pre_sale_price_rise() -> None:
    status = SaleStatus(
        state=SaleState.PRE_SALE, event=event(NOW + timedelta(days=3)), verified=False
    )
    hike = PreSaleHike(
        detected=True,
        recent_median=D(68990),
        prior_median=D(64990),
        change_pct=D("6.15"),
        reason="median price in the 7 days before the sale is 6.15% above the earlier median",
    )
    result = analyze_timing(
        current_effective=D(64990),
        target=None,
        baseline=None,
        sale=status,
        now=NOW,
        pre_sale_hike=hike,
    )
    assert any(s.name == "pre_sale_price_rise" for s in result.signals)
    assert "raised before the sale" in result.summary
    assert "against ₹64,990" in result.summary
