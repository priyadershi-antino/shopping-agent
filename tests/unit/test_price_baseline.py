from datetime import timedelta

import pytest

from shopping_agent.domain.price import CoverageTier, PriceObservation, Trend
from shopping_agent.services.price_baseline import (
    compute_baseline,
    coverage_tier,
    describe_low,
    detect_pre_sale_hike,
)
from tests.factories import NOW, D, daily_prices


@pytest.mark.parametrize(
    ("days", "tier"),
    [
        (1, CoverageTier.INSUFFICIENT),
        (13, CoverageTier.INSUFFICIENT),
        (14, CoverageTier.MINIMUM),
        (29, CoverageTier.MINIMUM),
        (30, CoverageTier.GOOD),
        (59, CoverageTier.GOOD),
        (60, CoverageTier.STRONG),
        (90, CoverageTier.STRONG),
    ],
)
def test_coverage_tiers(days: int, tier: CoverageTier) -> None:
    assert coverage_tier(days) is tier


def test_empty_history() -> None:
    assert compute_baseline([]) is None


def test_baseline_stats_and_observed_low() -> None:
    b = compute_baseline(daily_prices([75000] * 40 + [73000]))
    assert b is not None
    assert b.coverage_days == 41
    assert b.tier is CoverageTier.GOOD
    assert b.current == D(73000)
    assert b.previous == D(75000)
    assert b.minimum == D(73000) and b.maximum == D(75000)
    assert b.change_vs_previous_pct == D("-2.67")
    assert b.at_or_below_observed_min
    low = describe_low(b)
    assert low is not None and "41-day observed history" in low
    assert "ever" not in low


def test_short_history_never_claims_a_low() -> None:
    b = compute_baseline(daily_prices([75000, 74000, 73000]))
    assert b is not None
    assert b.tier is CoverageTier.INSUFFICIENT
    assert b.at_or_below_observed_min
    assert describe_low(b) is None


def test_trend_falling_and_rising() -> None:
    falling = compute_baseline(daily_prices([80000] * 7 + [76000] * 7))
    rising = compute_baseline(daily_prices([76000] * 7 + [80000] * 7))
    flat = compute_baseline(daily_prices([76000] * 14))
    assert falling is not None and falling.trend is Trend.FALLING
    assert rising is not None and rising.trend is Trend.RISING
    assert flat is not None and flat.trend is Trend.FLAT
    short = compute_baseline(daily_prices([76000] * 3))
    assert short is not None and short.trend is Trend.UNKNOWN


def test_discount_vs_mrp() -> None:
    b = compute_baseline(daily_prices([74999], mrp=99999))
    assert b is not None
    assert b.discount_vs_mrp_pct == D("25.00")


def test_daily_close_uses_last_price_of_the_day() -> None:
    obs = [
        PriceObservation(
            listing_id="L1", price=D(p), source="t", observed_at=NOW + timedelta(hours=h)
        )
        for p, h in [(70000, 0), (72000, 1), (71000, 2)]
    ]
    b = compute_baseline(obs)
    assert b is not None
    assert b.days_with_data == 1
    assert b.average == D(71000)
    assert b.minimum == D(70000)  # intraday low is still an observed price


def test_pre_sale_hike_detected() -> None:
    sale_start = NOW + timedelta(days=1)
    obs = daily_prices([70000] * 23 + [76000] * 7)  # ends at NOW, the day before the sale
    hike = detect_pre_sale_hike(obs, sale_start)
    assert hike.detected
    assert hike.prior_median == D(70000)
    assert hike.change_pct == D("8.57")
    assert "70000" in hike.reason


def test_pre_sale_no_hike() -> None:
    hike = detect_pre_sale_hike(daily_prices([70000] * 30), NOW + timedelta(days=1))
    assert not hike.detected


def test_pre_sale_hike_needs_history() -> None:
    hike = detect_pre_sale_hike(daily_prices([70000] * 5), NOW + timedelta(days=1))
    assert not hike.detected
    assert hike.change_pct is None
    assert "not enough" in hike.reason
