"""Seed the database with the FICTIONAL demo catalog, a synthetic price history and a demo
sale event, so price history, charts and sale features can be tried without API keys.

Everything written here is labelled as demo data: observations use source "demo-seed" and the
sale event is named "DEMO … (fictional)" with `unverified` status.
"""

import random
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shopping_agent.db.models import PriceObservationRow, SaleEventRow
from shopping_agent.db.repositories import SaleEventRepository
from shopping_agent.domain.common import Verification
from shopping_agent.domain.product import Marketplace
from shopping_agent.domain.sale import PhaseKind, SaleEvent, SalePhase
from shopping_agent.providers.fixtures import FixtureProductProvider
from shopping_agent.providers.registry import ProviderRegistry
from shopping_agent.tools.base import ToolContext
from shopping_agent.tools.common import ingest

DEMO_HISTORY_SOURCE = "demo-seed"
DEMO_SALE_NAME = "DEMO Festive Sale (fictional)"
HIKE_SHARE = 0.35  # share of listings given a pre-sale price rise, to demonstrate detection


@dataclass(frozen=True)
class SeedReport:
    listings: int
    observations: int
    sale_events: int
    sale_start: datetime


def _retail_round(value: Decimal) -> Decimal:
    """Round to a typical Indian e-commerce price ending in 90/99 (e.g. ₹64,990)."""
    step = Decimal(500) if value >= 10_000 else Decimal(50)
    return max((value / step).quantize(Decimal(1)) * step - 10, Decimal(99))


def synthetic_history(
    seed: str, current: Decimal, *, days: int, now: datetime, hike: bool
) -> list[tuple[datetime, Decimal]]:
    """Deterministic daily prices ending at `current`, newest first. Price changes come in
    steps (as real listings do); with `hike`, the last 5 days sit ~7% above the weeks before."""
    rng = random.Random(zlib.crc32(seed.encode()))  # noqa: S311 - not security sensitive
    points = [(now, current)]
    price = current
    for back in range(1, days):
        day = (now - timedelta(days=back)).replace(hour=6, minute=30, second=0, microsecond=0)
        if hike and back == 5:
            price = _retail_round(current / Decimal("1.07"))
        elif (not hike or back > 5) and rng.random() < 0.15:
            change = Decimal(str(round(rng.uniform(0.01, 0.05), 3)))
            sign = 1 if rng.random() < 0.55 else -1  # slightly more often higher in the past
            price = _retail_round(price * (1 + sign * change))
            price = min(max(price, current * Decimal("0.75")), current * Decimal("1.35"))
            price = _retail_round(price)
        points.append((day, price))
    return points


async def clear_demo(sessions: async_sessionmaker[AsyncSession]) -> tuple[int, int]:
    """Remove the synthetic price history and the fictional sale events (the demo listings stay).
    Returns (observations, sale events) deleted. Use it before switching to real data."""
    async with sessions() as session, session.begin():
        obs = await session.execute(
            delete(PriceObservationRow).where(PriceObservationRow.source == DEMO_HISTORY_SOURCE)
        )
        events = await session.execute(
            delete(SaleEventRow).where(SaleEventRow.name == DEMO_SALE_NAME)
        )
    return int(obs.rowcount or 0), int(events.rowcount or 0)  # type: ignore[attr-defined]


async def seed_demo(
    providers: ProviderRegistry,
    sessions: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
    days: int = 60,
    sale_in_days: int = 3,
) -> SeedReport:
    provider = FixtureProductProvider(clock=lambda: now)
    ctx = ToolContext(providers=providers, sessions=sessions, clock=lambda: now)
    observations = 0
    async with sessions() as session, session.begin():
        rows, _ = await ingest(ctx, session, provider.all_listings())
        await session.execute(
            delete(PriceObservationRow).where(
                PriceObservationRow.listing_id.in_([r.id for r in rows]),
                PriceObservationRow.source == DEMO_HISTORY_SOURCE,
            )
        )
        for row in rows:
            rng = random.Random(zlib.crc32(f"hike:{row.external_id}".encode()))  # noqa: S311
            history = synthetic_history(
                row.external_id or row.id,
                row.current_price,
                days=days,
                now=now,
                hike=rng.random() < HIKE_SHARE,
            )
            for observed_at, price in history[1:]:  # the newest point was recorded by ingest
                session.add(
                    PriceObservationRow(
                        listing_id=row.id,
                        price=price,
                        mrp=row.mrp,
                        currency="INR",
                        availability="in_stock",
                        source=DEMO_HISTORY_SOURCE,
                        observed_at=observed_at,
                    )
                )
                observations += 1

        start = (now + timedelta(days=sale_in_days)).replace(
            hour=18, minute=30, second=0, microsecond=0
        )  # midnight IST
        repo = SaleEventRepository(session)
        for platform, member in (
            (Marketplace.AMAZON, "amazon prime"),
            (Marketplace.FLIPKART, "flipkart plus"),
        ):
            await repo.upsert(
                SaleEvent(
                    name=DEMO_SALE_NAME,
                    platform=platform,
                    start=start,
                    end=start + timedelta(days=6),
                    phases=[
                        SalePhase(
                            name="Early access (demo)",
                            kind=PhaseKind.EARLY_ACCESS,
                            start=start - timedelta(days=1),
                            end=start,
                            eligibility=member,
                        ),
                        SalePhase(
                            name="Main sale (demo)",
                            kind=PhaseKind.MAIN,
                            start=start,
                            end=start + timedelta(days=6),
                        ),
                    ],
                    partner_banks=["HDFC", "ICICI"],
                    categories=["electronics"],
                    official_sources=[],
                    verification=Verification.UNVERIFIED,
                    last_verified_at=now,
                )
            )
    return SeedReport(
        listings=len(rows), observations=observations, sale_events=2, sale_start=start
    )
