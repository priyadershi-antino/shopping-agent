from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shopping_agent.db.models import SaleEventRow
from shopping_agent.domain.common import Verification
from shopping_agent.domain.evidence import new_id
from shopping_agent.domain.product import Marketplace
from shopping_agent.domain.sale import SaleEvent, SalePhase


def _to_domain(row: SaleEventRow) -> SaleEvent:
    return SaleEvent(
        id=row.id,
        name=row.name,
        platform=Marketplace(row.platform),
        start=row.start_at,
        end=row.end_at,
        phases=[SalePhase.model_validate(p) for p in row.phases or []],
        partner_banks=list(row.partner_banks or []),
        categories=list(row.categories or []),
        official_sources=list(row.official_sources or []),
        verification=Verification(row.verification),
        last_verified_at=row.last_verified_at,
    )


class SaleEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert(self, event: SaleEvent) -> str:
        values = {
            "name": event.name,
            "platform": event.platform.value,
            "start_at": event.start,
            "end_at": event.end,
            "phases": [p.model_dump(mode="json") for p in event.phases],
            "partner_banks": event.partner_banks,
            "categories": event.categories,
            "official_sources": event.official_sources,
            "verification": event.verification.value,
            "last_verified_at": event.last_verified_at,
        }
        excluded = pg_insert(SaleEventRow).excluded
        stmt = (
            pg_insert(SaleEventRow)
            .values(id=new_id(), **values)
            .on_conflict_do_update(
                index_elements=["platform", "name", "start_at"],
                set_={
                    k: getattr(excluded, k)
                    for k in values
                    if k not in {"platform", "name", "start_at"}
                },
            )
            .returning(SaleEventRow.id)
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def list(
        self, *, platform: Marketplace | None = None, ending_after: datetime | None = None
    ) -> list[SaleEvent]:
        stmt = select(SaleEventRow)
        if platform is not None:
            stmt = stmt.where(SaleEventRow.platform == platform.value)
        if ending_after is not None:
            stmt = stmt.where(SaleEventRow.end_at >= ending_after)
        return [
            _to_domain(r) for r in await self.session.scalars(stmt.order_by(SaleEventRow.start_at))
        ]
