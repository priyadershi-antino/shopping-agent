"""All read-only tools end to end on the demo catalog + test database."""

import asyncio
import json
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel

from shopping_agent.db.repositories import PriceRepository, SaleEventRepository
from shopping_agent.domain.common import Verification
from shopping_agent.domain.evidence import Evidence
from shopping_agent.domain.offer import CardType, OfferType
from shopping_agent.domain.product import Availability, Marketplace
from shopping_agent.domain.review import ReviewCoverage
from shopping_agent.domain.sale import SaleEvent, SaleState
from shopping_agent.services.variant_gate import GateDecision
from shopping_agent.tools import ToolContext, ToolRegistry, default_tools
from shopping_agent.tools.base import Tool, ToolResult
from shopping_agent.tools.registry import result_for_model
from tests.factories import NOW, profile

pytestmark = pytest.mark.db

NIMBUS_AMAZON = "https://www.amazon.in/Acme-Nimbus-15/dp/B0DEMO0001/ref=sr_1_1?tag=aff-21"
NIMBUS_FLIPKART = (
    "https://www.flipkart.com/acme-nimbus-15-nb15-g4/p/itmdemo000001?pid=COMDEMO000000001&affid=x"
)
NIMBUS_8GB_FLIPKART = (
    "https://www.flipkart.com/acme-nimbus-15-nb15-g4-8gb/p/itmdemo000002?pid=COMDEMO000000002"
)
PROBOOK_AMAZON = "https://www.amazon.in/dp/B0DEMO0003"

REGISTRY = ToolRegistry(default_tools())


@dataclass
class Called:
    data: Any  # the tool's output model (asserted present)
    evidence: list[Evidence]
    raw: ToolResult[Any]


async def call(ctx: ToolContext, tool: str, **args: Any) -> Called:
    result, record = await REGISTRY.execute(tool, args, ctx)
    assert record.status in {"ok", "partial"}, record.error
    assert result.data is not None, result.errors
    return Called(data=result.data, evidence=result.evidence, raw=result)


async def listing_id(ctx: ToolContext, url: str) -> str:
    result = await call(ctx, "resolve_product_link", url=url)
    assert result.data.status == "resolved"
    return str(result.data.listings[0].listing_id)


def test_registry_exposes_eleven_read_only_tools() -> None:
    specs = REGISTRY.specs()
    assert len(specs) == 11
    assert all(not s.side_effect for s in specs)
    assert all(s.parameters.get("type") == "object" for s in specs)


async def test_search_products_groups_variants(tool_ctx: ToolContext) -> None:
    result = await call(
        tool_ctx, "search_products", query="gaming laptop", category="laptop", max_price=80000
    )
    data = result.data
    assert data.demo_data
    nimbus = [c for c in data.candidates if c.model_number == "NB15-G4"]
    main = next(c for c in nimbus if c.attributes.ram_gb == 16)
    eight = next(c for c in nimbus if c.attributes.ram_gb == 8)
    assert {s.marketplace for s in main.listings} >= {Marketplace.AMAZON, Marketplace.FLIPKART}
    assert main.lowest_price == Decimal("73490.00")  # renewed unit excluded from lowest
    assert any("renewed" in f for f in main.risk_flags)
    assert main.variant_id is not None
    assert eight.key != main.key
    probook = next(c for c in data.candidates if c.model_number == "ZP14-2025")
    assert probook.over_budget
    assert data.note
    assert len(result.evidence) == data.total_listings


async def test_resolve_links(tool_ctx: ToolContext) -> None:
    amazon = await call(tool_ctx, "resolve_product_link", url=NIMBUS_AMAZON)
    assert amazon.data.status == "resolved"
    assert amazon.data.parsed.canonical_url == "https://www.amazon.in/dp/B0DEMO0001"
    assert amazon.data.listings[0].price == Decimal("74990.00")

    flipkart = await call(tool_ctx, "resolve_product_link", url=NIMBUS_FLIPKART)
    assert flipkart.data.status == "resolved"
    assert flipkart.data.listings[0].marketplace is Marketplace.FLIPKART

    unknown = await call(
        tool_ctx,
        "resolve_product_link",
        url="https://www.amazon.in/Zenith-ProBook-14/dp/B0ZZZZZZZZ",
    )
    assert unknown.data.status == "needs_confirmation"
    assert any(c.model_number == "ZP14-2025" for c in unknown.data.candidates)

    short = await call(tool_ctx, "resolve_product_link", url="https://amzn.in/d/abc123")
    assert short.data.status == "needs_full_link"

    bad, record = await REGISTRY.execute("resolve_product_link", {"url": "ftp://x.com/a"}, tool_ctx)
    assert record.status == "partial"
    assert bad.errors


async def test_product_details_flags_spec_conflict(tool_ctx: ToolContext) -> None:
    lid = await listing_id(tool_ctx, NIMBUS_8GB_FLIPKART)
    result = await call(tool_ctx, "get_product_details", listing_id=lid)
    data = result.data
    assert data.specs["ram_gb"] == 16
    assert data.verification is Verification.VERIFIED
    (conflict,) = data.conflicts
    assert conflict.attribute == "ram_gb"
    assert conflict.preferred_value == "16"
    assert conflict.resolved

    by_name = await call(tool_ctx, "get_product_details", name="Acme Nimbus 15 (NB15-G4)")
    assert by_name.data.model_number == "NB15-G4"
    assert by_name.data.conflicts == []


async def test_get_listings_applies_variant_gate(tool_ctx: ToolContext) -> None:
    lid = await listing_id(tool_ctx, NIMBUS_AMAZON)
    result = await call(tool_ctx, "get_listings", listing_id=lid)
    data = result.data
    matching = {m.listing.marketplace: m for m in data.matching}
    assert set(matching) == {Marketplace.AMAZON, Marketplace.FLIPKART, Marketplace.CROMA}
    assert matching[Marketplace.CROMA].decision is GateDecision.PASS_UNVERIFIED
    assert "ram_gb" in matching[Marketplace.CROMA].unverified
    reasons = " | ".join(r for rej in data.rejected for r in rej.reasons)
    assert "ram_gb is 8" in reasons
    assert "condition is renewed" in reasons
    assert data.matching[0].listing.price == Decimal("73490.00")


async def test_find_offers_scenarios_for_hdfc_user(tool_ctx: ToolContext) -> None:
    lid = await listing_id(tool_ctx, NIMBUS_FLIPKART)
    tool_ctx.profile = profile(("HDFC", CardType.CREDIT))
    result = await call(tool_ctx, "find_offers", listing_id=lid)
    data = result.data
    labels = {s.label: s for s in data.scenarios}
    assert list(labels) == ["A", "B", "C", "D"]
    assert labels["A"].upfront_conservative == Decimal("73490.00")
    assert labels["B"].upfront_conservative == Decimal("69490.00")
    assert labels["C"].upfront_conservative == Decimal("67490.00")
    assert [a.type for a in labels["C"].applied] == [OfferType.COUPON, OfferType.BANK]
    assert labels["D"].conditional
    assert labels["D"].upfront_best == Decimal("55490.00")
    assert any("Axis" in e.title for e in data.excluded)
    cards = {row.payment: row.best.upfront_conservative for row in data.card_matrix}
    assert cards["Without card offers"] == Decimal("71490.00")
    assert cards["HDFC credit card"] == Decimal("67490.00")
    assert data.unstructured_offer_texts[0].startswith("<untrusted_content")
    assert any(e.attribute == "scenario_C" for e in result.evidence)


async def test_find_offers_without_profile_marks_cards_conditional(tool_ctx: ToolContext) -> None:
    lid = await listing_id(tool_ctx, NIMBUS_FLIPKART)
    result = await call(tool_ctx, "find_offers", listing_id=lid)
    assert result.data.notes
    assert result.data.card_matrix == []
    assert all(not s.conditional for s in result.data.scenarios if s.label in {"B", "C"})


async def test_price_history_and_pre_sale_hike(tool_ctx: ToolContext) -> None:
    lid = await listing_id(tool_ctx, NIMBUS_AMAZON)
    async with tool_ctx.sessions() as session, session.begin():
        prices = PriceRepository(session)
        for days_ago in range(40, 0, -1):
            price = Decimal(79000) if days_ago <= 7 else Decimal(75000)
            await prices.record(
                lid,
                price=price,
                mrp=None,
                availability=Availability.IN_STOCK,
                source="test",
                observed_at=NOW - timedelta(days=days_ago),
            )
        await SaleEventRepository(session).upsert(
            SaleEvent(
                name="Demo Sale",
                platform=Marketplace.AMAZON,
                start=NOW + timedelta(days=2),
                end=NOW + timedelta(days=8),
                verification=Verification.VERIFIED,
                last_verified_at=NOW,
            )
        )
    result = await call(tool_ctx, "get_price_history", listing_id=lid, days=90)
    (history,) = result.data.listings
    baseline = history.baseline
    assert baseline is not None
    assert baseline.coverage_days == 41
    assert baseline.current == Decimal("74990.00")
    assert baseline.at_or_below_observed_min
    assert history.low_note is not None
    assert "41-day observed history" in history.low_note
    assert history.pre_sale_hike is not None
    assert history.pre_sale_hike.detected
    assert len(history.daily) == 41
    assert "not complete market history" in result.data.coverage_note

    _, record = await REGISTRY.execute("get_price_history", {"days": 30}, tool_ctx)
    assert record.status == "invalid"


async def test_sale_events(tool_ctx: ToolContext) -> None:
    empty = await call(tool_ctx, "get_sale_events")
    assert empty.data.note
    async with tool_ctx.sessions() as session, session.begin():
        await SaleEventRepository(session).upsert(
            SaleEvent(
                name="Demo Festive Sale",
                platform=Marketplace.FLIPKART,
                start=NOW + timedelta(days=6),
                end=NOW + timedelta(days=12),
                partner_banks=["HDFC"],
                verification=Verification.SOURCE_REPORTED,
                last_verified_at=NOW - timedelta(days=2),
            )
        )
    result = await call(tool_ctx, "get_sale_events", platform="flipkart")
    (view,) = result.data.events
    assert view.state_now is SaleState.PRE_SALE
    assert not view.verified
    assert view.needs_reverification
    assert result.data.current.state is SaleState.PRE_SALE


async def test_find_alternatives_cheaper(tool_ctx: ToolContext) -> None:
    lid = await listing_id(tool_ctx, PROBOOK_AMAZON)
    result = await call(tool_ctx, "find_alternatives", listing_id=lid, cheaper_by=10000)
    data = result.data
    assert data.price_ceiling == Decimal("72990.00")
    best = data.alternatives[0]
    assert best.candidate.model_number == "NS16-R5"
    # The ProBook has a 1 TB SSD; the Swift's 512 GB is reported as failing, not hidden.
    assert {c.requirement: c.status.value for c in best.comparison} == {
        "ram_gb": "meets",
        "storage_gb": "fails",
    }
    assert best.price_difference <= Decimal(-10000)
    for alt in data.alternatives:
        assert alt.candidate.model_number != "ZP14-2025"
        assert all(s.condition.value == "new" for s in alt.candidate.listings)

    ram_only = await call(
        tool_ctx, "find_alternatives", listing_id=lid, cheaper_by=10000, keep_at_least=["ram_gb"]
    )
    top = ram_only.data.alternatives[0]
    assert top.candidate.model_number == "NS16-R5"
    assert [c.status.value for c in top.comparison] == ["meets"]


async def test_search_web_flags_injection(tool_ctx: ToolContext) -> None:
    result = await call(tool_ctx, "search_web", query="acme nimbus 15 deal price")
    (item,) = result.data.results
    assert "override_instructions" in item.injection_flags
    assert item.content.startswith("<untrusted_content")
    assert "external data" in result.data.note


async def test_fetch_page(tool_ctx: ToolContext) -> None:
    spec = await call(tool_ctx, "fetch_page", url="https://acme.demo.invalid/nimbus-15")
    assert "16GB DDR5" in spec.data.content
    assert spec.data.injection_flags == []
    hostile = await call(tool_ctx, "fetch_page", url="https://deals.demo.invalid/nimbus-15-deal")
    assert "override_instructions" in hostile.data.injection_flags
    blocked, record = await REGISTRY.execute(
        "fetch_page", {"url": "https://www.gsmarena.com/x"}, tool_ctx
    )
    assert record.status == "partial"
    assert blocked.data is None


async def test_research_reviews(tool_ctx: ToolContext) -> None:
    lid = await listing_id(tool_ctx, NIMBUS_AMAZON)
    await call(tool_ctx, "get_listings", listing_id=lid)  # brings in the other sellers' ratings
    result = await call(tool_ctx, "research_reviews", listing_id=lid, use_case="gaming")
    data = result.data
    assert "Nimbus" in data.subject
    assert len(data.sources) == 2
    assert {r.marketplace for r in data.ratings} >= {"amazon", "flipkart"}
    assert data.coverage is ReviewCoverage.MODERATE
    assert data.findings is None
    assert "opinions" in data.note


# --- registry behaviour ---


class _Args(BaseModel):
    value: int = 0


class _Out(BaseModel):
    ok: bool = True


class SlowTool(Tool[_Args, _Out]):
    name: ClassVar[str] = "slow"
    description: ClassVar[str] = "sleeps"
    timeout_s: ClassVar[float] = 0.05
    input_model = _Args
    output_model = _Out

    async def run(self, ctx: ToolContext, args: _Args) -> ToolResult[_Out]:
        await asyncio.sleep(1)
        return ToolResult(data=_Out())


class CrashTool(SlowTool):
    name: ClassVar[str] = "crash"

    async def run(self, ctx: ToolContext, args: _Args) -> ToolResult[_Out]:
        raise RuntimeError("secret internals")


class WriteTool(SlowTool):
    name: ClassVar[str] = "write"
    side_effect: ClassVar[bool] = True
    timeout_s: ClassVar[float] = 5

    async def run(self, ctx: ToolContext, args: _Args) -> ToolResult[_Out]:
        return ToolResult(data=_Out())


async def test_registry_guards(tool_ctx: ToolContext) -> None:
    registry = ToolRegistry([SlowTool(), CrashTool(), WriteTool(), *default_tools()])

    _, rec = await registry.execute("nope", {}, tool_ctx)
    assert rec.status == "unknown_tool"
    _, rec = await registry.execute("slow", {}, tool_ctx)
    assert rec.status == "timeout"
    result, rec = await registry.execute("crash", {}, tool_ctx)
    assert rec.status == "error"
    assert "secret internals" not in result.errors[0]
    _, rec = await registry.execute("write", {}, tool_ctx)
    assert rec.status == "denied"
    _, rec = await registry.execute("write", {}, tool_ctx, allow_side_effects=True)
    assert rec.status == "ok"
    _, rec = await registry.execute("search_products", {"query": "x"}, tool_ctx)
    assert rec.status == "invalid"
    assert rec.error is not None
    assert "query" in rec.error
    result, rec = await registry.execute("find_offers", {"listing_id": "missing"}, tool_ctx)
    assert rec.status == "error"
    assert "unknown listing_id" in result.errors[0]
    assert "write" not in {s.name for s in registry.specs()}
    assert "write" in {s.name for s in registry.specs(include_side_effects=True)}


async def test_result_for_model_is_compact_json(tool_ctx: ToolContext) -> None:
    result = await call(tool_ctx, "search_products", query="nova pixelon", category="smartphone")
    payload = json.loads(result_for_model(result.raw))
    assert payload["data"]["candidates"]
    assert payload["evidence"][0]["id"]
    assert "partial" in payload


async def test_parallel_overlapping_searches_do_not_deadlock(tool_ctx: ToolContext) -> None:
    """The model may run several searches at once; their ingests overlap on the same products."""
    import asyncio

    from shopping_agent.tools import ToolRegistry, default_tools

    registry = ToolRegistry(default_tools())
    queries = [
        "gaming laptop",
        "acme nimbus laptop",
        "nova swift laptop",
        "laptop rtx",
        "nimbus",
        "swift",
    ]
    for _ in range(3):
        outcomes = await asyncio.gather(
            *(registry.execute("search_products", {"query": q}, tool_ctx) for q in queries)
        )
        assert [record.status for _, record in outcomes] == ["ok"] * len(queries)
