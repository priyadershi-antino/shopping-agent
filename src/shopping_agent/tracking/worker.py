"""The background worker: a plain asyncio loop (no scheduler library needed).

Each tick: announce sales (once per event and phase), check the watched items that are due
(most overdue first, so quota limits fall on the least urgent), then deliver notifications.
A per-tick counter in the cache keeps two workers from doing the same tick.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime

from pydantic import BaseModel, Field

from shopping_agent.db.repositories.tracking import WatchRepository
from shopping_agent.runtime import Runtime
from shopping_agent.tracking.monitor import (
    RETRY_AFTER_ERROR,
    CheckOutcome,
    check_watch,
    record_error,
)
from shopping_agent.tracking.notify import deliver_pending
from shopping_agent.tracking.sales import check_sales

log = logging.getLogger(__name__)

MAX_CHECKS_PER_TICK = 20


class TickReport(BaseModel):
    at: datetime
    sale_alerts: list[str] = Field(default_factory=list)
    checked: int = 0
    failed: int = 0
    alerts: list[str] = Field(default_factory=list)
    delivered: int = 0


async def run_once(rt: Runtime, *, now: datetime | None = None) -> TickReport:
    now = now or rt.clock()
    report = TickReport(at=now)
    report.sale_alerts = [a.title for a in await check_sales(rt, now=now)]
    async with rt.sessions() as session:
        due = await WatchRepository(session).due(now, MAX_CHECKS_PER_TICK)
    outcomes: list[CheckOutcome] = []
    for watch in due:  # sequential: provider quotas and rate limits are per process
        try:
            outcomes.append(await check_watch(rt, watch.id, now=now))
        except Exception:  # one broken item must not stop the others or stay due forever
            log.exception("watch check crashed", extra={"watch_id": watch.id})
            outcomes.append(
                await record_error(
                    rt, watch.id, "internal error during the check", now + RETRY_AFTER_ERROR, now
                )
            )
    report.checked = sum(1 for o in outcomes if o.ok)
    report.failed = sum(1 for o in outcomes if not o.ok)
    report.alerts = [a for o in outcomes for a in o.alerts]
    report.delivered = await deliver_pending(rt, now=now)
    return report


async def run_worker(
    rt: Runtime,
    *,
    once: bool = False,
    stop: asyncio.Event | None = None,
    on_tick: Callable[[TickReport], Awaitable[None]] | None = None,
) -> list[TickReport]:
    tick_s = rt.settings.worker_tick_seconds
    reports: list[TickReport] = []
    while True:
        tick = int(time.time() // tick_s)
        if once or await rt.cache.incr(f"worker:tick:{tick}", tick_s * 2) == 1:
            try:
                report = await run_once(rt)
                reports = [*reports[-49:], report]
                log.info("worker_tick", extra=report.model_dump(mode="json"))
                if on_tick is not None:
                    await on_tick(report)
            except Exception:  # keep the worker alive; the next tick retries
                log.exception("worker tick failed")
        if once:
            return reports
        try:
            await asyncio.wait_for((stop or asyncio.Event()).wait(), timeout=tick_s)
            return reports
        except TimeoutError:
            continue
