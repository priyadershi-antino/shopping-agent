"""Buy-now-or-monitor analysis from evidence. No price predictions."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

from shopping_agent.domain.price import CoverageTier, PreSaleHike, PriceBaseline, Trend
from shopping_agent.domain.product import Availability
from shopping_agent.domain.sale import SaleState
from shopping_agent.services.formatting import format_inr
from shopping_agent.services.price_baseline import describe_low
from shopping_agent.services.sale_mode import SaleStatus


class TimingAdvice(StrEnum):
    TARGET_MET = "target_met"
    CONSIDER_BUYING = "consider_buying"
    SET_ALERT = "set_alert"
    CONSIDER_WAITING_FOR_SALE = "consider_waiting_for_sale"
    NEED_MORE_DATA = "need_more_data"


class TimingSignal(BaseModel):
    name: str
    detail: str


class TimingAnalysis(BaseModel):
    advice: TimingAdvice
    suggested_alert_price: Decimal | None = None
    signals: list[TimingSignal] = Field(default_factory=list)
    summary: str


def analyze_timing(
    *,
    current_effective: Decimal,
    target: Decimal | None,
    baseline: PriceBaseline | None,
    sale: SaleStatus | None,
    now: datetime,
    offer_expires_at: datetime | None = None,
    availability: Availability = Availability.UNKNOWN,
    pre_sale_hike: PreSaleHike | None = None,
) -> TimingAnalysis:
    signals: list[TimingSignal] = []
    parts = [f"Current effective price is {format_inr(current_effective)}."]
    if target is not None:
        parts.append(f"Your target is {format_inr(target)}.")

    usable_history = baseline is not None and baseline.tier is not CoverageTier.INSUFFICIENT
    if baseline is not None:
        parts.append(
            f"The listed price over our {baseline.coverage_days} observed days ranged "
            f"{format_inr(baseline.minimum)}–{format_inr(baseline.maximum)} "
            f"({baseline.tier.value} coverage)."
        )
        if not usable_history:
            signals.append(
                TimingSignal(
                    name="insufficient_history",
                    detail=f"only {baseline.coverage_days} days of observations",
                )
            )
        if low := describe_low(baseline):
            signals.append(TimingSignal(name="at_observed_low", detail=low))
        if baseline.trend in {Trend.RISING, Trend.FALLING}:
            signals.append(
                TimingSignal(
                    name=f"recent_trend_{baseline.trend.value}", detail="last 7 days vs prior 7"
                )
            )
    if pre_sale_hike is not None and pre_sale_hike.detected and pre_sale_hike.prior_median:
        signals.append(TimingSignal(name="pre_sale_price_rise", detail=pre_sale_hike.reason))
        parts.append(
            f"The listed price was raised before the sale (about {pre_sale_hike.change_pct}% "
            f"above its earlier level of {format_inr(pre_sale_hike.prior_median)}); judge sale "
            f"discounts against {format_inr(pre_sale_hike.prior_median)}, not the raised price."
        )

    if sale is not None and sale.event is not None:
        verified = "verified" if sale.verified else "unverified"
        if sale.is_sale_window:
            signals.append(
                TimingSignal(name="sale_active", detail=f"{sale.event.name} is live ({verified})")
            )
        elif sale.state is SaleState.PRE_SALE:
            signals.append(
                TimingSignal(
                    name="sale_upcoming",
                    detail=f"{sale.event.name} starts {sale.event.start:%d %b %Y} ({verified})",
                )
            )
    if offer_expires_at is not None and offer_expires_at > now:
        hours = (offer_expires_at - now).total_seconds() / 3600
        if hours <= 48:
            signals.append(
                TimingSignal(name="offer_expiring", detail=f"best offer ends in {hours:.0f} hours")
            )
    if availability in {Availability.LIMITED, Availability.OUT_OF_STOCK}:
        signals.append(TimingSignal(name="availability", detail=availability.value))

    suggested: Decimal | None = None
    if target is not None and current_effective <= target:
        advice = TimingAdvice.TARGET_MET
        parts.append("Your target price is met.")
    elif sale is not None and sale.state is SaleState.PRE_SALE:
        advice = TimingAdvice.CONSIDER_WAITING_FOR_SALE
        suggested = target
        parts.append(
            "A sale is scheduled soon; prices may or may not drop, so compare sale prices "
            "with this baseline."
        )
    elif usable_history and baseline is not None and baseline.at_or_below_observed_min:
        advice = TimingAdvice.CONSIDER_BUYING
        parts.append("The price is at the low end of what we have observed.")
    elif target is not None:
        advice = TimingAdvice.SET_ALERT
        suggested = target
        parts.append(f"A price alert at {format_inr(target)} would be useful.")
    elif usable_history and baseline is not None and baseline.minimum < current_effective:
        advice = TimingAdvice.SET_ALERT
        suggested = baseline.minimum
        parts.append(
            f"The price has been as low as {format_inr(baseline.minimum)} in our observations; "
            f"an alert at that level would be useful."
        )
    else:
        advice = TimingAdvice.NEED_MORE_DATA
        parts.append("There isn't enough price history yet; tracking this product will build it.")

    return TimingAnalysis(
        advice=advice, suggested_alert_price=suggested, signals=signals, summary=" ".join(parts)
    )
