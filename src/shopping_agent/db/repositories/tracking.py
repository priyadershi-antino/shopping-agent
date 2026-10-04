"""Watchlist, effective-price snapshots, alert events and notifications."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shopping_agent.db.models import (
    AlertEventRow,
    EffectivePriceSnapshotRow,
    NotificationRow,
    WatchItemRow,
)
from shopping_agent.db.repositories.profile import LOCAL_USER
from shopping_agent.domain.evidence import new_id

WATCH_FIELDS = {
    "label",
    "priority",
    "target_price",
    "target_effective",
    "target_drop_pct",
    "notify_back_in_stock",
    "notify_offer_changes",
    "notify_sale_start",
    "active",
}


class WatchRepository:
    def __init__(self, session: AsyncSession, user_id: str = LOCAL_USER) -> None:
        self.session = session
        self.user_id = user_id

    async def upsert(
        self, *, listing_id: str, label: str, reference_price: Decimal, now: datetime, **fields: Any
    ) -> WatchItemRow:
        """Track a listing, or update the targets of an existing watch on it."""
        unknown = set(fields) - WATCH_FIELDS
        if unknown:
            raise ValueError(f"unknown watch fields: {sorted(unknown)}")
        existing = await self.session.scalar(
            select(WatchItemRow).where(
                WatchItemRow.user_id == self.user_id, WatchItemRow.listing_id == listing_id
            )
        )
        if existing is not None:
            for key, value in fields.items():
                if value is not None:
                    setattr(existing, key, value)
            existing.active = True
            existing.next_check_at = now
            await self.session.flush()
            return existing
        row = WatchItemRow(
            id=new_id(),
            user_id=self.user_id,
            listing_id=listing_id,
            label=label,
            reference_price=reference_price,
            created_at=now,
            next_check_at=now,
            last_offers={},
            **{k: v for k, v in fields.items() if v is not None},
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def get(self, watch_id: str) -> WatchItemRow | None:
        row = await self.session.get(WatchItemRow, watch_id)
        return row if row is not None and row.user_id == self.user_id else None

    async def for_listing(self, listing_id: str) -> WatchItemRow | None:
        return await self.session.scalar(
            select(WatchItemRow).where(
                WatchItemRow.user_id == self.user_id, WatchItemRow.listing_id == listing_id
            )
        )

    async def items(self, *, active_only: bool = False) -> list[WatchItemRow]:
        stmt = select(WatchItemRow).where(WatchItemRow.user_id == self.user_id)
        if active_only:
            stmt = stmt.where(WatchItemRow.active.is_(True))
        return list(await self.session.scalars(stmt.order_by(WatchItemRow.created_at)))

    async def update(self, watch_id: str, **fields: Any) -> WatchItemRow | None:
        unknown = set(fields) - WATCH_FIELDS
        if unknown:
            raise ValueError(f"unknown watch fields: {sorted(unknown)}")
        row = await self.get(watch_id)
        if row is None:
            return None
        for key, value in fields.items():
            setattr(row, key, value)
        await self.session.flush()
        return row

    async def delete(self, watch_id: str) -> bool:
        result = await self.session.execute(
            delete(WatchItemRow).where(
                WatchItemRow.id == watch_id, WatchItemRow.user_id == self.user_id
            )
        )
        return bool(getattr(result, "rowcount", 0))

    async def due(self, now: datetime, limit: int) -> list[WatchItemRow]:
        """Active items whose next check is due, most overdue first (all users)."""
        stmt = (
            select(WatchItemRow)
            .where(WatchItemRow.active.is_(True), WatchItemRow.next_check_at <= now)
            .order_by(WatchItemRow.next_check_at)
            .limit(limit)
        )
        return list(await self.session.scalars(stmt))

    async def active_priorities(self) -> dict[str, str]:
        rows = await self.session.execute(
            select(WatchItemRow.id, WatchItemRow.monitor_priority).where(
                WatchItemRow.active.is_(True)
            )
        )
        return {wid: prio for wid, prio in rows}

    async def add_snapshot(
        self,
        watch_id: str,
        *,
        listing_price: Decimal,
        best_effective: Decimal,
        scenario: dict[str, Any],
        observed_at: datetime,
    ) -> None:
        self.session.add(
            EffectivePriceSnapshotRow(
                watch_item_id=watch_id,
                listing_price=listing_price,
                best_effective=best_effective,
                scenario=scenario,
                observed_at=observed_at,
            )
        )
        await self.session.flush()

    async def snapshots(
        self, watch_id: str, *, since: datetime | None = None
    ) -> list[EffectivePriceSnapshotRow]:
        stmt = select(EffectivePriceSnapshotRow).where(
            EffectivePriceSnapshotRow.watch_item_id == watch_id
        )
        if since is not None:
            stmt = stmt.where(EffectivePriceSnapshotRow.observed_at >= since)
        return list(
            await self.session.scalars(stmt.order_by(EffectivePriceSnapshotRow.observed_at))
        )


class AlertRepository:
    def __init__(self, session: AsyncSession, user_id: str = LOCAL_USER) -> None:
        self.session = session
        self.user_id = user_id

    async def add(
        self,
        *,
        kind: str,
        dedup_key: str,
        title: str,
        message: str,
        now: datetime,
        watch_item_id: str | None = None,
        data: dict[str, Any] | None = None,
        channels: list[str] | None = None,
    ) -> AlertEventRow | None:
        """Record an alert and queue its notifications. Returns None if it already exists."""
        alert_id = new_id()
        inserted = await self.session.scalar(
            pg_insert(AlertEventRow)
            .values(
                id=alert_id,
                user_id=self.user_id,
                watch_item_id=watch_item_id,
                kind=kind,
                dedup_key=dedup_key,
                title=title[:300],
                message=message,
                data=data or {},
                created_at=now,
            )
            .on_conflict_do_nothing(index_elements=["dedup_key"])
            .returning(AlertEventRow.id)
        )
        if inserted is None:
            return None
        for channel in dict.fromkeys(["in_app", *(channels or [])]):
            self.session.add(
                NotificationRow(
                    id=new_id(),
                    user_id=self.user_id,
                    alert_event_id=alert_id,
                    channel=channel,
                    status="sent" if channel == "in_app" else "pending",
                    created_at=now,
                    sent_at=now if channel == "in_app" else None,
                )
            )
        await self.session.flush()
        return await self.session.get(AlertEventRow, alert_id)

    async def last_of_kind(self, watch_item_id: str, kind: str) -> AlertEventRow | None:
        return await self.session.scalar(
            select(AlertEventRow)
            .where(AlertEventRow.watch_item_id == watch_item_id, AlertEventRow.kind == kind)
            .order_by(AlertEventRow.created_at.desc())
            .limit(1)
        )

    async def recent(self, *, limit: int = 50) -> list[AlertEventRow]:
        stmt = (
            select(AlertEventRow)
            .where(AlertEventRow.user_id == self.user_id)
            .order_by(AlertEventRow.created_at.desc())
            .limit(limit)
        )
        return list(await self.session.scalars(stmt))


class NotificationRepository:
    def __init__(self, session: AsyncSession, user_id: str = LOCAL_USER) -> None:
        self.session = session
        self.user_id = user_id

    async def inbox(
        self, *, unread_only: bool = False, limit: int = 50
    ) -> list[tuple[NotificationRow, AlertEventRow]]:
        stmt = (
            select(NotificationRow, AlertEventRow)
            .join(AlertEventRow, AlertEventRow.id == NotificationRow.alert_event_id)
            .where(NotificationRow.user_id == self.user_id, NotificationRow.channel == "in_app")
        )
        if unread_only:
            stmt = stmt.where(NotificationRow.read_at.is_(None))
        rows = await self.session.execute(
            stmt.order_by(NotificationRow.created_at.desc()).limit(limit)
        )
        return [(n, a) for n, a in rows]

    async def unread_count(self) -> int:
        count = await self.session.scalar(
            select(func.count())
            .select_from(NotificationRow)
            .where(
                NotificationRow.user_id == self.user_id,
                NotificationRow.channel == "in_app",
                NotificationRow.read_at.is_(None),
            )
        )
        return int(count or 0)

    async def mark_read(self, now: datetime, notification_id: str | None = None) -> int:
        stmt = update(NotificationRow).where(
            NotificationRow.user_id == self.user_id,
            NotificationRow.channel == "in_app",
            NotificationRow.read_at.is_(None),
        )
        if notification_id is not None:
            stmt = stmt.where(NotificationRow.id == notification_id)
        result = await self.session.execute(stmt.values(read_at=now))
        return int(getattr(result, "rowcount", 0) or 0)

    async def pending(self, *, limit: int = 50) -> list[tuple[NotificationRow, AlertEventRow]]:
        """Undelivered external notifications (all users)."""
        rows = await self.session.execute(
            select(NotificationRow, AlertEventRow)
            .join(AlertEventRow, AlertEventRow.id == NotificationRow.alert_event_id)
            .where(NotificationRow.status == "pending")
            .order_by(NotificationRow.created_at)
            .limit(limit)
        )
        return [(n, a) for n, a in rows]
