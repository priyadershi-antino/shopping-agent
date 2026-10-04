"""Sale Mode: Normal → Pre-sale → Early access / Active → Post-sale, from verified events."""

from datetime import datetime, timedelta

from pydantic import BaseModel

from shopping_agent.domain.common import Verification
from shopping_agent.domain.product import Marketplace
from shopping_agent.domain.sale import PhaseKind, SaleEvent, SalePhase, SaleState

_RANK = {
    SaleState.ACTIVE: 4,
    SaleState.EARLY_ACCESS: 3,
    SaleState.PRE_SALE: 2,
    SaleState.POST_SALE: 1,
    SaleState.NORMAL: 0,
}


class SaleStatus(BaseModel):
    state: SaleState
    event: SaleEvent | None = None
    phase: SalePhase | None = None
    verified: bool = False  # event dates come from a verified source

    @property
    def is_sale_window(self) -> bool:
        return self.state in {SaleState.ACTIVE, SaleState.EARLY_ACCESS}


def _state_for(
    event: SaleEvent, now: datetime, pre_sale: timedelta, post_sale: timedelta
) -> tuple[SaleState, SalePhase | None]:
    for phase in event.phases:
        if phase.start <= now < phase.end:
            if phase.kind is PhaseKind.EARLY_ACCESS:
                return SaleState.EARLY_ACCESS, phase
            return SaleState.ACTIVE, phase
    if event.start <= now < event.end:
        return SaleState.ACTIVE, None
    first_start = min([event.start, *(p.start for p in event.phases)])
    if first_start - pre_sale <= now < first_start:
        return SaleState.PRE_SALE, None
    if event.end <= now < event.end + post_sale:
        return SaleState.POST_SALE, None
    return SaleState.NORMAL, None


def sale_status(
    events: list[SaleEvent],
    now: datetime,
    *,
    platform: Marketplace | None = None,
    pre_sale_days: int = 14,
    post_sale_days: int = 3,
) -> SaleStatus:
    best = SaleStatus(state=SaleState.NORMAL)
    for event in events:
        if platform is not None and event.platform is not platform:
            continue
        state, phase = _state_for(
            event, now, timedelta(days=pre_sale_days), timedelta(days=post_sale_days)
        )
        if state is SaleState.NORMAL:
            continue
        candidate = SaleStatus(
            state=state,
            event=event,
            phase=phase,
            verified=event.verification is Verification.VERIFIED,
        )
        if _RANK[state] > _RANK[best.state] or (
            _RANK[state] == _RANK[best.state]
            and best.event is not None
            and event.start < best.event.start
        ):
            best = candidate
    return best
