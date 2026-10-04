"""Offer tools: parse pasted offer terms, true cost of No-Cost EMI, budget planning, sales."""

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from shopping_agent.agent.analyzers import parse_offer_text
from shopping_agent.api.deps import ToolResponse, get_runtime, require_api_key
from shopping_agent.db.repositories import CatalogRepository, OfferRepository
from shopping_agent.domain.offer import Offer
from shopping_agent.domain.product import Marketplace
from shopping_agent.llm.client import RunUsage
from shopping_agent.runtime import Runtime
from shopping_agent.services.budget_planner import BudgetPlan, Slot, SortOrder, plan_budget
from shopping_agent.services.offer_engine import EmiCost, no_cost_emi_true_cost
from shopping_agent.tools.base import utcnow

router = APIRouter(tags=["offers & planning"], dependencies=[Depends(require_api_key)])


class ParseOfferRequest(BaseModel):
    text: str = Field(
        min_length=5, max_length=4000, description="Offer or coupon terms as shown on the page"
    )
    listing_id: str | None = Field(default=None, description="Listing the offer applies to")
    save: bool = Field(
        default=False, description="Store the offers for that listing (needs listing_id)"
    )


class ParseOfferResponse(BaseModel):
    offers: list[Offer]
    warnings: list[str]
    saved: bool


class EmiRequest(BaseModel):
    price: Decimal = Field(gt=0)
    tenure_months: int = Field(ge=1, le=60)
    annual_rate_pct: Decimal = Field(ge=0, le=60)
    processing_fee: Decimal = Field(default=Decimal(0), ge=0)
    discount_forgone: Decimal = Field(
        default=Decimal(0), ge=0, description="Discount available only on full payment"
    )


class BudgetRequest(BaseModel):
    budget: Decimal = Field(gt=0)
    slots: list[Slot]
    sort: SortOrder = SortOrder.USE_MOST_BUDGET
    limit: int = Field(default=20, ge=1, le=100)


@router.post("/offers/parse")
async def parse_offer(
    body: ParseOfferRequest, runtime: Runtime = Depends(get_runtime)
) -> ParseOfferResponse:
    platform = None
    if body.listing_id:
        async with runtime.sessions() as session:
            row = await CatalogRepository(session).get_listing(body.listing_id)
        if row is None:
            raise HTTPException(404, "listing not found")
        platform = Marketplace(row.marketplace)
    if body.save and not body.listing_id:
        raise HTTPException(400, "saving an offer needs the listing it applies to")
    offers, warnings = await parse_offer_text(
        runtime.llm(), body.text, platform=platform, usage=RunUsage()
    )
    if body.save and body.listing_id:
        async with runtime.sessions() as session, session.begin():
            repo = OfferRepository(session)
            for offer in offers:
                await repo.upsert(offer, source="user", now=utcnow(), listing_id=body.listing_id)
    return ParseOfferResponse(offers=offers, warnings=warnings, saved=body.save)


@router.post("/offers/emi-cost")
async def emi_cost(body: EmiRequest) -> EmiCost:
    return no_cost_emi_true_cost(
        body.price,
        body.tenure_months,
        body.annual_rate_pct,
        processing_fee=body.processing_fee,
        discount_forgone=body.discount_forgone,
    )


@router.post("/budget/plan")
async def budget_plan(body: BudgetRequest) -> BudgetPlan:
    return plan_budget(body.slots, body.budget, sort=body.sort, limit=body.limit)


@router.get("/sale-events")
async def sale_events(
    platform: Marketplace | None = None,
    include_past: bool = Query(default=False),
    runtime: Runtime = Depends(get_runtime),
) -> ToolResponse:
    args: dict[str, object] = {"include_past": include_past}
    if platform:
        args["platform"] = platform.value
    return ToolResponse.of(await runtime.call_tool("get_sale_events", args))
