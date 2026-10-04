"""Presentation of watch items and notifications, shared by the API, CLI and web UI."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from shopping_agent.db.models import AlertEventRow, NotificationRow, WatchItemRow
from shopping_agent.tools.common import ListingSummary


class WatchView(BaseModel):
    id: str
    label: str
    listing: ListingSummary | None
    priority: str
    target_price: Decimal | None
    target_effective: Decimal | None
    target_drop_pct: Decimal | None
    reference_price: Decimal
    last_price: Decimal | None
    last_effective: Decimal | None
    last_availability: str | None
    last_checked_at: datetime | None
    next_check_at: datetime
    check_count: int
    monitor_priority: str
    last_error: str | None
    active: bool
    notify_back_in_stock: bool
    notify_offer_changes: bool
    notify_sale_start: bool
    created_at: datetime

    @classmethod
    def of(cls, row: WatchItemRow, listing: ListingSummary | None) -> "WatchView":
        return cls(
            id=row.id,
            label=row.label,
            listing=listing,
            priority=row.priority,
            target_price=row.target_price,
            target_effective=row.target_effective,
            target_drop_pct=row.target_drop_pct,
            reference_price=row.reference_price,
            last_price=row.last_price,
            last_effective=row.last_effective,
            last_availability=row.last_availability,
            last_checked_at=row.last_checked_at,
            next_check_at=row.next_check_at,
            check_count=row.check_count,
            monitor_priority=row.monitor_priority,
            last_error=row.last_error,
            active=row.active,
            notify_back_in_stock=row.notify_back_in_stock,
            notify_offer_changes=row.notify_offer_changes,
            notify_sale_start=row.notify_sale_start,
            created_at=row.created_at,
        )


class AlertView(BaseModel):
    id: str
    kind: str
    title: str
    message: str
    watch_item_id: str | None
    created_at: datetime

    @classmethod
    def of(cls, row: AlertEventRow) -> "AlertView":
        return cls(
            id=row.id,
            kind=row.kind,
            title=row.title,
            message=row.message,
            watch_item_id=row.watch_item_id,
            created_at=row.created_at,
        )


class NotificationView(BaseModel):
    id: str
    read: bool
    alert: AlertView

    @classmethod
    def of(cls, note: NotificationRow, alert: AlertEventRow) -> "NotificationView":
        return cls(id=note.id, read=note.read_at is not None, alert=AlertView.of(alert))
