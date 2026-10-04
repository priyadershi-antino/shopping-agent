"""Agent runs end to end with a scripted LLM, the demo catalog and the test database."""

import json
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shopping_agent.agent.orchestrator import AgentEvent, RunBudget, ShoppingAgent
from shopping_agent.agent.state import AgentState, RunStatus
from shopping_agent.agent.store import SqlRunStore
from shopping_agent.db.models import AgentRunRow, OfferRow, ToolCallRow
from shopping_agent.errors import ProviderError
from shopping_agent.llm.types import Message, ResponseSchema, Role, ToolCall, ToolDef
from shopping_agent.tools import ToolContext, ToolRegistry, default_tools
from tests.factories import NOW
from tests.fakes import (
    Handler,
    analysis_json,
    facts_from,
    research_turns,
    scripted_client,
)
from tests.scenarios import PLAN, discovery_handler, final_answer

pytestmark = pytest.mark.db

Sessions = async_sessionmaker[AsyncSession]


def make_agent(
    ctx: ToolContext, db: Sessions, handler: Handler, **budget: Any
) -> tuple[ShoppingAgent, Any]:
    llm, provider = scripted_client(handler)
    agent = ShoppingAgent(
        llm=llm,
        tools=ToolRegistry(default_tools()),
        tool_context=ctx,
        budget=RunBudget(**budget) if budget else None,
        store=SqlRunStore(db),
        clock=lambda: NOW,
    )
    return agent, provider


async def test_discovery_run(tool_ctx: ToolContext, db: Sessions) -> None:
    agent, _ = make_agent(tool_ctx, db, discovery_handler)
    events: list[AgentEvent] = []

    async def collect(e: AgentEvent) -> None:
        events.append(e)

    state = await agent.run(
        "laptop under 80k for coding and gaming, I have an HDFC credit card", on_event=collect
    )

    assert state.status is RunStatus.COMPLETED, state.error
    assert state.demo_data
    assert state.requirements is not None
    assert state.requirements.budget.maximum is not None
    assert [m.describe() for m in state.profile.payment_methods] == ["HDFC credit card"]
    assert state.plan is not None
    assert [s.tool for s in state.plan.steps] == ["search_products", "get_product_details"]

    tools_used = [c.tool for c in state.tool_calls]
    assert tools_used[:3] == ["search_products", "get_product_details", "search_products"]
    assert state.tool_calls[2].status == "invalid"
    # Deterministic enrichment ran even though the model never asked for it.
    assert {
        "get_sale_events",
        "get_listings",
        "find_offers",
        "get_price_history",
        "research_reviews",
    } <= set(tools_used)

    top = state.focus[0]
    assert top.best_eligible < top.listing.price  # HDFC offer applied by the offer engine
    assert top.listing.listing_id in state.offers

    final = state.final
    assert final is not None
    assert [r.listing_id for r in final.recommendations] == [
        top.listing.listing_id
    ]  # made-up id dropped
    assert "₹9,999 [unverified]" in final.summary
    assert state.grounding is not None
    assert [m.raw for m in state.grounding.ungrounded] == ["₹9,999"]
    assert [(a.action, a.listing_id) for a in state.proposed_actions] == [
        ("price_alert", top.listing.listing_id)
    ]

    review = next(iter(state.reviews.values()))
    assert review.findings is not None
    assert [t.theme for t in review.findings.themes] == ["loud fans"]  # unsupported theme dropped

    assert events[0].type == "stage"
    assert events[-1].type == "done"
    assert any(e.type == "tool_call" for e in events)
    assert state.usage.total.input_tokens > 0

    async with db() as session:
        row = await session.get(AgentRunRow, state.run_id)
        assert row is not None
        assert row.status == "completed"
        calls = await session.scalar(
            select(func.count()).select_from(ToolCallRow).where(ToolCallRow.run_id == state.run_id)
        )
    assert calls == len(state.tool_calls)
    restored = await SqlRunStore(db).load(state.run_id)
    assert restored is not None
    assert restored.final == state.final


async def test_vague_request_asks_one_question(tool_ctx: ToolContext, db: Sessions) -> None:
    def handler(
        messages: list[Message], tools: list[ToolDef] | None, schema: ResponseSchema | None
    ) -> Any:
        assert schema is not None
        assert schema.name == "RequestAnalysis"
        return analysis_json(
            intent="general",
            category=None,
            budget={"minimum": None, "maximum": None, "target": None, "approximate": False},
            payment_methods=[],
            needs_clarification=True,
            clarification_question="What would you like to buy?",
        )

    agent, provider = make_agent(tool_ctx, db, handler)
    state = await agent.run("help me buy something nice")
    assert state.status is RunStatus.NEEDS_CLARIFICATION
    assert state.clarification_question == "What would you like to buy?"
    assert state.tool_calls == []
    assert len(provider.calls) == 1


async def test_research_stops_at_tool_budget(tool_ctx: ToolContext, db: Sessions) -> None:
    def handler(
        messages: list[Message], tools: list[ToolDef] | None, schema: ResponseSchema | None
    ) -> Any:
        if schema is not None:
            if schema.name == "FinalAnswer":
                return "{}"  # invalid twice → computed fallback answer
            return analysis_json() if schema.name == "RequestAnalysis" else PLAN
        return [
            ToolCall(
                id=f"w{research_turns(messages)}-{i}",
                name="search_web",
                arguments={"query": "acme nimbus review"},
            )
            for i in range(2)
        ]

    agent, _ = make_agent(tool_ctx, db, handler, max_iterations=10, max_tool_calls=3)
    state = await agent.run("laptop under 80k")
    llm_driven = [c for c in state.tool_calls if c.tool == "search_web"]
    assert len(llm_driven) == 3
    assert any("tool-call budget" in u for u in state.uncertainties)
    assert state.status is RunStatus.COMPLETED
    assert state.final is not None
    assert "computed results" in state.final.summary or "No suitable" in state.final.summary
    assert any("summary generation failed" in w for w in state.warnings)


async def test_write_and_unknown_tools_are_refused(tool_ctx: ToolContext, db: Sessions) -> None:
    def handler(
        messages: list[Message], tools: list[ToolDef] | None, schema: ResponseSchema | None
    ) -> Any:
        if schema is not None:
            if schema.name == "RequestAnalysis":
                return analysis_json()
            if schema.name == "ResearchPlan":
                return PLAN
            return final_answer("none", "₹1")
        if research_turns(messages) == 0:
            assert tools is not None
            assert {t.name for t in tools}.isdisjoint({"track_product", "create_price_alert"})
            return [ToolCall(id="evil", name="track_product", arguments={"listing_id": "x"})]
        last = [m for m in messages if m.role is Role.TOOL][-1]
        assert last.is_error
        return "DONE"

    agent, _ = make_agent(tool_ctx, db, handler)
    state = await agent.run("laptop under 80k")
    assert state.tool_calls[0].status == "unknown_tool"
    assert state.status is RunStatus.COMPLETED


async def test_llm_outage_fails_cleanly(tool_ctx: ToolContext, db: Sessions) -> None:
    agent, _ = make_agent(
        tool_ctx, db, lambda *_: ProviderError("fake", "server error 503", transient=True)
    )
    state = await agent.run("laptop under 80k")
    assert state.status is RunStatus.FAILED
    assert state.error is not None
    assert "unavailable" in state.error
    async with db() as session:
        row = await session.get(AgentRunRow, state.run_id)
    assert row is not None
    assert row.status == "failed"
    assert "try again later" not in state.error  # only said for rate limits


async def test_rate_limit_failure_says_what_to_do(tool_ctx: ToolContext, db: Sessions) -> None:
    from shopping_agent.errors import RateLimitedError

    agent, _ = make_agent(tool_ctx, db, lambda *_: RateLimitedError("gemini"))
    state = await agent.run("laptop under 80k")
    assert state.status is RunStatus.FAILED
    assert state.error is not None and "LLM_FALLBACKS" in state.error


async def test_link_with_pasted_offer(tool_ctx: ToolContext, db: Sessions) -> None:
    offer_text = (
        "7.5% instant discount up to Rs 3,000 on ICICI Bank credit card EMI, min order Rs 40,000"
    )

    def handler(
        messages: list[Message], tools: list[ToolDef] | None, schema: ResponseSchema | None
    ) -> Any:
        if schema is not None:
            if schema.name == "RequestAnalysis":
                return analysis_json(
                    intent="deal_check",
                    category=None,
                    budget={"minimum": None, "maximum": None, "target": None, "approximate": False},
                    payment_methods=[{"issuer": "ICICI", "card_type": "credit"}],
                    prefers_emi=True,
                    pasted_offer_text=offer_text,
                )
            if schema.name == "OfferExtraction":
                assert "<untrusted_content" in messages[-1].content
                return json.dumps(
                    {
                        "offers": [
                            {
                                "type": "bank",
                                "title": "7.5% off ICICI EMI",
                                "provider": "ICICI",
                                "flat_amount": None,
                                "percent": 7.5,
                                "max_discount": 3000,
                                "min_order_value": 40000,
                                "issuer": "ICICI",
                                "card_type": "credit",
                                "network": None,
                                "emi_rule": "emi_only",
                                "membership_required": None,
                                "exchange_max_value": None,
                                "exchange_min_value": None,
                                "exchange_bonus": None,
                                "combinable_with": [],
                                "exclusive": False,
                                "cashback_deferred": False,
                                "valid_from": None,
                                "valid_to": None,
                            }
                        ]
                    }
                )
            if schema.name == "ResearchPlan":
                return json.dumps({"steps": []})
            if schema.name == "ReviewAnalysis":
                return json.dumps({"themes": []})
            facts = facts_from(messages)
            top = facts["candidates_in_order"][0]
            assert any("ICICI" in s for s in json.dumps(top["offer_scenarios"]).split('"'))
            return final_answer(top["listing_id"], top["best_unconditional_price"])
        return "DONE"

    agent, _ = make_agent(tool_ctx, db, handler)
    url = "https://www.flipkart.com/acme-nimbus-15-nb15-g4/p/itmdemo000001?pid=COMDEMO000000001"
    state: AgentState = await agent.run(
        f"Is this a good deal? {url} The page shows '{offer_text}'. I have an ICICI credit card"
    )
    assert state.status is RunStatus.COMPLETED, state.error
    assert state.tool_calls[0].tool == "resolve_product_link"
    assert len(state.linked_listing_ids) == 1
    lid = state.linked_listing_ids[0]
    assert state.requirements is not None
    assert state.requirements.budget.maximum is None  # the offer's ₹3,000 is not a budget
    async with db() as session:
        user_offers = await session.scalar(
            select(func.count())
            .select_from(OfferRow)
            .where(OfferRow.source == "user", OfferRow.listing_id == lid)
        )
    assert user_offers == 1
    scenarios = state.offers[lid].scenarios
    icici = [s for s in scenarios if any("ICICI" in a.title for a in s.applied)]
    assert icici
    assert icici[0].upfront_conservative == 73490 - 3000


async def test_cheapest_seller_is_judged_with_the_users_cards(
    tool_ctx: ToolContext, db: Sessions
) -> None:
    """Flipkart lists the Nova Swift lower (₹68,990), but with an SBI card Amazon's ₹69,990
    becomes ₹66,490: the shortlist must show the seller where this user pays least."""

    def handler(
        messages: list[Message], tools: list[ToolDef] | None, schema: ResponseSchema | None
    ) -> Any:
        if schema is not None and schema.name == "RequestAnalysis":
            return analysis_json(payment_methods=[{"issuer": "SBI", "card_type": "credit"}])
        return discovery_handler(messages, tools, schema)

    agent, _ = make_agent(tool_ctx, db, handler)
    state = await agent.run("gaming laptop under 80k, I pay with an SBI credit card")
    assert state.status is RunStatus.COMPLETED, state.error
    swift = next(f for f in state.focus if "NS16-R5" in f.listing.title)
    assert swift.listing.marketplace.value == "amazon"
    assert swift.best_eligible == 66490


async def test_follow_up_keeps_the_earlier_products(tool_ctx: ToolContext, db: Sessions) -> None:
    """ "Which of those has the better GPU?" names no product: the earlier answer's products
    must be carried into the new run, or the agent has nothing to compare."""
    from shopping_agent.agent.views import follow_up

    agent, _ = make_agent(tool_ctx, db, discovery_handler)
    first = await agent.run("gaming laptop under 80k, I have an HDFC credit card")
    context, carried = follow_up(first)
    assert carried

    seen: list[str] = []

    def handler(
        messages: list[Message], tools: list[ToolDef] | None, schema: ResponseSchema | None
    ) -> Any:
        if schema is not None and schema.name == "RequestAnalysis":
            return analysis_json(
                intent="compare", category=None, product_mentions=[], payment_methods=[]
            )
        if schema is None:  # research turn: record what the model was told, research nothing
            seen.append(messages[-1].content)
            return "DONE: nothing new to look up"
        return discovery_handler(messages, tools, schema)

    follow, _ = make_agent(tool_ctx, db, handler)
    state = await follow.run(
        "Which of those has the better GPU?", context=context, carry_listing_ids=carried
    )
    assert state.status is RunStatus.COMPLETED, state.error
    assert {f.listing.listing_id for f in state.focus} >= set(carried[:1])
    assert "Earlier conversation:" in seen[0] and "Products from the earlier answer" in seen[0]
    assert state.final is not None and state.final.recommendations


async def test_facts_explain_card_offers_that_do_not_help(
    tool_ctx: ToolContext, db: Sessions
) -> None:
    """A Flipkart Axis card's 5% cashback can't be combined with the coupon, so it doesn't
    change the best price; the facts must still tell the model about it and why."""
    captured: dict[str, Any] = {}

    def handler(
        messages: list[Message], tools: list[ToolDef] | None, schema: ResponseSchema | None
    ) -> Any:
        if schema is not None and schema.name == "RequestAnalysis":
            return analysis_json(
                payment_methods=[{"issuer": "Flipkart Axis Bank", "card_type": "credit"}]
            )
        if schema is not None and schema.name == "FinalAnswer":
            captured.update(facts_from(messages))
        return discovery_handler(messages, tools, schema)

    agent, _ = make_agent(tool_ctx, db, handler)
    state = await agent.run("Nimbus 15 on Flipkart, I have a Flipkart Axis Bank credit card")
    assert state.status is RunStatus.COMPLETED, state.error
    nimbus = next(
        c
        for c in captured["candidates_in_order"]
        if c["marketplace"] == "flipkart" and "NB15-G4" in c["product"] and "16 GB" in c["product"]
    )
    cashback = next(o for o in nimbus["offers_for_you"] if "cashback" in o["offer"].lower())
    assert cashback["status"] == "eligible" and cashback["paid_later"] is True
    assert any(
        "cashback" in n.lower() and "coupon" in n.lower() for n in nimbus["offers_not_combined"]
    )
