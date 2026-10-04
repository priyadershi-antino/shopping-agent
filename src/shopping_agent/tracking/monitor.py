"""Check one watched item: refresh its price from the providers (quota-limited), compute the
best price for the user's own cards, compare with the previous check and record alerts.

Prices and offers come from the same tools the agent uses; nothing here involves the LLM.
"""

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from shopping_agent.db.models import ListingRow, WatchItemRow
from shopping_agent.db.repositories import CatalogRepository, SaleEventRepository
from shopping_agent.db.repositories.profile import ProfileRepository
from shopping_agent.db.repositories.tracking import AlertRepository, WatchRepository
from shopping_agent.domain.offer import EligibilityStatus, Offer, OfferType
from shopping_agent.domain.product import Availability, Marketplace
from shopping_agent.errors import ProviderError, QuotaExhaustedError, ShoppingAgentError
from shopping_agent.runtime import Runtime
from shopping_agent.services.alerts import (
    PRICE_KINDS,
    AlertCandidate,
    CheckSnapshot,
    WatchRules,
    evaluate,
)
from shopping_agent.services.formatting import format_inr
from shopping_agent.services.monitor_priority import (
    Priority,
    UserPriority,
    monitor_priority,
    plan_intervals,
)
from shopping_agent.services.price_baseline import IST
from shopping_agent.services.sale_mode import SaleStatus, sale_status
from shopping_agent.tools.common import ingest, ref_from_row
from shopping_agent.tools.deal_tools import FindOffersOutput

log = logging.getLogger(__name__)

RETRY_AFTER_ERROR = timedelta(hours=1)


@dataclass
class CheckOutcome:
    watch_id: str
    ok: bool
    listing_price: Decimal | None = None
    best_effective: Decimal | None = None
    alerts: list[str] = field(default_factory=list)
    error: str | None = None
    next_check_at: datetime | None = None


def offer_identity(offer: Offer) -> str:
    """Stable across amount changes: type + who runs it + card + title without numbers."""
    who = (offer.payment.issuer if offer.payment and offer.payment.issuer else offer.provider) or ""
    card = offer.payment.card_type.value if offer.payment else ""
    title = re.sub(r"[\d,.₹%]+", "#", offer.title.lower()).strip()
    return f"{offer.type.value}|{who.lower()}|{card}|{title}"


def describe_offer(offer: Offer) -> str:
    if offer.type is OfferType.EXCHANGE and offer.exchange:
        text = f"exchange: up to {format_inr(offer.exchange.max_value)}"
    elif offer.percent is not None:
        text = f"{offer.percent.normalize():f}% off"
        if offer.max_discount:
            text += f" up to {format_inr(offer.max_discount)}"
    else:
        text = f"{format_inr(offer.flat_amount or Decimal(0))} off"
    if offer.payment:
        text += f" with {offer.payment.describe()}"
    if offer.min_order_value:
        text += f" (min order {format_inr(offer.min_order_value)})"
    if offer.timing.value == "deferred":
        text += ", paid later as cashback"
    return text


def dedup_key(watch_id: str, alert: AlertCandidate, now: datetime) -> str:
    if alert.kind in PRICE_KINDS:
        return f"{watch_id}:{alert.kind.value}:{alert.key}:{now.astimezone(IST):%Y-%m-%d}"
    digest = hashlib.sha1(alert.key.encode(), usedforsecurity=False).hexdigest()[:20]
    return f"{watch_id}:{alert.kind.value}:{digest}"


def _next_ist_morning(now: datetime) -> datetime:
    local = now.astimezone(IST)
    tomorrow = (local + timedelta(days=1)).replace(hour=0, minute=10, second=0, microsecond=0)
    return tomorrow


async def refresh_listing(rt: Runtime, row: ListingRow, *, sale_active: bool) -> None:
    """Fetch the listing's current price and offers through the providers and store them."""
    if row.external_id:
        gathered = await rt.providers.lookup(
            Marketplace(row.marketplace), row.external_id, sale_active=sale_active
        )
    else:
        gathered = await rt.providers.product_listings(ref_from_row(row), sale_active=sale_active)
    if not gathered.value:
        if any("quota" in e.lower() for e in gathered.errors):
            raise QuotaExhaustedError("product providers")
        raise ProviderError("refresh", "; ".join(gathered.errors) or "listing not found")
    async with rt.sessions() as session, session.begin():
        await ingest(rt.tool_context(), session, gathered.value, replace_offers=True)


def _previous(watch: WatchItemRow) -> CheckSnapshot | None:
    if watch.last_price is None or watch.last_effective is None:
        return None
    return CheckSnapshot(
        listing_price=watch.last_price,
        best_effective=watch.last_effective,
        availability=Availability(watch.last_availability or "unknown"),
        offers=dict(watch.last_offers or {}),
    )


def _distance_pct(current: CheckSnapshot, watch: WatchItemRow) -> Decimal | None:
    gaps = []
    for target, value in (
        (watch.target_price, current.listing_price),
        (watch.target_effective, current.best_effective),
    ):
        if target:
            gaps.append(max((value - target) / target * 100, Decimal(0)))
    return min(gaps) if gaps else None


async def check_watch(rt: Runtime, watch_id: str, *, now: datetime) -> CheckOutcome:
    async with rt.sessions() as session:
        watch = await session.get(WatchItemRow, watch_id)
        if watch is None:
            return CheckOutcome(watch_id=watch_id, ok=False, error="not found")
        listing = await CatalogRepository(session).get_listing(watch.listing_id)
        events = await SaleEventRepository(session).list(ending_after=now - timedelta(days=3))
        profile = await ProfileRepository(session, watch.user_id).load()
    if listing is None:
        return CheckOutcome(watch_id=watch_id, ok=False, error="listing no longer exists")
    status = sale_status(events, now, platform=Marketplace(listing.marketplace))

    try:
        await refresh_listing(rt, listing, sale_active=status.is_sale_window)
        offers = await rt.tool_data(
            "find_offers", {"listing_id": listing.id}, FindOffersOutput, profile=profile
        )
    except QuotaExhaustedError as exc:
        return await record_error(rt, watch_id, str(exc), _next_ist_morning(now), now)
    except ShoppingAgentError as exc:
        return await record_error(rt, watch_id, str(exc), now + RETRY_AFTER_ERROR, now)

    best = min(
        (s for s in offers.scenarios if not s.conditional), key=lambda s: s.net_after_cashback
    )
    current = CheckSnapshot(
        listing_price=offers.listing.price,
        best_effective=best.net_after_cashback,
        effective_how=" + ".join(a.title for a in best.applied) or None,
        availability=offers.listing.availability,
        offers={
            offer_identity(v.offer): describe_offer(v.offer)
            for v in offers.offers
            if v.eligibility.status is not EligibilityStatus.INELIGIBLE
        },
    )
    return await _record_check(rt, watch_id, current, best.model_dump(mode="json"), status, now)


async def record_error(
    rt: Runtime, watch_id: str, error: str, retry_at: datetime, now: datetime
) -> CheckOutcome:
    async with rt.sessions() as session, session.begin():
        watch = await session.get(WatchItemRow, watch_id)
        if watch is not None:
            watch.last_error = error[:500]
            watch.last_checked_at = now
            watch.next_check_at = retry_at
    log.warning("watch check failed", extra={"watch_id": watch_id, "error": error[:200]})
    return CheckOutcome(watch_id=watch_id, ok=False, error=error, next_check_at=retry_at)


async def _record_check(
    rt: Runtime,
    watch_id: str,
    current: CheckSnapshot,
    scenario: dict[str, object],
    status: SaleStatus,
    now: datetime,
) -> CheckOutcome:
    settings = rt.settings
    created: list[str] = []
    async with rt.sessions() as session, session.begin():
        watch = await session.get(WatchItemRow, watch_id, with_for_update=True)
        if watch is None:
            return CheckOutcome(watch_id=watch_id, ok=False, error="not found")
        previous = _previous(watch)
        rules = WatchRules(
            label=watch.label,
            target_price=watch.target_price,
            target_effective=watch.target_effective,
            target_drop_pct=watch.target_drop_pct,
            reference_price=watch.reference_price,
            notify_back_in_stock=watch.notify_back_in_stock,
            notify_offer_changes=watch.notify_offer_changes,
        )
        alerts = AlertRepository(session, watch.user_id)
        cooldown = timedelta(hours=settings.alert_cooldown_hours)
        for candidate in evaluate(rules, previous, current):
            if candidate.kind in PRICE_KINDS and cooldown:
                last = await alerts.last_of_kind(watch.id, candidate.kind.value)
                if last is not None and now - last.created_at < cooldown:
                    continue
            row = await alerts.add(
                kind=candidate.kind.value,
                dedup_key=dedup_key(watch.id, candidate, now),
                title=candidate.title,
                message=candidate.message,
                now=now,
                watch_item_id=watch.id,
                data=candidate.data,
                channels=settings.notify_channel_list(),
            )
            if row is not None:
                created.append(row.title)

        repo = WatchRepository(session, watch.user_id)
        await repo.add_snapshot(
            watch.id,
            listing_price=current.listing_price,
            best_effective=current.best_effective,
            scenario=scenario,
            observed_at=now,
        )
        recent_change = (
            (current.listing_price - previous.listing_price) / previous.listing_price * 100
            if previous and previous.listing_price
            else None
        )
        watch.monitor_priority = monitor_priority(
            user_priority=UserPriority(watch.priority),
            distance_to_target_pct=_distance_pct(current, watch),
            recent_change_pct=recent_change,
        ).value
        priorities = await repo.active_priorities()
        priorities[watch.id] = watch.monitor_priority
        intervals = plan_intervals(
            {wid: Priority(p) for wid, p in priorities.items()},
            status.state,
            budget_per_hour=settings.monitor_checks_per_hour,
        )
        watch.last_price = current.listing_price
        watch.last_effective = current.best_effective
        watch.last_availability = current.availability.value
        watch.last_offers = current.offers
        watch.last_checked_at = now
        watch.last_error = None
        watch.check_count += 1
        watch.next_check_at = now + timedelta(minutes=intervals[watch.id])
        next_at = watch.next_check_at
    return CheckOutcome(
        watch_id=watch_id,
        ok=True,
        listing_price=current.listing_price,
        best_effective=current.best_effective,
        alerts=created,
        next_check_at=next_at,
    )
