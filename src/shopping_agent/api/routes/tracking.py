"""Tracking and sale intelligence: confirm proposed actions, watchlist, alerts, notifications,
sale prep and the sale-start brief."""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

from shopping_agent.agent.state import ProposedAction
from shopping_agent.api.deps import get_runtime, require_api_key
from shopping_agent.db.repositories import SaleEventRepository
from shopping_agent.db.repositories.tracking import (
    AlertRepository,
    NotificationRepository,
    WatchRepository,
)
from shopping_agent.domain.common import Verification
from shopping_agent.domain.product import Marketplace
from shopping_agent.domain.sale import PhaseKind, SaleEvent, SalePhase
from shopping_agent.runtime import Runtime
from shopping_agent.services.monitor_priority import UserPriority
from shopping_agent.services.sale_prep import SaleBrief, SalePrepPlan
from shopping_agent.tools.catalog_tools import ResolveLinkOutput
from shopping_agent.tracking.actions import ConfirmOverrides, confirm_action, dismiss_action
from shopping_agent.tracking.monitor import check_watch
from shopping_agent.tracking.queries import watch_views
from shopping_agent.tracking.sales import current_brief, prep_plan
from shopping_agent.tracking.views import AlertView, NotificationView, WatchView
from shopping_agent.tracking.worker import TickReport, run_once

router = APIRouter(tags=["tracking & sales"], dependencies=[Depends(require_api_key)])


# --- proposed actions ---


class ActionResult(BaseModel):
    action: ProposedAction
    result: Any = None


@router.post("/runs/{run_id}/actions/{action_id}/confirm")
async def confirm(
    run_id: str,
    action_id: str,
    body: ConfirmOverrides | None = Body(default=None),
    runtime: Runtime = Depends(get_runtime),
) -> ActionResult:
    """Carry out an action the agent proposed (track a price, set an alert, save a preference)."""
    action, data = await confirm_action(runtime, run_id, action_id, body)
    return ActionResult(
        action=action, result=data.model_dump(mode="json") if isinstance(data, BaseModel) else data
    )


@router.post("/runs/{run_id}/actions/{action_id}/dismiss")
async def dismiss(
    run_id: str, action_id: str, runtime: Runtime = Depends(get_runtime)
) -> ActionResult:
    return ActionResult(action=await dismiss_action(runtime, run_id, action_id))


# --- watchlist ---


class WatchIn(BaseModel):
    listing_id: str | None = None
    url: str | None = Field(
        default=None, max_length=2000, description="Product link instead of an id"
    )
    target_price: Decimal | None = Field(default=None, gt=0, description="Listed price target")
    target_effective: Decimal | None = Field(
        default=None, gt=0, description="Target for the best price with your cards"
    )
    target_drop_pct: Decimal | None = Field(default=None, gt=0, lt=90)
    priority: UserPriority = UserPriority.NORMAL

    @model_validator(mode="after")
    def _one_ref(self) -> "WatchIn":
        if bool(self.listing_id) == bool(self.url):
            raise ValueError("give listing_id or url")
        return self


class WatchPatch(BaseModel):
    target_price: Decimal | None = Field(default=None, gt=0)
    target_effective: Decimal | None = Field(default=None, gt=0)
    target_drop_pct: Decimal | None = Field(default=None, gt=0, lt=90)
    priority: UserPriority | None = None
    notify_back_in_stock: bool | None = None
    notify_offer_changes: bool | None = None
    notify_sale_start: bool | None = None
    active: bool | None = None
    clear_targets: bool = Field(default=False, description="Remove all targets first")


async def listing_for(runtime: Runtime, listing_id: str | None, url: str | None) -> str:
    if listing_id:
        return listing_id
    out = await runtime.tool_data("resolve_product_link", {"url": url}, ResolveLinkOutput)
    if out.status != "resolved" or not out.listings:
        raise HTTPException(400, out.message)
    return out.listings[0].listing_id


@router.get("/watchlist")
async def watchlist(runtime: Runtime = Depends(get_runtime)) -> list[WatchView]:
    async with runtime.sessions() as session:
        rows = await WatchRepository(session).items()
    return await watch_views(runtime, rows)


@router.post("/watchlist")
async def add_watch(body: WatchIn, runtime: Runtime = Depends(get_runtime)) -> WatchView:
    """Track a listing. You are asking directly, so no separate confirmation is needed."""
    lid = await listing_for(runtime, body.listing_id, body.url)
    args: dict[str, Any] = {"listing_id": lid, "priority": body.priority.value}
    for key in ("target_price", "target_effective", "target_drop_pct"):
        if (value := getattr(body, key)) is not None:
            args[key] = str(value)
    return await runtime.tool_data("track_product", args, WatchView, allow_side_effects=True)


@router.patch("/watchlist/{watch_id}")
async def update_watch(
    watch_id: str, body: WatchPatch, runtime: Runtime = Depends(get_runtime)
) -> WatchView:
    fields = body.model_dump(exclude_none=True, exclude={"clear_targets"})
    if "priority" in fields:
        fields["priority"] = body.priority.value if body.priority else "normal"
    async with runtime.sessions() as session, session.begin():
        repo = WatchRepository(session)
        if body.clear_targets:
            await repo.update(
                watch_id, target_price=None, target_effective=None, target_drop_pct=None
            )
        row = await repo.update(watch_id, **fields)
        if row is None:
            raise HTTPException(404, "watch item not found")
    return (await watch_views(runtime, [row]))[0]


@router.delete("/watchlist/{watch_id}")
async def remove_watch(watch_id: str, runtime: Runtime = Depends(get_runtime)) -> dict[str, bool]:
    async with runtime.sessions() as session, session.begin():
        if not await WatchRepository(session).delete(watch_id):
            raise HTTPException(404, "watch item not found")
    return {"deleted": True}


class CheckResult(BaseModel):
    ok: bool
    listing_price: Decimal | None
    best_effective: Decimal | None
    alerts: list[str]
    error: str | None
    next_check_at: datetime | None


@router.post("/watchlist/{watch_id}/check")
async def check_now(watch_id: str, runtime: Runtime = Depends(get_runtime)) -> CheckResult:
    """Check the price now instead of waiting for the worker."""
    async with runtime.sessions() as session:
        if await WatchRepository(session).get(watch_id) is None:
            raise HTTPException(404, "watch item not found")
    o = await check_watch(runtime, watch_id, now=runtime.clock())
    return CheckResult(
        ok=o.ok,
        listing_price=o.listing_price,
        best_effective=o.best_effective,
        alerts=o.alerts,
        error=o.error,
        next_check_at=o.next_check_at,
    )


class SnapshotView(BaseModel):
    observed_at: datetime
    listing_price: Decimal
    best_effective: Decimal


@router.get("/watchlist/{watch_id}/effective-history")
async def effective_history(
    watch_id: str,
    days: int = Query(default=90, ge=1, le=365),
    runtime: Runtime = Depends(get_runtime),
) -> list[SnapshotView]:
    async with runtime.sessions() as session:
        repo = WatchRepository(session)
        if await repo.get(watch_id) is None:
            raise HTTPException(404, "watch item not found")
        rows = await repo.snapshots(watch_id, since=runtime.clock() - timedelta(days=days))
    return [
        SnapshotView(
            observed_at=r.observed_at,
            listing_price=r.listing_price,
            best_effective=r.best_effective,
        )
        for r in rows
    ]


# --- alerts and notifications ---


@router.get("/alerts")
async def alerts(
    limit: int = Query(default=50, ge=1, le=200), runtime: Runtime = Depends(get_runtime)
) -> list[AlertView]:
    async with runtime.sessions() as session:
        return [AlertView.of(a) for a in await AlertRepository(session).recent(limit=limit)]


class Inbox(BaseModel):
    unread: int
    items: list[NotificationView]


@router.get("/notifications")
async def notifications(
    unread_only: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
    runtime: Runtime = Depends(get_runtime),
) -> Inbox:
    async with runtime.sessions() as session:
        repo = NotificationRepository(session)
        items = await repo.inbox(unread_only=unread_only, limit=limit)
        return Inbox(
            unread=await repo.unread_count(), items=[NotificationView.of(n, a) for n, a in items]
        )


class ReadIn(BaseModel):
    id: str | None = Field(default=None, description="One notification; omit for all")


@router.post("/notifications/read")
async def mark_read(body: ReadIn, runtime: Runtime = Depends(get_runtime)) -> dict[str, int]:
    async with runtime.sessions() as session, session.begin():
        count = await NotificationRepository(session).mark_read(runtime.clock(), body.id)
    return {"marked": count}


# --- sales ---


@router.get("/sale-prep")
async def sale_prep(
    platform: Marketplace | None = None, runtime: Runtime = Depends(get_runtime)
) -> SalePrepPlan:
    """A checklist for the next sale: dates, tracking, baselines, targets, cards, early access."""
    return await prep_plan(runtime, now=runtime.clock(), platform=platform)


@router.get("/sale-brief")
async def sale_brief(runtime: Runtime = Depends(get_runtime)) -> SaleBrief:
    """Each watched item against its pre-sale price, for the live (or next) sale."""
    brief = await current_brief(runtime, now=runtime.clock())
    if brief is None:
        raise HTTPException(404, "no upcoming or live sale is recorded")
    return brief


class SaleEventIn(BaseModel):
    name: str = Field(min_length=3, max_length=200)
    platform: Marketplace
    start: datetime
    end: datetime
    early_access_hours: int = Field(default=0, ge=0, le=72)
    early_access_for: str | None = Field(default=None, description="e.g. flipkart plus")
    partner_banks: list[str] = Field(default_factory=list)
    official_sources: list[str] = Field(default_factory=list, description="Where you saw the dates")

    @model_validator(mode="after")
    def _order(self) -> "SaleEventIn":
        if self.end <= self.start:
            raise ValueError("end must be after start")
        if self.start.tzinfo is None:
            raise ValueError("give times with a timezone, e.g. 2026-10-10T00:00:00+05:30")
        return self


@router.post("/sale-events")
async def add_sale_event(body: SaleEventIn, runtime: Runtime = Depends(get_runtime)) -> SaleEvent:
    """Record a sale you know about. It is marked user-provided, not verified."""
    phases = [SalePhase(name="Main sale", kind=PhaseKind.MAIN, start=body.start, end=body.end)]
    if body.early_access_hours:
        phases.insert(
            0,
            SalePhase(
                name="Early access",
                kind=PhaseKind.EARLY_ACCESS,
                start=body.start - timedelta(hours=body.early_access_hours),
                end=body.start,
                eligibility=body.early_access_for,
            ),
        )
    event = SaleEvent(
        name=body.name,
        platform=body.platform,
        start=body.start,
        end=body.end,
        phases=phases,
        partner_banks=body.partner_banks,
        official_sources=body.official_sources,
        verification=Verification.USER_PROVIDED,
        last_verified_at=runtime.clock(),
    )
    async with runtime.sessions() as session, session.begin():
        event.id = await SaleEventRepository(session).upsert(event)
    return event


@router.post("/worker/run-once")
async def worker_tick(runtime: Runtime = Depends(get_runtime)) -> TickReport:
    """Run one worker tick now: sale announcements, due price checks, deliveries."""
    return await run_once(runtime)
