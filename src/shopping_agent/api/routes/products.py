"""Products and listings: search, link resolution, details, sellers, offers, price history."""

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from shopping_agent.api.deps import ToolResponse, get_runtime, require_api_key
from shopping_agent.db.repositories import CatalogRepository
from shopping_agent.runtime import Runtime
from shopping_agent.tools.common import ListingSummary, summarize

router = APIRouter(tags=["products"], dependencies=[Depends(require_api_key)])


class LinkRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2000)


@router.get("/search")
async def search(
    q: str = Query(min_length=2, max_length=200),
    category: str | None = None,
    max_price: Decimal | None = Query(default=None, gt=0),
    limit: int = Query(default=10, ge=1, le=20),
    runtime: Runtime = Depends(get_runtime),
) -> ToolResponse:
    args = {"query": q, "category": category, "max_price": max_price, "limit": limit}
    return ToolResponse.of(
        await runtime.call_tool("search_products", {k: v for k, v in args.items() if v is not None})
    )


@router.post("/products/resolve-link")
async def resolve_link(body: LinkRequest, runtime: Runtime = Depends(get_runtime)) -> ToolResponse:
    return ToolResponse.of(await runtime.call_tool("resolve_product_link", {"url": body.url}))


@router.get("/listings/{listing_id}")
async def get_listing(listing_id: str, runtime: Runtime = Depends(get_runtime)) -> ListingSummary:
    async with runtime.sessions() as session:
        row = await CatalogRepository(session).get_listing(listing_id)
    if row is None:
        raise HTTPException(404, "listing not found")
    return summarize(row, runtime.tool_context())


@router.get("/listings/{listing_id}/details")
async def listing_details(listing_id: str, runtime: Runtime = Depends(get_runtime)) -> ToolResponse:
    return ToolResponse.of(
        await runtime.call_tool("get_product_details", {"listing_id": listing_id})
    )


@router.get("/listings/{listing_id}/sellers")
async def listing_sellers(listing_id: str, runtime: Runtime = Depends(get_runtime)) -> ToolResponse:
    return ToolResponse.of(await runtime.call_tool("get_listings", {"listing_id": listing_id}))


@router.get("/listings/{listing_id}/offers")
async def listing_offers(listing_id: str, runtime: Runtime = Depends(get_runtime)) -> ToolResponse:
    """Offers and effective-price scenarios for your saved payment methods and memberships."""
    profile = await runtime.load_profile()
    return ToolResponse.of(
        await runtime.call_tool("find_offers", {"listing_id": listing_id}, profile=profile)
    )


@router.get("/listings/{listing_id}/price-history")
async def listing_history(
    listing_id: str,
    days: int = Query(default=90, ge=1, le=365),
    runtime: Runtime = Depends(get_runtime),
) -> ToolResponse:
    return ToolResponse.of(
        await runtime.call_tool("get_price_history", {"listing_id": listing_id, "days": days})
    )


@router.get("/listings/{listing_id}/reviews")
async def listing_reviews(
    listing_id: str, use_case: str | None = None, runtime: Runtime = Depends(get_runtime)
) -> ToolResponse:
    args = {"listing_id": listing_id, **({"use_case": use_case} if use_case else {})}
    return ToolResponse.of(await runtime.call_tool("research_reviews", args))
