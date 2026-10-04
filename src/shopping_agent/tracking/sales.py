"""Sale intelligence for the user: prep plan before a sale, a brief when it starts, and the
alerts that announce both (each fires once per event and phase)."""

from datetime import datetime, timedelta

from pydantic import BaseModel

from shopping_agent.db.models import AlertEventRow
from shopping_agent.db.repositories import CatalogRepository, PriceRepository, SaleEventRepository
from shopping_agent.db.repositories.profile import LOCAL_USER, ProfileRepository
from shopping_agent.db.repositories.tracking import AlertRepository, WatchRepository
from shopping_agent.domain.product import Marketplace
from shopping_agent.domain.sale import SaleEvent, SaleState
from shopping_agent.runtime import Runtime
from shopping_agent.services.alerts import AlertKind
from shopping_agent.services.price_baseline import IST, compute_baseline, detect_pre_sale_hike
from shopping_agent.services.sale_mode import sale_status
from shopping_agent.services.sale_prep import (
    SaleBrief,
    SalePrepPlan,
    WatchSummary,
    build_sale_brief,
    sale_prep_plan,
)

UPCOMING_NOTICE = timedelta(days=3)


def first_start(event: SaleEvent) -> datetime:
    return min([event.start, *(p.start for p in event.phases)])


async def watch_summaries(
    rt: Runtime,
    *,
    now: datetime,
    user_id: str = LOCAL_USER,
    sale_start: datetime | None = None,
    platform: Marketplace | None = None,
) -> list[WatchSummary]:
    async with rt.sessions() as session:
        watches = await WatchRepository(session, user_id).items(active_only=True)
        catalog = CatalogRepository(session)
        listings = {w.listing_id: await catalog.get_listing(w.listing_id) for w in watches}
        observations = await PriceRepository(session).history(
            [w.listing_id for w in watches], since=now - timedelta(days=120)
        )
    out: list[WatchSummary] = []
    for w in watches:
        row = listings.get(w.listing_id)
        if row is None or (platform is not None and row.marketplace != platform.value):
            continue
        obs = [o for o in observations if o.listing_id == w.listing_id]
        baseline = compute_baseline(obs)
        hike = detect_pre_sale_hike(obs, sale_start) if sale_start else None
        out.append(
            WatchSummary(
                watch_id=w.id,
                label=w.label,
                marketplace=Marketplace(row.marketplace),
                listing_price=row.current_price,
                best_effective=w.last_effective,
                target_price=w.target_price,
                target_effective=w.target_effective,
                history_days=baseline.coverage_days if baseline else 0,
                pre_sale_median=hike.prior_median if hike else None,
                pre_sale_rise=bool(hike and hike.detected),
            )
        )
    return out


async def prep_plan(
    rt: Runtime, *, now: datetime, platform: Marketplace | None = None, user_id: str = LOCAL_USER
) -> SalePrepPlan:
    async with rt.sessions() as session:
        events = await SaleEventRepository(session).list(ending_after=now)
        profile = await ProfileRepository(session, user_id).load()
    watch = await watch_summaries(rt, now=now, user_id=user_id)
    return sale_prep_plan(events=events, now=now, profile=profile, watch=watch, platform=platform)


async def brief_for(
    rt: Runtime, event: SaleEvent, *, now: datetime, user_id: str = LOCAL_USER
) -> SaleBrief:
    watch = await watch_summaries(
        rt, now=now, user_id=user_id, sale_start=first_start(event), platform=event.platform
    )
    return build_sale_brief(event, watch, live=sale_status([event], now).is_sale_window)


async def current_brief(
    rt: Runtime, *, now: datetime, user_id: str = LOCAL_USER
) -> SaleBrief | None:
    """Brief for the sale that is live now, or else the next one."""
    async with rt.sessions() as session:
        events = await SaleEventRepository(session).list(ending_after=now)
    if not events:
        return None
    tracked = {w.marketplace for w in await watch_summaries(rt, now=now, user_id=user_id)}
    # Live sales first, then the next one; prefer platforms where something is tracked.
    event = min(
        events,
        key=lambda e: (
            not sale_status([e], now).is_sale_window,
            e.platform not in tracked,
            first_start(e),
        ),
    )
    return await brief_for(rt, event, now=now, user_id=user_id)


async def check_sales(
    rt: Runtime, *, now: datetime, user_id: str = LOCAL_USER
) -> list[AlertEventRow]:
    """Announce a sale a few days before it starts (with the prep plan) and when each phase
    starts (with the brief). Dedup keys make each announcement happen once."""
    async with rt.sessions() as session:
        events = await SaleEventRepository(session).list(ending_after=now)
    created: list[AlertEventRow] = []
    data: BaseModel
    channels = rt.settings.notify_channel_list()
    for event in events:
        status = sale_status([event], now)
        unconfirmed = "" if status.verified else " (dates not confirmed by an official source)"
        where = event.platform.value.replace("_", " ").title()
        if status.state in {SaleState.EARLY_ACCESS, SaleState.ACTIVE}:
            phase = status.phase.name if status.phase else "main"
            brief = await brief_for(rt, event, now=now, user_id=user_id)
            early = status.state is SaleState.EARLY_ACCESS
            title = f"{event.name} is live on {where}" + (" (early access)" if early else "")
            message = "\n".join(
                [brief.summary + unconfirmed, *(f"• {i.line}" for i in brief.items)]
            )
            kind, key, data = AlertKind.SALE_START, f"sale_start:{event.id}:{phase}", brief
        elif status.state is SaleState.PRE_SALE and first_start(event) - now <= UPCOMING_NOTICE:
            plan = await prep_plan(rt, now=now, platform=event.platform, user_id=user_id)
            open_steps = [s for s in plan.steps if s.done is False]
            when = first_start(event).astimezone(IST).strftime("%d %b")
            title = f"{event.name} on {where} starts {when}"
            message = "\n".join([plan.summary, *(f"• {s.title}: {s.detail}" for s in open_steps)])
            kind, key, data = AlertKind.SALE_UPCOMING, f"sale_upcoming:{event.id}", plan
        else:
            continue
        async with rt.sessions() as session, session.begin():
            row = await AlertRepository(session, user_id).add(
                kind=kind.value,
                dedup_key=key,
                title=title,
                message=message,
                now=now,
                data=data.model_dump(mode="json"),
                channels=channels,
            )
        if row is not None:
            created.append(row)
    return created
