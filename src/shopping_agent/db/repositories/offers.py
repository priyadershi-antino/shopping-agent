"""Stored offers (provider-structured, extracted, or pasted by the user)."""

import hashlib
import json
from datetime import datetime

from sqlalchemy import and_, or_, select, true, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shopping_agent.db.models import OfferRow
from shopping_agent.domain.evidence import new_id
from shopping_agent.domain.offer import Offer
from shopping_agent.domain.product import Marketplace

_VOLATILE = {"id", "verified_at", "stale", "evidence_ids"}


def offer_fingerprint(offer: Offer, *, listing_id: str | None, variant_id: str | None) -> str:
    terms = offer.model_dump(mode="json", exclude=_VOLATILE)
    payload = json.dumps(
        {"terms": terms, "listing": listing_id, "variant": variant_id}, sort_keys=True
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class OfferRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert(
        self,
        offer: Offer,
        *,
        source: str,
        now: datetime,
        listing_id: str | None = None,
        variant_id: str | None = None,
    ) -> str:
        fingerprint = offer_fingerprint(offer, listing_id=listing_id, variant_id=variant_id)
        stmt = (
            pg_insert(OfferRow)
            .values(
                id=new_id(),
                fingerprint=fingerprint,
                type=offer.type.value,
                platform=offer.platform.value if offer.platform else None,
                provider=offer.provider,
                listing_id=listing_id,
                variant_id=variant_id,
                terms=offer.model_dump(mode="json"),
                verification=offer.verification.value,
                source=source,
                valid_from=offer.valid_from,
                valid_to=offer.valid_to,
                first_seen_at=now,
                last_seen_at=now,
                active=True,
            )
            .on_conflict_do_update(
                index_elements=["fingerprint"],
                set_={
                    "last_seen_at": now,
                    "active": True,
                    "verification": offer.verification.value,
                },
            )
            .returning(OfferRow.id)
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def deactivate_missing(self, *, listing_id: str, source: str, keep: list[str]) -> int:
        """After a full refresh of one listing: offers this source no longer shows are ended."""
        stmt = (
            update(OfferRow)
            .where(
                OfferRow.listing_id == listing_id,
                OfferRow.source == source,
                OfferRow.active.is_(True),
                OfferRow.id.not_in(keep) if keep else true(),
            )
            .values(active=False)
        )
        result = await self.session.execute(stmt)
        return int(getattr(result, "rowcount", 0) or 0)

    async def active_for(
        self,
        *,
        platform: Marketplace,
        now: datetime,
        listing_id: str | None = None,
        variant_id: str | None = None,
    ) -> list[tuple[Offer, OfferRow]]:
        scope = [and_(OfferRow.listing_id.is_(None), OfferRow.variant_id.is_(None))]
        if listing_id:
            scope.append(OfferRow.listing_id == listing_id)
        if variant_id:
            scope.append(OfferRow.variant_id == variant_id)
        stmt = select(OfferRow).where(
            OfferRow.active.is_(True),
            or_(OfferRow.platform.is_(None), OfferRow.platform == platform.value),
            or_(OfferRow.valid_to.is_(None), OfferRow.valid_to >= now),
            or_(*scope),
        )
        out: list[tuple[Offer, OfferRow]] = []
        for row in await self.session.scalars(stmt.order_by(OfferRow.first_seen_at)):
            offer = Offer.model_validate({**row.terms, "id": row.id})
            out.append((offer, row))
        return out
