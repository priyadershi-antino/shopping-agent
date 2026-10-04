"""Price observations (the basis of price history)."""

from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shopping_agent.db.models import PriceObservationRow
from shopping_agent.domain.price import PriceObservation
from shopping_agent.domain.product import Availability


def _to_domain(row: PriceObservationRow) -> PriceObservation:
    return PriceObservation(
        listing_id=row.listing_id,
        price=row.price,
        mrp=row.mrp,
        currency=row.currency,
        availability=Availability(row.availability),
        source=row.source,
        observed_at=row.observed_at,
    )


class PriceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def latest(self, listing_id: str) -> PriceObservationRow | None:
        stmt = (
            select(PriceObservationRow)
            .where(PriceObservationRow.listing_id == listing_id)
            .order_by(PriceObservationRow.observed_at.desc())
            .limit(1)
        )
        return (await self.session.scalars(stmt)).first()

    async def record(
        self,
        listing_id: str,
        *,
        price: Decimal,
        mrp: Decimal | None,
        availability: Availability,
        source: str,
        observed_at: datetime,
        min_gap: timedelta = timedelta(minutes=30),
    ) -> bool:
        """Store an observation. Identical repeats within `min_gap` are skipped (cache replays)."""
        last = await self.latest(listing_id)
        if (
            last is not None
            and last.price == price
            and last.availability == availability.value
            and abs(observed_at - last.observed_at) < min_gap
        ):
            return False
        self.session.add(
            PriceObservationRow(
                listing_id=listing_id,
                price=price,
                mrp=mrp,
                currency="INR",
                availability=availability.value,
                source=source,
                observed_at=observed_at,
            )
        )
        await self.session.flush()
        return True

    async def history(
        self, listing_ids: list[str], *, since: datetime | None = None
    ) -> list[PriceObservation]:
        if not listing_ids:
            return []
        stmt = select(PriceObservationRow).where(PriceObservationRow.listing_id.in_(listing_ids))
        if since is not None:
            stmt = stmt.where(PriceObservationRow.observed_at >= since)
        rows = await self.session.scalars(stmt.order_by(PriceObservationRow.observed_at))
        return [_to_domain(r) for r in rows]
