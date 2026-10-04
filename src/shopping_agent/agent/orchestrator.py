"""The shopping agent: a single agent with fixed stages and one bounded research loop.

    understand → (links, pasted offers) → plan → research loop (LLM + read-only tools)
    → deterministic analysis → synthesis (LLM) → grounding check → proposed actions

The LLM decides what to research and how to explain it; all prices, offer combinations and
statistics are computed by code. Write actions are only proposed, never executed here.
"""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

from shopping_agent.agent.analyzers import (
    ReviewAnalyzer,
    analyse_request,
    find_links,
    merge_profile,
    parse_offer_text,
    plan_research,
    system_prompt,
    to_requirements,
)
from shopping_agent.agent.facts import (
    build_facts,
    facts_json,
    fallback_answer,
    ground_answer,
    rank_subjects,
    seller_pool,
    subjects,
)
from shopping_agent.agent.schemas import FinalAnswer, Intent
from shopping_agent.agent.state import AgentState, ProposedAction, RunStatus
from shopping_agent.db.repositories import CatalogRepository, OfferRepository
from shopping_agent.domain.offer import UserProfile
from shopping_agent.domain.product import parse_marketplaces
from shopping_agent.errors import (
    LLMRefusalError,
    LLMUnavailableError,
    ShoppingAgentError,
    ValidationFailedError,
)
from shopping_agent.llm.client import LLMClient, LLMRole
from shopping_agent.llm.structured import generate_structured
from shopping_agent.llm.types import Message, ToolCall, ToolDef
from shopping_agent.prompts import ALL_PROMPTS, load_prompt
from shopping_agent.services.normalization import title_similarity
from shopping_agent.services.url_parser import UrlParseError, parse_product_url
from shopping_agent.tools.base import ToolContext, ToolResult
from shopping_agent.tools.catalog_tools import (
    FindAlternativesOutput,
    GetListingsOutput,
    ProductDetailsOutput,
    ResolveLinkOutput,
    SearchProductsOutput,
)
from shopping_agent.tools.common import summarize
from shopping_agent.tools.deal_tools import FindOffersOutput, PriceHistoryOutput, SaleEventsOutput
from shopping_agent.tools.registry import ToolRegistry, result_for_model
from shopping_agent.tools.web_tools import ResearchReviewsOutput, SearchWebOutput

log = logging.getLogger(__name__)

FAILED_TOOL = {"error", "invalid", "denied", "timeout", "unknown_tool"}
REVIEW_INTENTS = {
    Intent.DISCOVER,
    Intent.COMPARE,
    Intent.LINK_LOOKUP,
    Intent.DEAL_CHECK,
    Intent.ALTERNATIVES,
}
SELLERS_PER_PRODUCT = 3  # sellers per shortlisted variant whose offers are checked
_LISTING_NOISE = {"url", "observed_at", "source", "product_id", "variant_id"}


class AgentEvent(BaseModel):
    type: Literal["stage", "tool_call", "tool_result", "warning", "done"]
    message: str
    data: dict[str, Any] = Field(default_factory=dict)


EventCallback = Callable[[AgentEvent], Awaitable[None]]


class RunStore:
    """Persistence hook (see agent/store.py)."""

    async def save(self, state: AgentState) -> None:  # pragma: no cover - interface
        raise NotImplementedError


@dataclass(frozen=True)
class RunBudget:
    max_iterations: int = 6
    max_tool_calls: int = 25
    max_seconds: float = 150.0


def _prune(value: Any) -> Any:
    """Smaller tool payloads for the model: drop per-listing bookkeeping fields."""
    if isinstance(value, dict):
        is_listing = "listing_id" in value and "price" in value
        return {
            k: _prune(v)
            for k, v in value.items()
            if not (is_listing and k in _LISTING_NOISE) and v not in (None, [], {})
        }
    if isinstance(value, list):
        return [_prune(v) for v in value]
    return value


def payload_for_model(result: ToolResult[Any], max_chars: int) -> str:
    payload = json.loads(result_for_model(result, max_evidence=20))
    text = json.dumps(_prune(payload), ensure_ascii=False, default=str)
    if len(text) > max_chars:
        text = text[:max_chars] + ' …[truncated: ask for fewer results or a specific listing_id]"'
    return text


class ShoppingAgent:
    def __init__(
        self,
        *,
        llm: LLMClient,
        tools: ToolRegistry,
        tool_context: ToolContext,
        budget: RunBudget | None = None,
        store: RunStore | None = None,
        top_k: int = 3,
        max_tool_chars: int = 12_000,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.base_ctx = tool_context
        self.budget = budget or RunBudget()
        self.store = store
        self.top_k = top_k
        self.max_tool_chars = max_tool_chars
        self.clock = clock

    async def run(
        self,
        request: str,
        *,
        profile: UserProfile | None = None,
        context: str | None = None,
        carry_listing_ids: list[str] | None = None,
        platforms: list[str] | None = None,
        on_event: EventCallback | None = None,
    ) -> AgentState:
        """Run once. For a follow-up, pass the earlier conversation as `context` and the
        products it was about as `carry_listing_ids` (see `agent.views.follow_up`). `platforms`
        limits the listings used to those marketplaces (default: DEFAULT_PLATFORMS)."""
        allowed = parse_marketplaces(platforms or []) or self.base_ctx.platforms
        state = AgentState(
            created_at=self.clock(),
            request=request.strip(),
            demo_data=self.base_ctx.providers.demo_mode,
            profile=profile or UserProfile(),
            prompt_versions={name: load_prompt(name).version for name in ALL_PROMPTS},
            context=context,
            platforms=sorted(m.value for m in allowed) if allowed else [],
        )
        started = time.monotonic()

        async def emit(
            kind: Literal["stage", "tool_call", "tool_result", "warning", "done"],
            message: str,
            **data: Any,
        ) -> None:
            if on_event is not None:
                try:
                    await on_event(AgentEvent(type=kind, message=message, data=data))
                except Exception:  # a broken listener must not break the run
                    log.exception("event listener failed")

        try:
            await emit("stage", "Understanding your request")
            await self._carry_over(state, carry_listing_ids or [])
            await self._understand(state, context)
            if state.status is RunStatus.NEEDS_CLARIFICATION:
                return state
            ctx = replace(
                self.base_ctx,
                profile=state.profile,
                run_id=state.run_id,
                platforms=allowed,
                review_analyzer=ReviewAnalyzer(self.llm, state.usage),
            )
            await self._resolve_links(state, ctx, emit)
            await self._pasted_offers(state, ctx, emit)
            await emit("stage", "Planning the research")
            await self._plan(state)
            await emit("stage", "Researching products, prices and reviews")
            await self._research(state, ctx, emit, started)
            await emit("stage", "Checking offers, price history and requirements")
            await self._analyse(state, ctx, emit)
            await emit("stage", "Writing the answer")
            await self._synthesise(state, emit)
            self._propose_actions(state)
            state.status = RunStatus.COMPLETED
        except LLMRefusalError as exc:
            state.status, state.error = (
                RunStatus.FAILED,
                f"The model declined this request ({exc}).",
            )
        except LLMUnavailableError as exc:
            state.status = RunStatus.FAILED
            state.error = f"The language model is unavailable right now. {exc}"
            if "rate limited" in str(exc) or "quota" in str(exc).lower():
                state.error += (
                    ". The model's request limit (for example a free tier's daily quota) may be "
                    "used up: try again later, or add another model in LLM_FALLBACKS "
                    "(see docs/configuration.md)."
                )
        except ShoppingAgentError as exc:
            state.status, state.error = RunStatus.FAILED, str(exc)
        except Exception:
            log.exception("agent run %s failed", state.run_id)
            state.status, state.error = RunStatus.FAILED, "An internal error stopped the research."
        finally:
            state.finished_at = self.clock()
            if self.store is not None:
                try:
                    await self.store.save(state)
                except Exception:
                    log.exception("could not save run %s", state.run_id)
            log.info(
                "agent_run",
                extra={**state.summary(), "seconds": round(time.monotonic() - started, 1)},
            )
            await emit("done", state.status.value, **state.summary())
        return state

    # --- stages ---

    async def _understand(self, state: AgentState, context: str | None) -> None:
        analysis = await analyse_request(
            self.llm, state.request, usage=state.usage, context=context
        )
        state.analysis = analysis
        state.intent = analysis.intent
        state.requirements = to_requirements(analysis, state.request)
        state.profile = merge_profile(state.profile, analysis)
        state.links = find_links(state.request)
        actionable = bool(
            state.links
            or state.carried_listing_ids
            or analysis.product_mentions
            or state.requirements.category
            or analysis.pasted_offer_text
        )
        if analysis.needs_clarification and not actionable:
            state.status = RunStatus.NEEDS_CLARIFICATION
            state.clarification_question = analysis.clarification_question or (
                "What kind of product are you shopping for, and roughly what budget?"
            )

    async def _carry_over(self, state: AgentState, listing_ids: list[str]) -> None:
        """Products from the earlier answer join this run, so "which of those…" has subjects."""
        if not listing_ids:
            return
        async with self.base_ctx.sessions() as session:
            catalog = CatalogRepository(session)
            for lid in listing_ids:
                row = await catalog.get_listing(lid)
                if row is not None:
                    state.listings[lid] = summarize(row, self.base_ctx)
                    state.carried_listing_ids.append(lid)

    async def _resolve_links(self, state: AgentState, ctx: ToolContext, emit: Any) -> None:
        for link in state.links[:3]:
            await self._call_tool(
                state,
                ctx,
                ToolCall(
                    id=f"link-{len(state.tool_calls)}",
                    name="resolve_product_link",
                    arguments={"url": link},
                ),
                emit,
            )

    async def _pasted_offers(self, state: AgentState, ctx: ToolContext, emit: Any) -> None:
        text = state.analysis.pasted_offer_text if state.analysis else None
        if not text:
            return
        platform = None
        for link in state.links:
            try:
                platform = parse_product_url(link).marketplace
                break
            except UrlParseError:
                continue
        try:
            offers, warnings = await parse_offer_text(
                self.llm, text, platform=platform, usage=state.usage
            )
        except (ValidationFailedError, LLMUnavailableError) as exc:
            state.warnings.append(f"could not read the pasted offer: {exc}")
            return
        state.warnings.extend(warnings)
        if len(state.linked_listing_ids) != 1:
            state.warnings.append(
                "the pasted offer was read but not applied: include the product link so it can be "
                "tied to the right listing"
            )
            return
        listing_id = state.linked_listing_ids[0]
        async with ctx.sessions() as session, session.begin():
            repo = OfferRepository(session)
            for offer in offers:
                await repo.upsert(offer, source="user", now=self.clock(), listing_id=listing_id)
        await emit("stage", f"Applied {len(offers)} pasted offer(s) to the linked listing")

    async def _plan(self, state: AgentState) -> None:
        assert state.analysis is not None
        try:
            state.plan = await plan_research(
                self.llm,
                state.analysis,
                [s.name for s in self.tools.specs()],
                usage=state.usage,
            )
        except ValidationFailedError as exc:
            state.warnings.append(f"planning skipped: {exc}")

    async def _research(
        self, state: AgentState, ctx: ToolContext, emit: Any, started: float
    ) -> None:
        assert state.analysis is not None
        plan = (
            "\n".join(f"- {s.tool}: {s.goal}" for s in (state.plan.steps if state.plan else []))
            or "(none)"
        )
        profile = {
            "cards": [m.describe() for m in state.profile.payment_methods] or "unknown",
            "memberships": sorted(state.profile.memberships) or "unknown",
            "prefers_emi": state.profile.prefers_emi,
        }
        research = load_prompt("agent/research").render(
            max_tool_calls=str(self.budget.max_tool_calls),
            analysis=state.analysis.model_dump_json(exclude_none=True),
            plan=plan,
            profile=json.dumps(profile),
        )
        user = state.request
        if state.context:
            user = f"Earlier conversation:\n{state.context}\n\nFollow-up: {state.request}"
        if state.carried_listing_ids:
            earlier = [
                {"listing_id": lid, "title": state.listings[lid].title}
                for lid in state.carried_listing_ids
            ]
            user += "\n\nProducts from the earlier answer: " + json.dumps(
                earlier, ensure_ascii=False
            )
        if state.linked_listing_ids:
            linked = [
                {"listing_id": lid, "title": state.listings[lid].title}
                for lid in state.linked_listing_ids
                if lid in state.listings
            ]
            user += "\n\nAlready resolved product links: " + json.dumps(linked, ensure_ascii=False)
        messages = [Message.system(system_prompt() + "\n\n" + research), Message.user(user)]
        specs = [
            ToolDef(name=s.name, description=s.description, parameters=s.parameters)
            for s in self.tools.specs()
        ]
        calls_used = 0
        for _ in range(self.budget.max_iterations):
            if time.monotonic() - started > self.budget.max_seconds:
                state.uncertainties.append(
                    "Research stopped at the time limit; some checks may be missing."
                )
                return
            response = await self.llm.complete(
                LLMRole.RESEARCH, messages, tools=specs, usage=state.usage
            )
            state.research_iterations += 1
            messages.append(response.assistant_message)
            if not response.tool_calls:
                return
            remaining = self.budget.max_tool_calls - calls_used
            to_run, skipped = (
                response.tool_calls[: max(remaining, 0)],
                response.tool_calls[max(remaining, 0) :],
            )
            results = await self._call_tools(state, ctx, to_run, emit)
            for call, (content, is_error) in zip(to_run, results, strict=True):
                messages.append(Message.tool_result(call, content, is_error=is_error))
            for call in skipped:
                messages.append(
                    Message.tool_result(
                        call, "Not run: the research budget is used up.", is_error=True
                    )
                )
            calls_used += len(to_run)
            if calls_used >= self.budget.max_tool_calls:
                state.uncertainties.append(
                    "Research stopped at the tool-call budget; some checks may be missing."
                )
                return
        state.uncertainties.append(
            "Research stopped at the iteration budget; some checks may be missing."
        )

    async def _call_tool(
        self, state: AgentState, ctx: ToolContext, call: ToolCall, emit: Any
    ) -> tuple[str, bool]:
        return (await self._call_tools(state, ctx, [call], emit))[0]

    async def _call_tools(
        self, state: AgentState, ctx: ToolContext, calls: list[ToolCall], emit: Any
    ) -> list[tuple[str, bool]]:
        """Run calls concurrently; record and ingest results in the order they were issued."""

        async def execute(call: ToolCall) -> tuple[ToolResult[Any], Any] | None:
            if call.argument_error:
                return None
            await emit("tool_call", call.name, args=call.arguments)
            return await self.tools.execute(call.name, call.arguments, ctx)

        outcomes = await asyncio.gather(*(execute(c) for c in calls))
        replies: list[tuple[str, bool]] = []
        for call, outcome in zip(calls, outcomes, strict=True):
            if outcome is None:
                replies.append((f"Invalid arguments: {call.argument_error}", True))
                continue
            result, record = outcome
            state.tool_calls.append(record)
            self._ingest(state, call.arguments, result)
            await emit("tool_result", call.name, status=record.status, latency_ms=record.latency_ms)
            replies.append(
                (payload_for_model(result, self.max_tool_chars), record.status in FAILED_TOOL)
            )
        return replies

    def _ingest(self, state: AgentState, args: dict[str, Any], result: ToolResult[Any]) -> None:
        for e in result.evidence:
            state.evidence[e.id] = e
        state.warnings.extend(result.errors)
        data = result.data
        if isinstance(data, SearchProductsOutput):
            for c in data.candidates:
                state.candidates[c.key] = c
                state.listings.update({s.listing_id: s for s in c.listings})
        elif isinstance(data, ResolveLinkOutput):
            state.link_results.append(
                {
                    "link": data.parsed.canonical_url,
                    "status": data.status,
                    "message": data.message,
                }
            )
            for s in data.listings:
                state.listings[s.listing_id] = s
                if s.listing_id not in state.linked_listing_ids:
                    state.linked_listing_ids.append(s.listing_id)
            for c in data.candidates:
                state.candidates[c.key] = c
                state.listings.update({s.listing_id: s for s in c.listings})
        elif isinstance(data, ProductDetailsOutput):
            key = str(args.get("listing_id") or args.get("name") or data.name)
            state.details[key] = data
            state.conflicts.extend(data.conflicts)
        elif isinstance(data, GetListingsOutput):
            state.seller_comparisons[str(args.get("listing_id"))] = data
            for g in data.matching:
                state.listings[g.listing.listing_id] = g.listing
            for r in data.rejected:
                state.listings[r.listing.listing_id] = r.listing
        elif isinstance(data, FindOffersOutput):
            state.offers[data.listing.listing_id] = data
            state.listings[data.listing.listing_id] = data.listing
        elif isinstance(data, PriceHistoryOutput):
            for h in data.listings:
                state.price_history[h.listing.listing_id] = h
        elif isinstance(data, ResearchReviewsOutput):
            state.reviews[data.subject] = data
        elif isinstance(data, SaleEventsOutput):
            state.sale_events = data
        elif isinstance(data, FindAlternativesOutput):
            state.alternatives.append(data)
            state.listings[data.reference.listing_id] = data.reference
            for alt in data.alternatives:
                state.candidates[alt.candidate.key] = alt.candidate
                state.listings.update({s.listing_id: s for s in alt.candidate.listings})
        elif isinstance(data, SearchWebOutput):
            state.web_results.extend(data.results)

    async def _analyse(self, state: AgentState, ctx: ToolContext, emit: Any) -> None:
        """Deterministic: enrich the top candidates with offers and history, then rank them."""
        now = self.clock()
        state.examined_listing_ids = list(
            dict.fromkeys(
                [
                    *state.linked_listing_ids,
                    *state.carried_listing_ids,
                    *state.details,
                    *state.seller_comparisons,
                    *state.offers,
                    *state.price_history,
                ]
            )
        )
        if state.sale_events is None:
            await self._call_tool(
                state, ctx, ToolCall(id="enrich-sales", name="get_sale_events", arguments={}), emit
            )
        preliminary = rank_subjects(state, subjects(state), now)
        if preliminary and not self._compared(state, preliminary[0].listing.listing_id):
            top = preliminary[0].listing.listing_id
            await self._call_tool(
                state,
                ctx,
                ToolCall(
                    id=f"enrich-sellers-{top}", name="get_listings", arguments={"listing_id": top}
                ),
                emit,
            )
            preliminary = rank_subjects(state, subjects(state), now)
        # Offers for several sellers of each shortlisted variant: the cheapest listed price is
        # not always the cheapest for this user's cards. (find_offers reads stored data only.)
        offer_jobs: list[ToolCall] = []
        for item in preliminary[: self.top_k]:
            cand = state.candidates.get(item.candidate_key or "")
            pool = seller_pool(cand, state)[:SELLERS_PER_PRODUCT] if cand else []
            for lid in dict.fromkeys([item.listing.listing_id, *(s.listing_id for s in pool)]):
                if lid not in state.offers:
                    offer_jobs.append(
                        ToolCall(
                            id=f"enrich-offers-{lid}",
                            name="find_offers",
                            arguments={"listing_id": lid},
                        )
                    )
        await self._call_tools(state, ctx, offer_jobs, emit)
        ranked = rank_subjects(state, subjects(state), now)
        history_jobs = [
            ToolCall(
                id=f"enrich-history-{item.listing.listing_id}",
                name="get_price_history",
                arguments={"listing_id": item.listing.listing_id},
            )
            for item in ranked[: self.top_k]
            if item.listing.listing_id not in state.price_history
        ]
        await self._call_tools(state, ctx, history_jobs, emit)
        state.focus = rank_subjects(state, subjects(state), now)[: max(self.top_k, 5)]
        if not state.focus:
            state.uncertainties.append("No product matched the request with the available data.")
            return
        top_item = state.focus[0]
        if state.intent in REVIEW_INTENTS and not any(
            title_similarity(r.subject, top_item.listing.title) >= 0.3
            for r in state.reviews.values()
        ):
            await self._call_tool(
                state,
                ctx,
                ToolCall(
                    id="enrich-reviews",
                    name="research_reviews",
                    arguments={
                        "listing_id": top_item.listing.listing_id,
                        "use_case": ", ".join(state.requirements.use_cases[:2])
                        if state.requirements and state.requirements.use_cases
                        else None,
                    },
                ),
                emit,
            )

    @staticmethod
    def _compared(state: AgentState, listing_id: str) -> bool:
        return listing_id in state.seller_comparisons or any(
            listing_id in {g.listing.listing_id for g in c.matching}
            for c in state.seller_comparisons.values()
        )

    async def _synthesise(self, state: AgentState, emit: Any) -> None:
        facts_text = facts_json(build_facts(state))
        prompt = load_prompt("agent/synthesis").render(facts=facts_text)
        try:
            answer = await generate_structured(
                self.llm,
                LLMRole.SYNTHESIS,
                [Message.system(system_prompt()), Message.user(prompt)],
                FinalAnswer,
                usage=state.usage,
            )
        except (ValidationFailedError, LLMUnavailableError) as exc:
            state.warnings.append(f"summary generation failed ({exc}); showing computed results")
            await emit("warning", "Showing computed results without a written summary")
            answer = fallback_answer(state)
        state.final, state.grounding = ground_answer(state, answer, facts_text=facts_text)
        if state.grounding.ungrounded:
            await emit(
                "warning", f"{len(state.grounding.ungrounded)} figure(s) could not be verified"
            )

    def _propose_actions(self, state: AgentState) -> None:
        if state.final is None:
            return
        for s in state.final.suggested_actions:
            if s.action != "save_preference" and s.listing_id not in state.listings:
                continue
            state.proposed_actions.append(
                ProposedAction(
                    action=s.action,
                    listing_id=s.listing_id,
                    target_price=Decimal(str(s.target_price)) if s.target_price else None,
                    description=s.detail,
                )
            )
