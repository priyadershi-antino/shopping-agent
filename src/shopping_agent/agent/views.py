"""Presentation view of a run, shared by the API, CLI and web UI."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from shopping_agent.agent.schemas import FinalAnswer
from shopping_agent.agent.state import AgentState, ProposedAction
from shopping_agent.domain.offer import PriceScenario
from shopping_agent.services.requirement_match import RequirementCheck
from shopping_agent.tools.common import ListingSummary


class HistoryView(BaseModel):
    coverage_days: int
    coverage: str
    minimum: Decimal
    maximum: Decimal
    average: Decimal
    trend: str
    note: str | None = None
    pre_sale_hike: str | None = None


class ItemView(BaseModel):
    listing: ListingSummary
    best_eligible: Decimal
    best_conditional: Decimal | None
    checks: list[RequirementCheck]
    scenarios: list[PriceScenario] = Field(default_factory=list)
    by_payment_method: dict[str, Decimal] = Field(default_factory=dict)
    offers_not_applicable: list[str] = Field(default_factory=list)
    history: HistoryView | None = None
    timing: str | None = None
    timing_advice: str | None = None


class SourceView(BaseModel):
    id: str
    claim: str
    source_name: str
    url: str | None
    verification: str
    retrieved_at: datetime


class RunView(BaseModel):
    run_id: str
    status: str
    request: str
    intent: str | None
    demo_data: bool
    created_at: datetime
    finished_at: datetime | None
    clarification_question: str | None
    error: str | None
    answer: FinalAnswer | None
    items: list[ItemView]
    proposed_actions: list[ProposedAction]
    uncertainties: list[str]
    conflicts: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    sources: list[SourceView] = Field(default_factory=list)
    unverified_figures: int
    tool_calls: int
    llm_calls: int
    input_tokens: int
    output_tokens: int


MAX_SOURCES = 30
MAX_WARNINGS = 10


def _sources(state: AgentState) -> list[SourceView]:
    """Evidence cited by the answer first, then evidence about the shortlisted listings."""
    cited = [
        eid for r in (state.final.recommendations if state.final else []) for eid in r.evidence_ids
    ]
    focus = {f.listing.listing_id for f in state.focus}
    about_focus = [e.id for e in state.evidence.values() if e.subject in focus]
    out: list[SourceView] = []
    for eid in dict.fromkeys([*cited, *about_focus]):
        e = state.evidence.get(eid)
        if e is None:
            continue
        out.append(
            SourceView(
                id=e.id,
                claim=e.claim,
                source_name=e.source_name,
                url=e.url,
                verification=e.verification.value,
                retrieved_at=e.retrieved_at,
            )
        )
        if len(out) >= MAX_SOURCES:
            break
    return out


def run_view(state: AgentState) -> RunView:
    items: list[ItemView] = []
    for focus in state.focus:
        lid = focus.listing.listing_id
        offers = state.offers.get(lid)
        history = state.price_history.get(lid)
        hv = None
        if history and history.baseline:
            b = history.baseline
            hv = HistoryView(
                coverage_days=b.coverage_days,
                coverage=b.tier.value,
                minimum=b.minimum,
                maximum=b.maximum,
                average=b.average,
                trend=b.trend.value,
                note=history.low_note,
                pre_sale_hike=history.pre_sale_hike.reason
                if history.pre_sale_hike and history.pre_sale_hike.detected
                else None,
            )
        items.append(
            ItemView(
                listing=focus.listing,
                best_eligible=focus.best_eligible,
                best_conditional=focus.best_conditional,
                checks=focus.checks,
                scenarios=offers.scenarios if offers else [],
                by_payment_method={r.payment: r.best.net_after_cashback for r in offers.card_matrix}
                if offers
                else {},
                offers_not_applicable=[
                    f"{e.title}: {'; '.join(e.reasons)}" for e in offers.excluded
                ]
                if offers
                else [],
                history=hv,
                timing=focus.timing.summary if focus.timing else None,
                timing_advice=focus.timing.advice.value if focus.timing else None,
            )
        )
    total = state.usage.total
    uncertainties = list(
        dict.fromkeys([*(state.final.uncertainties if state.final else []), *state.uncertainties])
    )
    return RunView(
        conflicts=[c.explanation for c in state.conflicts],
        warnings=list(dict.fromkeys(state.warnings))[:MAX_WARNINGS],
        sources=_sources(state),
        run_id=state.run_id,
        status=state.status.value,
        request=state.request,
        intent=state.intent.value if state.intent else None,
        demo_data=state.demo_data,
        created_at=state.created_at,
        finished_at=state.finished_at,
        clarification_question=state.clarification_question,
        error=state.error,
        answer=state.final,
        items=items,
        proposed_actions=state.proposed_actions,
        uncertainties=uncertainties,
        unverified_figures=len(state.grounding.ungrounded) if state.grounding else 0,
        tool_calls=len(state.tool_calls),
        llm_calls=len(state.usage.calls),
        input_tokens=total.input_tokens,
        output_tokens=total.output_tokens,
    )


def conversation_context(state: AgentState) -> str:
    lines = [f"User asked: {state.request}"]
    if state.final:
        lines.append(f"Assistant answered: {state.final.summary}")
        for r in state.final.recommendations[:3]:
            lines.append(
                f"- recommended {r.product} (listing_id {r.listing_id}): {r.price_summary}"
            )
    elif state.clarification_question:
        lines.append(f"Assistant asked: {state.clarification_question}")
    return "\n".join(lines)


def follow_up(previous: AgentState) -> tuple[str, list[str]]:
    """Context and products to carry into a follow-up question about an earlier answer."""
    ids = [r.listing_id for r in previous.final.recommendations] if previous.final else []
    ids += [f.listing.listing_id for f in previous.focus[:3]]
    return conversation_context(previous), list(dict.fromkeys(ids))[:5]
