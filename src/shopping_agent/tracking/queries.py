"""Read helpers shared by the API, CLI and web UI."""

from datetime import date
from decimal import Decimal

from shopping_agent.db.models import WatchItemRow
from shopping_agent.db.repositories import CatalogRepository
from shopping_agent.db.repositories.tracking import WatchRepository
from shopping_agent.runtime import Runtime
from shopping_agent.services.price_baseline import IST
from shopping_agent.tools.common import summarize
from shopping_agent.tracking.views import WatchView


async def watch_views(rt: Runtime, rows: list[WatchItemRow]) -> list[WatchView]:
    ctx = rt.tool_context()
    out: list[WatchView] = []
    async with rt.sessions() as session:
        catalog = CatalogRepository(session)
        for row in rows:
            listing = await catalog.get_listing(row.listing_id)
            out.append(WatchView.of(row, summarize(listing, ctx) if listing else None))
    return out


async def effective_daily(rt: Runtime, watch_id: str) -> list[tuple[date, Decimal]]:
    """Best price for the user's cards, last check of each IST day."""
    async with rt.sessions() as session:
        snaps = await WatchRepository(session).snapshots(watch_id)
    daily: dict[date, Decimal] = {}
    for s in snaps:
        daily[s.observed_at.astimezone(IST).date()] = s.best_effective
    return sorted(daily.items())
