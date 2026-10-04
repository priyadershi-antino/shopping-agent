"""Price Baseline Analyzer.

Summarises *our observed* history for a listing. It never claims "lowest ever": the best it
can say is "lowest in our N-day observed history", and only with at least minimum coverage.
"""

from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from statistics import median

from shopping_agent.domain.common import money
from shopping_agent.domain.price import (
    CoverageTier,
    PreSaleHike,
    PriceBaseline,
    PriceObservation,
    Trend,
)

IST = timezone(timedelta(hours=5, minutes=30))  # India has no DST
TREND_WINDOW_DAYS = 7
TREND_THRESHOLD_PCT = Decimal(2)


def coverage_tier(days: int) -> CoverageTier:
    if days < 14:
        return CoverageTier.INSUFFICIENT
    if days < 30:
        return CoverageTier.MINIMUM
    if days < 60:
        return CoverageTier.GOOD
    return CoverageTier.STRONG


def _pct(new: Decimal, old: Decimal) -> Decimal:
    return money((new - old) / old * 100) if old else Decimal(0)


def _ist_date(ts: datetime) -> date:
    return ts.astimezone(IST).date()


def daily_closes(observations: list[PriceObservation]) -> "OrderedDict[date, Decimal]":
    """Last observed price for each IST calendar day, oldest first."""
    closes: OrderedDict[date, Decimal] = OrderedDict()
    for obs in sorted(observations, key=lambda o: o.observed_at):
        closes[_ist_date(obs.observed_at)] = obs.price
    return closes


def _trend(closes: "OrderedDict[date, Decimal]") -> Trend:
    if not closes:
        return Trend.UNKNOWN
    last_day = next(reversed(closes))
    recent_start = last_day - timedelta(days=TREND_WINDOW_DAYS - 1)
    prior_start = recent_start - timedelta(days=TREND_WINDOW_DAYS)
    recent = [p for d, p in closes.items() if d >= recent_start]
    prior = [p for d, p in closes.items() if prior_start <= d < recent_start]
    if len(recent) < 2 or len(prior) < 2:
        return Trend.UNKNOWN
    change = _pct(Decimal(sum(recent)) / len(recent), Decimal(sum(prior)) / len(prior))
    if change >= TREND_THRESHOLD_PCT:
        return Trend.RISING
    if change <= -TREND_THRESHOLD_PCT:
        return Trend.FALLING
    return Trend.FLAT


def compute_baseline(observations: list[PriceObservation]) -> PriceBaseline | None:
    if not observations:
        return None
    obs = sorted(observations, key=lambda o: o.observed_at)
    current_obs = obs[-1]
    current = current_obs.price
    closes = daily_closes(obs)
    close_values = list(closes.values())

    first_day, last_day = next(iter(closes)), next(reversed(closes))
    coverage_days = (last_day - first_day).days + 1

    previous: Decimal | None = None
    for price in reversed(close_values[:-1]):
        if price != current:
            previous = price
            break

    earlier = [o.price for o in obs[:-1]]
    average = money(Decimal(sum(close_values)) / len(close_values))
    mrp = next((o.mrp for o in reversed(obs) if o.mrp is not None), None)

    return PriceBaseline(
        listing_id=current_obs.listing_id,
        current=current,
        current_observed_at=current_obs.observed_at,
        previous=previous,
        minimum=min(o.price for o in obs),
        maximum=max(o.price for o in obs),
        average=average,
        median=money(Decimal(median(close_values))),
        first_observed_at=obs[0].observed_at,
        coverage_days=coverage_days,
        days_with_data=len(closes),
        observation_count=len(obs),
        tier=coverage_tier(coverage_days),
        trend=_trend(closes),
        change_vs_average_pct=_pct(current, average),
        change_vs_previous_pct=_pct(current, previous) if previous is not None else None,
        at_or_below_observed_min=bool(earlier) and current <= min(earlier),
        mrp=mrp,
        discount_vs_mrp_pct=money((mrp - current) / mrp * 100) if mrp else None,
    )


def describe_low(baseline: PriceBaseline) -> str | None:
    """Safe wording for a low price, or None when history can't support any claim."""
    if not baseline.at_or_below_observed_min or baseline.tier is CoverageTier.INSUFFICIENT:
        return None
    return (
        f"Lowest price in our {baseline.coverage_days}-day observed history "
        f"({baseline.observation_count} observations)."
    )


def detect_pre_sale_hike(
    observations: list[PriceObservation],
    sale_start: datetime,
    *,
    recent_days: int = 7,
    prior_days: int = 30,
    threshold_pct: Decimal = Decimal(5),
    min_days_each: int = 3,
) -> PreSaleHike:
    """Was the price raised in the days before a sale? Compares median daily prices."""
    start_day = _ist_date(sale_start)
    closes = daily_closes([o for o in observations if o.observed_at < sale_start])
    recent_from = start_day - timedelta(days=recent_days)
    prior_from = start_day - timedelta(days=prior_days)
    recent = [p for d, p in closes.items() if recent_from <= d < start_day]
    prior = [p for d, p in closes.items() if prior_from <= d < recent_from]
    if len(recent) < min_days_each or len(prior) < min_days_each:
        return PreSaleHike(
            detected=False,
            recent_median=None,
            prior_median=None,
            change_pct=None,
            reason="not enough pre-sale history to compare",
        )
    recent_med = money(Decimal(median(recent)))
    prior_med = money(Decimal(median(prior)))
    change = _pct(recent_med, prior_med)
    detected = change >= threshold_pct
    reason = (
        f"median price in the {recent_days} days before the sale ({recent_med:.0f}) is "
        f"{change}% above the earlier median ({prior_med:.0f}); compare sale prices with "
        f"{prior_med:.0f}, not the raised price"
        if detected
        else f"no meaningful pre-sale increase ({change}%)"
    )
    return PreSaleHike(
        detected=detected,
        recent_median=recent_med,
        prior_median=prior_med,
        change_pct=change,
        reason=reason,
    )
