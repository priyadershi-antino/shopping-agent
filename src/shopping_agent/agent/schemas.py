"""Structured outputs the LLM produces. Kept simple (floats, plain strings) for strict schemas;
code converts them to domain types afterwards."""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from shopping_agent.domain.offer import CardType, EmiRule, OfferType
from shopping_agent.domain.requirements import ConstraintOp
from shopping_agent.domain.review import ThemeKind


class Intent(StrEnum):
    DISCOVER = "discover"
    COMPARE = "compare"
    ALTERNATIVES = "alternatives"
    LINK_LOOKUP = "link_lookup"
    DEAL_CHECK = "deal_check"
    PRICE_HISTORY = "price_history"
    TRACK = "track"
    BUDGET_PLAN = "budget_plan"
    SALE_PREP = "sale_prep"
    OFFER_CHECK = "offer_check"
    GENERAL = "general"


class LLMConstraint(BaseModel):
    attribute: str
    op: ConstraintOp
    value: str | float | bool | list[str]
    note: str | None


class LLMBudget(BaseModel):
    minimum: float | None
    maximum: float | None
    target: float | None
    approximate: bool


class LLMPaymentMethod(BaseModel):
    issuer: str
    card_type: CardType


class RequestAnalysis(BaseModel):
    intent: Intent
    category: str | None = Field(description="laptop, smartphone, tablet, monitor, headphones, …")
    budget: LLMBudget
    use_cases: list[str]
    hard_constraints: list[LLMConstraint]
    strong_preferences: list[LLMConstraint]
    nice_to_have: list[LLMConstraint]
    unknowns: list[str]
    product_mentions: list[str]
    target_price: float | None
    payment_methods: list[LLMPaymentMethod]
    memberships: list[str]
    has_exchange_device: bool | None
    prefers_emi: bool | None
    pasted_offer_text: str | None
    needs_clarification: bool
    clarification_question: str | None


class PlanStep(BaseModel):
    tool: str
    goal: str


class ResearchPlan(BaseModel):
    steps: list[PlanStep]


class Recommendation(BaseModel):
    listing_id: str
    product: str
    verdict: Literal["top_pick", "strong_alternative", "budget_pick", "consider", "not_recommended"]
    price_summary: str = Field(description="Effective price with its conditions, quoted from facts")
    why: list[str]
    trade_offs: list[str]
    evidence_ids: list[str]


class SuggestedAction(BaseModel):
    action: Literal["track_price", "price_alert", "save_preference"]
    listing_id: str | None
    target_price: float | None
    detail: str


class FinalAnswer(BaseModel):
    summary: str
    recommendations: list[Recommendation]
    deal_insights: list[str]
    timing: str | None
    risks_and_conflicts: list[str]
    uncertainties: list[str]
    questions_for_user: list[str]
    suggested_actions: list[SuggestedAction]


class LLMReviewTheme(BaseModel):
    kind: ThemeKind
    theme: str
    mentions: int
    source_urls: list[str]
    use_case: str | None


class ReviewAnalysis(BaseModel):
    themes: list[LLMReviewTheme]


class ExtractedOffer(BaseModel):
    type: OfferType
    title: str
    provider: str | None
    flat_amount: float | None
    percent: float | None
    max_discount: float | None
    min_order_value: float | None
    issuer: str | None
    card_type: CardType | None
    network: str | None
    emi_rule: EmiRule
    membership_required: str | None
    exchange_max_value: float | None
    exchange_min_value: float | None
    exchange_bonus: float | None
    combinable_with: list[OfferType]
    exclusive: bool
    cashback_deferred: bool
    valid_from: str | None
    valid_to: str | None


class OfferExtraction(BaseModel):
    offers: list[ExtractedOffer]
