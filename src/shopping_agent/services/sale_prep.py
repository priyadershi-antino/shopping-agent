"""Sale Prep Plan (before a sale) and the sale-start brief (when it begins). Deterministic:
built from recorded sale events, the user's profile and the watchlist."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from shopping_agent.domain.common import Verification, money
from shopping_agent.domain.offer import UserProfile
from shopping_agent.domain.product import Marketplace
from shopping_agent.domain.sale import PhaseKind, SaleEvent, SaleState
from shopping_agent.services.formatting import format_inr
from shopping_agent.services.offer_engine import issuer_matches
from shopping_agent.services.price_baseline import IST
from shopping_agent.services.sale_mode import sale_status

MIN_BASELINE_DAYS = 14


class WatchSummary(BaseModel):
    """A watched item as the planner needs it."""

    watch_id: str
    label: str
    marketplace: Marketplace
    listing_price: Decimal
    best_effective: Decimal | None = None
    target_price: Decimal | None = None
    target_effective: Decimal | None = None
    history_days: int = 0
    pre_sale_median: Decimal | None = None  # median listed price before the pre-sale window
    pre_sale_rise: bool = False


class PrepStep(BaseModel):
    title: str
    detail: str
    done: bool | None  # None = advice, nothing to tick off


class SalePrepPlan(BaseModel):
    event: SaleEvent | None
    state: SaleState
    starts_in_days: float | None
    verified: bool
    steps: list[PrepStep] = Field(default_factory=list)
    summary: str


def _platform(event: SaleEvent) -> str:
    return event.platform.value.replace("_", " ").title()


def _when(ts: datetime) -> str:
    return ts.astimezone(IST).strftime("%d %b, %I:%M %p IST")


def sale_prep_plan(
    *,
    events: list[SaleEvent],
    now: datetime,
    profile: UserProfile,
    watch: list[WatchSummary],
    platform: Marketplace | None = None,
) -> SalePrepPlan:
    upcoming = [e for e in events if e.end > now and (platform is None or e.platform is platform)]
    if platform is None and watch:
        # Prefer a sale on a platform where the user tracks something.
        tracked = {w.marketplace for w in watch}
        upcoming = [e for e in upcoming if e.platform in tracked] or upcoming
    status = sale_status(upcoming, now, platform=platform)
    event = status.event or min(upcoming, key=lambda e: e.start, default=None)
    if event is None:
        return SalePrepPlan(
            event=None,
            state=status.state,
            starts_in_days=None,
            verified=False,
            summary=(
                "No upcoming sale is recorded. Track the products you want now, so there is a "
                "price history to compare against when a sale is announced."
            ),
            steps=[
                PrepStep(
                    title="Track what you want to buy",
                    detail="Price history built before a sale shows whether a sale price is "
                    "really lower.",
                    done=bool(watch),
                )
            ],
        )

    first_start = min([event.start, *(p.start for p in event.phases)])
    starts_in = round((first_start - now).total_seconds() / 86400, 1)
    verified = event.verification is Verification.VERIFIED
    relevant = [w for w in watch if w.marketplace is event.platform]
    steps: list[PrepStep] = []

    steps.append(
        PrepStep(
            title="Confirm the sale dates",
            detail=(
                f"Dates verified from {', '.join(event.official_sources)}."
                if verified and event.official_sources
                else "These dates are not confirmed by an official source yet; check the "
                f"{_platform(event)} app or site before planning around them."
            ),
            done=verified,
        )
    )
    steps.append(
        PrepStep(
            title="Shortlist and track the products",
            detail=(
                f"You are tracking {len(relevant)} item(s) for this sale."
                if relevant
                else f"Nothing on {_platform(event)} is tracked yet. Track each product you are "
                "considering (one exact variant each) so price changes are recorded."
            ),
            done=bool(relevant),
        )
    )
    thin = [w for w in relevant if w.history_days < MIN_BASELINE_DAYS]
    steps.append(
        PrepStep(
            title="Build a price baseline",
            detail=(
                "Every tracked item has at least two weeks of price history."
                if relevant and not thin
                else "Too little history for: "
                + ", ".join(f"{w.label} ({w.history_days} days)" for w in thin)
                + ". Without it, a raised-then-discounted price can look like a deal."
                if thin
                else "Start tracking to build history."
            ),
            done=bool(relevant) and not thin,
        )
    )
    no_target = [w for w in relevant if w.target_price is None and w.target_effective is None]
    steps.append(
        PrepStep(
            title="Set target prices",
            detail=(
                "Every tracked item has a target, so you will be alerted when it is met."
                if relevant and not no_target
                else "No target yet for: " + ", ".join(w.label for w in no_target) + "."
                if no_target
                else "Set a target price for each tracked item."
            ),
            done=bool(relevant) and not no_target,
        )
    )
    if event.partner_banks:
        matching = [
            b
            for b in event.partner_banks
            if any(issuer_matches(b, m.issuer) for m in profile.payment_methods)
        ]
        if not profile.payment_methods:
            detail = (
                f"Partner banks: {', '.join(event.partner_banks)}. Add your cards (bank and "
                "credit/debit only) to your profile to see which offers apply to you."
            )
        elif matching:
            detail = (
                f"Partner banks: {', '.join(event.partner_banks)}. Your matching card(s): "
                f"{', '.join(matching)}. Check each offer's minimum order value and cap."
            )
        else:
            detail = (
                f"Partner banks: {', '.join(event.partner_banks)}. None of your saved cards "
                "match, so instant bank discounts are unlikely to apply to you."
            )
        steps.append(PrepStep(title="Check bank offers", detail=detail, done=bool(matching)))
    early = [p for p in event.phases if p.kind is PhaseKind.EARLY_ACCESS]
    if early:
        phase = early[0]
        member = phase.eligibility
        has = member is not None and member.lower() in {m.lower() for m in profile.memberships}
        steps.append(
            PrepStep(
                title="Early access",
                detail=(
                    f"Early access starts {_when(phase.start)}"
                    + (f" for {member} members" if member else "")
                    + (". You have this membership." if has else ".")
                ),
                done=has if member else None,
            )
        )
    steps.append(
        PrepStep(
            title="Exchange",
            detail=(
                "Device on file: "
                + ", ".join(
                    f"{d.category} {d.model or ''}".strip() for d in profile.exchange_devices
                )
                + ". Exchange values depend on the device's condition, so treat them as a range."
                if profile.exchange_devices
                else "If you will trade in a device, add it to your profile so exchange offers "
                "can be shown (as a range that depends on its condition)."
            ),
            done=bool(profile.exchange_devices) or None,
        )
    )
    steps.append(
        PrepStep(
            title="On sale day",
            detail=(
                "Compare the sale price with the pre-sale price history, not the MRP. Bank "
                "discounts usually need the full amount on one card and have caps. Nothing "
                "here ever needs card numbers, CVV or OTPs."
            ),
            done=None,
        )
    )
    when = "is live" if status.is_sale_window else f"starts {_when(first_start)}"
    where = f" on {_platform(event)}"
    unconfirmed = "" if verified else " (dates unconfirmed)"
    open_steps = sum(1 for s in steps if s.done is False)
    return SalePrepPlan(
        event=event,
        state=status.state,
        starts_in_days=starts_in if starts_in > 0 else None,
        verified=verified,
        steps=steps,
        summary=f"{event.name}{where} {when}{unconfirmed}. "
        f"{open_steps} preparation step(s) still open.",
    )


class BriefItem(BaseModel):
    watch_id: str
    label: str
    listing_price: Decimal
    best_effective: Decimal | None
    pre_sale_median: Decimal | None
    change_vs_pre_sale_pct: Decimal | None
    target_met: bool
    pre_sale_rise: bool
    line: str


class SaleBrief(BaseModel):
    event_name: str
    items: list[BriefItem]
    summary: str


def build_sale_brief(
    event: SaleEvent, watch: list[WatchSummary], *, live: bool = True
) -> SaleBrief:
    """Each watched item against its pre-sale baseline. Sent when a sale starts (`live`), and
    available before it as a preview."""
    items: list[BriefItem] = []
    for w in watch:
        price_for_you = w.best_effective if w.best_effective is not None else w.listing_price
        # Like for like: the listed price now against the listed price before the sale.
        change = (
            money((w.listing_price - w.pre_sale_median) / w.pre_sale_median * 100)
            if w.pre_sale_median
            else None
        )
        met = (w.target_effective is not None and price_for_you <= w.target_effective) or (
            w.target_price is not None and w.listing_price <= w.target_price
        )
        line = f"{w.label}: listed {format_inr(w.listing_price)}"
        if change is not None and w.pre_sale_median is not None:
            if change == 0:
                line += f", same as its pre-sale median {format_inr(w.pre_sale_median)}"
            else:
                direction = "below" if change < 0 else "above"
                line += (
                    f", {abs(change)}% {direction} its pre-sale median "
                    f"{format_inr(w.pre_sale_median)}"
                )
        else:
            line += " (too little history to compare with before the sale)"
        if w.pre_sale_rise:
            line += "; the price was raised shortly before the sale"
        if price_for_you != w.listing_price:
            line += f". {format_inr(price_for_you)} for you with your cards"
        if met:
            line += " (target met)"
        line += "."
        items.append(
            BriefItem(
                watch_id=w.watch_id,
                label=w.label,
                listing_price=w.listing_price,
                best_effective=w.best_effective,
                pre_sale_median=w.pre_sale_median,
                change_vs_pre_sale_pct=change,
                target_met=met,
                pre_sale_rise=w.pre_sale_rise,
                line=line,
            )
        )
    met_count = sum(1 for i in items if i.target_met)
    first = min([event.start, *(p.start for p in event.phases)])
    status = "has started" if live else f"starts {_when(first)}"
    head = f"{event.name} on {_platform(event)} {status}."
    summary = (
        f"{head} {len(items)} tracked item(s); {met_count} at or below your target."
        if items
        else f"{head} You are not tracking any items on {_platform(event)}."
    )
    return SaleBrief(event_name=event.name, items=items, summary=summary)
