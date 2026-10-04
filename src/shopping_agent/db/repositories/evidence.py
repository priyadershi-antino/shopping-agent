from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shopping_agent.db.models import EvidenceRow
from shopping_agent.domain.common import Confidence, SourceType, Verification
from shopping_agent.domain.evidence import Evidence


class EvidenceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add_many(self, items: list[Evidence], *, run_id: str | None = None) -> None:
        if not items:
            return
        rows = [
            {
                **item.model_dump(mode="json", exclude={"retrieved_at"}),
                "retrieved_at": item.retrieved_at,
                "run_id": run_id,
            }
            for item in items
        ]
        await self.session.execute(pg_insert(EvidenceRow).values(rows).on_conflict_do_nothing())

    async def for_subject(self, subject: str) -> list[Evidence]:
        stmt = select(EvidenceRow).where(EvidenceRow.subject == subject)
        return [
            Evidence(
                id=r.id,
                claim=r.claim,
                subject=r.subject,
                attribute=r.attribute,
                value=r.value,
                source_type=SourceType(r.source_type),
                source_name=r.source_name,
                url=r.url,
                retrieved_at=r.retrieved_at,
                verification=Verification(r.verification),
                confidence=Confidence(r.confidence),
            )
            for r in await self.session.scalars(stmt.order_by(EvidenceRow.retrieved_at))
        ]
