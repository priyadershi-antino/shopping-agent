"""Minimal server-rendered web UI (no build step). Data comes from the same runtime and tools
as the API; interactive bits (streaming chat, profile edits, chart hover) are in app.js."""

import asyncio
import hmac
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import select

from shopping_agent.agent.store import SqlRunStore
from shopping_agent.agent.views import run_view
from shopping_agent.api.deps import API_KEY_COOKIE, get_runtime, require_api_key
from shopping_agent.db.models import AgentRunRow
from shopping_agent.db.repositories import CatalogRepository
from shopping_agent.db.repositories.profile import KNOWN_PREFERENCES, ProfileRepository
from shopping_agent.db.repositories.tracking import NotificationRepository, WatchRepository
from shopping_agent.errors import ShoppingAgentError
from shopping_agent.runtime import Runtime
from shopping_agent.services.budget_planner import (
    ItemParseError,
    SortOrder,
    parse_items,
    plan_budget,
)
from shopping_agent.services.formatting import format_inr
from shopping_agent.services.india_price_parser import parse_amount
from shopping_agent.tools.base import ToolResult
from shopping_agent.tools.common import summarize
from shopping_agent.tracking.queries import effective_daily, watch_views
from shopping_agent.tracking.sales import current_brief, prep_plan
from shopping_agent.tracking.views import NotificationView
from shopping_agent.web.charts import Series, price_chart

HERE = Path(__file__).parent
STATIC_DIR = HERE / "static"
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters["inr"] = lambda v: format_inr(v) if v is not None else "–"

TOOL_LABELS = {
    "resolve_product_link": "Reading the product link",
    "search_products": "Searching products",
    "get_product_details": "Checking specifications",
    "get_listings": "Comparing sellers",
    "find_offers": "Checking offers for your cards",
    "research_reviews": "Reading reviews",
    "get_price_history": "Checking price history",
    "get_sale_events": "Checking sale events",
    "find_alternatives": "Looking for alternatives",
    "search_web": "Searching the web",
    "fetch_page": "Reading a page",
}
NAV_FOR = {
    "index.html": "ask",
    "run_page.html": "ask",
    "link.html": "link",
    "listing.html": "link",
    "profile.html": "profile",
    "watchlist.html": "watch",
    "alerts.html": "alerts",
    "budget.html": "budget",
}


def require_web_key(request: Request) -> None:
    """Like require_api_key, but sends browsers to the key page instead of a JSON 401."""
    try:
        require_api_key(request)
    except HTTPException as exc:
        raise HTTPException(303, headers={"Location": "/ui/key"}) from exc


# Platforms offered as checkboxes on the ask form (manufacturer pages are not a shopping source).
PLATFORM_LABELS = {
    "amazon": "Amazon",
    "flipkart": "Flipkart",
    "croma": "Croma",
    "reliance_digital": "Reliance Digital",
    "vijay_sales": "Vijay Sales",
    "tata_cliq": "Tata CLiQ",
    "other": "Other sellers",
}

router = APIRouter(include_in_schema=False)
pages = APIRouter(include_in_schema=False, dependencies=[Depends(require_web_key)])


def page(request: Request, name: str, **context: Any) -> HTMLResponse:
    runtime = get_runtime(request)
    selected = runtime.settings.default_marketplaces()
    return templates.TemplateResponse(
        request,
        name,
        {
            "demo": runtime.providers.demo_mode,
            "tool_labels": TOOL_LABELS,
            "platform_options": [
                {"value": v, "label": label, "checked": selected is None or v in selected}
                for v, label in PLATFORM_LABELS.items()
            ],
            "nav": NAV_FOR.get(name),
            **context,
        },
    )


class KeyIn(BaseModel):
    value: str = Field(min_length=1, max_length=500)


@router.get("/ui/key")
async def key_page(request: Request) -> HTMLResponse:
    return page(request, "key.html")


@router.post("/ui/key")
async def set_key(body: KeyIn, request: Request) -> Response:
    """For deployments with API_KEY set: check the key and keep it in an HTTP-only cookie."""
    expected = get_runtime(request).settings.api_key.get_secret_value()
    if expected and not hmac.compare_digest(body.value.encode(), expected.encode()):
        raise HTTPException(401, "wrong access key")
    response = JSONResponse({"ok": True})
    response.set_cookie(
        API_KEY_COOKIE,
        body.value,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
    )
    return response


@pages.get("/")
async def index(request: Request, runtime: Runtime = Depends(get_runtime)) -> HTMLResponse:
    async with runtime.sessions() as session:
        recent = list(
            await session.scalars(
                select(AgentRunRow).order_by(AgentRunRow.created_at.desc()).limit(8)
            )
        )
    return page(request, "index.html", recent=recent)


@pages.get("/runs/{run_id}")
async def run_page(
    run_id: str, request: Request, runtime: Runtime = Depends(get_runtime)
) -> HTMLResponse:
    state = await SqlRunStore(runtime.sessions).load(run_id)
    if state is None:
        raise HTTPException(404, "run not found")
    return page(request, "run_page.html", run=run_view(state))


@pages.get("/ui/runs/{run_id}")
async def run_fragment(
    run_id: str, request: Request, runtime: Runtime = Depends(get_runtime)
) -> HTMLResponse:
    state = await SqlRunStore(runtime.sessions).load(run_id)
    if state is None:
        raise HTTPException(404, "run not found")
    return page(request, "_run.html", run=run_view(state))


@pages.get("/link")
async def link(
    request: Request,
    url: str | None = Query(default=None, max_length=2000),
    runtime: Runtime = Depends(get_runtime),
) -> Response:
    if not url:
        return page(request, "link.html", url=None, message=None, data=None)
    try:
        result = await runtime.call_tool("resolve_product_link", {"url": url})
    except ShoppingAgentError as exc:
        return page(request, "link.html", url=url, message=str(exc), data=None)
    data = result.data
    if data is not None and data.status == "resolved" and data.listings:
        return RedirectResponse(f"/listing/{data.listings[0].listing_id}", status_code=303)
    message = data.message if data else "; ".join(result.errors) or "Could not read that link."
    return page(request, "link.html", url=url, message=message, data=data)


async def _safe(coro: Any) -> ToolResult[Any] | None:
    try:
        result: ToolResult[Any] = await coro
        return result
    except ShoppingAgentError:
        return None


@pages.get("/listing/{listing_id}")
async def listing_page(
    listing_id: str, request: Request, days: int = 90, runtime: Runtime = Depends(get_runtime)
) -> HTMLResponse:
    async with runtime.sessions() as session:
        row = await CatalogRepository(session).get_listing(listing_id)
    if row is None:
        raise HTTPException(404, "listing not found")
    profile = await runtime.load_profile()
    sellers, offers, history, details = await asyncio.gather(
        _safe(runtime.call_tool("get_listings", {"listing_id": listing_id})),
        _safe(runtime.call_tool("find_offers", {"listing_id": listing_id}, profile=profile)),
        _safe(runtime.call_tool("get_price_history", {"listing_id": listing_id, "days": days})),
        _safe(runtime.call_tool("get_product_details", {"listing_id": listing_id})),
    )
    async with runtime.sessions() as session:
        row = await CatalogRepository(session).get_listing(listing_id) or row
        watch_row = await WatchRepository(session).for_listing(listing_id)
    listing = summarize(row, runtime.tool_context())
    watch = (await watch_views(runtime, [watch_row]))[0] if watch_row else None
    chart = None
    history_data = history.data if history else None
    if history_data and history_data.listings:
        h = history_data.listings[0]
        series = [Series(label="Listed price", points=[(p.date, p.price) for p in h.daily])]
        if watch is not None:
            cutoff = h.daily[0].date if h.daily else None
            mine = [
                p for p in await effective_daily(runtime, watch.id) if not cutoff or p[0] >= cutoff
            ]
            if mine:
                series.append(Series(label="Best price for you", points=mine))
        chart = price_chart(series, title=f"Observed price of {listing.title[:60]}")
    return page(
        request,
        "listing.html",
        listing=listing,
        sellers=sellers.data if sellers else None,
        offers=offers.data if offers else None,
        history=history_data,
        details=details.data if details else None,
        chart=chart,
        profile=profile,
        days=days,
        watch=watch,
    )


@pages.get("/watchlist")
async def watchlist_page(request: Request, runtime: Runtime = Depends(get_runtime)) -> HTMLResponse:
    async with runtime.sessions() as session:
        rows = await WatchRepository(session).items()
    items = await watch_views(runtime, rows)
    plan = await prep_plan(runtime, now=runtime.clock())
    return page(request, "watchlist.html", nav="watch", items=items, plan=plan)


@pages.get("/alerts")
async def alerts_page(request: Request, runtime: Runtime = Depends(get_runtime)) -> HTMLResponse:
    async with runtime.sessions() as session:
        repo = NotificationRepository(session)
        items = await repo.inbox(limit=60)
        unread = await repo.unread_count()
    brief = await current_brief(runtime, now=runtime.clock())
    return page(
        request,
        "alerts.html",
        nav="alerts",
        items=[NotificationView.of(n, a) for n, a in items],
        unread=unread,
        brief=brief,
    )


@pages.get("/profile")
async def profile_page(request: Request, runtime: Runtime = Depends(get_runtime)) -> HTMLResponse:
    async with runtime.sessions() as session:
        repo = ProfileRepository(session)
        profile = await repo.load()
        preferences = await repo.preferences()
    return page(
        request, "profile.html", profile=profile, preferences=preferences, known=KNOWN_PREFERENCES
    )


@pages.get("/budget")
async def budget_page(
    request: Request,
    budget: str | None = Query(default=None, max_length=30),
    items: str | None = Query(default=None, max_length=5000),
    sort: SortOrder = SortOrder.USE_MOST_BUDGET,
) -> HTMLResponse:
    plan, error = None, None
    if budget and items:
        amount = parse_amount(budget, allow_plain=True)
        try:
            if amount is None or amount <= 0:
                raise ItemParseError(f"'{budget}' is not a budget")
            plan = plan_budget(parse_items(items), amount, sort=sort, limit=15)
        except ItemParseError as exc:
            error = str(exc)
    return page(
        request,
        "budget.html",
        budget=budget or "",
        items=items or "",
        sort=sort,
        plan=plan,
        error=error,
    )
