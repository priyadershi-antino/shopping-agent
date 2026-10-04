"""Explicit, structured agent state. Only data and observations — no hidden reasoning."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from shopping_agent.agent.schemas import FinalAnswer, Intent, RequestAnalysis, ResearchPlan
from shopping_agent.domain.evidence import Evidence, new_id
from shopping_agent.domain.offer import UserProfile
from shopping_agent.domain.requirements import Requirements
from shopping_agent.llm.client import RunUsage
from shopping_agent.services.conflicts import Conflict
from shopping_agent.services.grounding import GroundingReport
from shopping_agent.services.requirement_match import RequirementCheck
from shopping_agent.services.timing import TimingAnalysis
from shopping_agent.tools.catalog_tools import (
    FindAlternativesOutput,
    GetListingsOutput,
    ProductDetailsOutput,
)
from shopping_agent.tools.common import Candidate, ListingSummary
from shopping_agent.tools.deal_tools import FindOffersOutput, ListingHistory, SaleEventsOutput
from shopping_agent.tools.registry import ToolCallRecord
from shopping_agent.tools.web_tools import ResearchReviewsOutput, WebItem


class RunStatus(StrEnum):
    RUNNING = "running"
    NEEDS_CLARIFICATION = "needs_clarification"
    COMPLETED = "completed"
    FAILED = "failed"


class ProposedAction(BaseModel):
    """An action the agent suggests; nothing is saved until the user confirms it."""

    id: str = Field(default_factory=new_id)
    action: Literal["track_price", "price_alert", "save_preference"]
    listing_id: str | None = None
    target_price: Decimal | None = None
    description: str
    status: Literal["pending", "confirmed", "dismissed"] = "pending"


class FocusItem(BaseModel):
    """A shortlisted listing with its deterministic analysis."""

    listing: ListingSummary
    candidate_key: str | None = None
    checks: list[RequirementCheck] = Field(default_factory=list)
    best_eligible: Decimal
    best_conditional: Decimal | None = None
    timing: TimingAnalysis | None = None
    price_suspect: bool = False


class AgentState(BaseModel):
    run_id: str = Field(default_factory=new_id)
    status: RunStatus = RunStatus.RUNNING
    created_at: datetime
    finished_at: datetime | None = None
    request: str
    demo_data: bool = False
    prompt_versions: dict[str, int] = Field(default_factory=dict)

    analysis: RequestAnalysis | None = None
    intent: Intent | None = None
    requirements: Requirements | None = None
    profile: UserProfile = Field(default_factory=UserProfile)
    links: list[str] = Field(default_factory=list)
    linked_listing_ids: list[str] = Field(default_factory=list)
    link_results: list[dict[str, str]] = Field(default_factory=list)  # status per link
    platforms: list[str] = Field(default_factory=list)  # platforms searched (empty = all)
    context: str | None = None  # earlier conversation, for follow-ups
    carried_listing_ids: list[str] = Field(default_factory=list)  # products from that answer
    plan: ResearchPlan | None = None
    clarification_question: str | None = None

    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    research_iterations: int = 0
    candidates: dict[str, Candidate] = Field(default_factory=dict)
    listings: dict[str, ListingSummary] = Field(default_factory=dict)
    details: dict[str, ProductDetailsOutput] = Field(default_factory=dict)
    seller_comparisons: dict[str, GetListingsOutput] = Field(default_factory=dict)
    offers: dict[str, FindOffersOutput] = Field(default_factory=dict)
    price_history: dict[str, ListingHistory] = Field(default_factory=dict)
    reviews: dict[str, ResearchReviewsOutput] = Field(default_factory=dict)
    alternatives: list[FindAlternativesOutput] = Field(default_factory=list)
    sale_events: SaleEventsOutput | None = None
    web_results: list[WebItem] = Field(default_factory=list)
    evidence: dict[str, Evidence] = Field(default_factory=dict)
    conflicts: list[Conflict] = Field(default_factory=list)

    examined_listing_ids: list[str] = Field(default_factory=list)  # looked at closely in research
    focus: list[FocusItem] = Field(default_factory=list)
    final: FinalAnswer | None = None
    grounding: GroundingReport | None = None
    proposed_actions: list[ProposedAction] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
    usage: RunUsage = Field(default_factory=RunUsage)

    def summary(self) -> dict[str, Any]:
        total = self.usage.total
        return {
            "run_id": self.run_id,
            "status": self.status.value,
            "intent": self.intent.value if self.intent else None,
            "tool_calls": len(self.tool_calls),
            "llm_calls": len(self.usage.calls),
            "input_tokens": total.input_tokens,
            "output_tokens": total.output_tokens,
            "evidence": len(self.evidence),
        }
