"""Freshness policy: how long each kind of data may be cached or shown without a 'stale' label."""

from datetime import datetime, timedelta
from enum import StrEnum


class DataKind(StrEnum):
    PRICE = "price"
    OFFER = "offer"
    SALE_EVENT = "sale_event"
    SEARCH = "search"
    REVIEW = "review"
    SPEC = "spec"
    PAGE = "page"


_MIN = 60
_HOUR = 60 * _MIN
_DAY = 24 * _HOUR

# (normal, during an active sale) in seconds
TTL_SECONDS: dict[DataKind, tuple[int, int]] = {
    DataKind.PRICE: (30 * _MIN, 10 * _MIN),
    DataKind.OFFER: (_HOUR, 30 * _MIN),
    DataKind.SALE_EVENT: (12 * _HOUR, _HOUR),
    DataKind.SEARCH: (6 * _HOUR, _HOUR),
    DataKind.REVIEW: (3 * _DAY, 3 * _DAY),
    DataKind.SPEC: (7 * _DAY, 7 * _DAY),
    DataKind.PAGE: (_DAY, 6 * _HOUR),
}


def ttl_seconds(kind: DataKind, *, sale_active: bool = False) -> int:
    normal, sale = TTL_SECONDS[kind]
    return sale if sale_active else normal


def is_stale(
    kind: DataKind, observed_at: datetime, now: datetime, *, sale_active: bool = False
) -> bool:
    return now - observed_at > timedelta(seconds=ttl_seconds(kind, sale_active=sale_active))
