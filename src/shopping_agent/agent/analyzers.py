"""LLM-backed analysis steps and their conversion into validated domain types."""

import json
import logging
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError

from shopping_agent.agent.schemas import (
    ExtractedOffer,
    OfferExtraction,
    RequestAnalysis,
    ResearchPlan,
    ReviewAnalysis,
)
from shopping_agent.domain.common import Verification, money
from shopping_agent.domain.evidence import Evidence
from shopping_agent.domain.offer import (
    BenefitTiming,
    CardType,
    ExchangeTerms,
    Offer,
    OfferType,
    PaymentMethod,
    PaymentRequirement,
    StackingTerms,
    UserProfile,
)
from shopping_agent.domain.product import Marketplace
from shopping_agent.domain.requirements import Budget, Constraint, Requirements
from shopping_agent.domain.review import ReviewCoverage, ReviewFindings, ReviewTheme
from shopping_agent.llm.client import LLMClient, LLMRole, RunUsage
from shopping_agent.llm.structured import generate_structured
from shopping_agent.llm.types import Message
from shopping_agent.prompts import load_prompt
from shopping_agent.security.untrusted import wrap_untrusted
from shopping_agent.services.india_price_parser import parse_budget
from shopping_agent.tools.web_tools import ResearchReviewsOutput

log = logging.getLogger(__name__)

URL_PATTERN = re.compile(
    r"https?://[^\s<>\"')\]]+|(?:www\.)?(?:amazon\.in|flipkart\.com|amzn\.in|fkrt\.it)/[^\s<>\"')\]]+",
    re.I,
)


def find_links(text: str) -> list[str]:
    return list(dict.fromkeys(m.group(0).rstrip(".,;") for m in URL_PATTERN.finditer(text)))


def system_prompt() -> str:
    return load_prompt("agent/system").template


# --- request analysis ---


_QUOTED = re.compile(r"'[^']{6,}'|\"[^\"]{6,}\"|“[^”]{6,}”|‘[^’]{6,}’")


def budget_text(request: str, pasted_offer_text: str | None = None) -> str:
    """The user's own words: without links, quoted passages or pasted offer terms."""
    text = URL_PATTERN.sub(" ", request)
    if pasted_offer_text:
        text = text.replace(pasted_offer_text, " ")
    return _QUOTED.sub(" ", text)


async def analyse_request(
    llm: LLMClient, request: str, *, usage: RunUsage, context: str | None = None
) -> RequestAnalysis:
    budget = parse_budget(budget_text(request))
    hints = {
        "possible_budget": budget.model_dump(mode="json", exclude_none=True) or None,
        "note": "possible_budget is parsed from the user's words; ignore it if those amounts "
        "belong to an offer, a price quote or a past purchase rather than a budget",
        "links": find_links(request),
    }
    prompt = load_prompt("requirements/extraction").render(hints=json.dumps(hints))
    user = (
        request if not context else f"Earlier conversation:\n{context}\n\nNew message:\n{request}"
    )
    return await generate_structured(
        llm,
        LLMRole.EXTRACTION,
        [Message.system(system_prompt() + "\n\n" + prompt), Message.user(user)],
        RequestAnalysis,
        usage=usage,
    )


def _dec(value: float | None) -> Decimal | None:
    if value is None or value <= 0:
        return None
    try:
        return money(Decimal(str(value)))
    except InvalidOperation:
        return None


def to_requirements(analysis: RequestAnalysis, request: str) -> Requirements:
    """LLM analysis → domain requirements.

    The model decides whether the user stated a budget at all; when it did, the deterministic
    parse of the user's own words supplies the exact figures.
    """
    llm_budget = Budget(
        minimum=_dec(analysis.budget.minimum),
        maximum=_dec(analysis.budget.maximum),
        target=_dec(analysis.budget.target),
        approximate=analysis.budget.approximate,
    )
    has_budget = any(
        v is not None for v in (llm_budget.minimum, llm_budget.maximum, llm_budget.target)
    )
    target = _dec(analysis.target_price)
    if target is not None and has_budget:
        # "Alert me below 60k" is a target for a product, not a budget for a search.
        amounts = [v for v in (llm_budget.minimum, llm_budget.maximum, llm_budget.target) if v]
        if all(abs(v - target) <= target / 100 for v in amounts):
            has_budget = False
    parsed = parse_budget(budget_text(request, analysis.pasted_offer_text))
    parsed_any = any(v is not None for v in (parsed.minimum, parsed.maximum, parsed.target))
    budget = parsed if has_budget and parsed_any else llm_budget if has_budget else Budget()

    def convert(items: list[object]) -> list[Constraint]:
        out = []
        for item in items:
            try:
                out.append(Constraint.model_validate(item, from_attributes=True))
            except ValidationError:
                continue
        return out

    category = analysis.category.lower().strip() if analysis.category else None
    return Requirements(
        category=category or None,
        budget=budget,
        use_cases=analysis.use_cases,
        hard_constraints=convert(list(analysis.hard_constraints)),
        strong_preferences=convert(list(analysis.strong_preferences)),
        nice_to_have=convert(list(analysis.nice_to_have)),
        unknowns=analysis.unknowns,
    )


def merge_profile(base: UserProfile, analysis: RequestAnalysis) -> UserProfile:
    """Session-level profile: saved preferences plus what the user said in this request."""
    methods = list(base.payment_methods)
    for m in analysis.payment_methods:
        if not m.issuer.strip():
            continue
        candidate = PaymentMethod(issuer=m.issuer.strip(), card_type=m.card_type)
        if candidate.card_type is CardType.ANY:
            continue  # card type unknown: offers needing it stay conditional
        if all(
            (p.issuer.lower(), p.card_type) != (candidate.issuer.lower(), candidate.card_type)
            for p in methods
        ):
            methods.append(candidate)
    return base.model_copy(
        update={
            "payment_methods": methods,
            "memberships": base.memberships
            | {m.strip().lower() for m in analysis.memberships if m.strip()},
            "prefers_emi": analysis.prefers_emi
            if analysis.prefers_emi is not None
            else base.prefers_emi,
        }
    )


# --- planning ---


async def plan_research(
    llm: LLMClient, analysis: RequestAnalysis, tool_names: list[str], *, usage: RunUsage
) -> ResearchPlan:
    prompt = load_prompt("agent/planning").render(
        analysis=analysis.model_dump_json(exclude_none=True), tools=", ".join(tool_names)
    )
    plan = await generate_structured(
        llm,
        LLMRole.RESEARCH,
        [Message.system(system_prompt()), Message.user(prompt)],
        ResearchPlan,
        usage=usage,
    )
    plan.steps = [s for s in plan.steps if s.tool in tool_names][:8]
    return plan


# --- review analysis ---


class ReviewAnalyzer:
    """Turns collected review sources into themes; bound to one run's usage tracker."""

    def __init__(self, llm: LLMClient, usage: RunUsage) -> None:
        self._llm = llm
        self._usage = usage

    async def __call__(
        self, output: ResearchReviewsOutput, evidence: list[Evidence], use_case: str | None
    ) -> ReviewFindings | None:
        if not output.sources:
            return None
        sources = "\n".join(
            f"- {s.title} ({s.url}, published {s.published_at:%Y-%m-%d})\n{s.content}"
            if s.published_at
            else f"- {s.title} ({s.url})\n{s.content}"
            for s in output.sources
        )
        ratings = (
            "\n".join(
                f"- {r.marketplace}: {r.rating}/5 from "
                f"{r.rating_count or 'an unknown number of'} ratings"
                for r in output.ratings
            )
            or "none"
        )
        prompt = load_prompt("reviews/analysis").render(
            subject=output.subject,
            use_case_line=f" Focus also on the use case: {use_case}." if use_case else "",
            ratings=ratings,
            sources=sources,
        )
        try:
            analysis = await generate_structured(
                self._llm,
                LLMRole.ANALYSIS,
                [Message.system(system_prompt()), Message.user(prompt)],
                ReviewAnalysis,
                usage=self._usage,
            )
        except Exception as exc:  # analysis is optional; research continues without it
            log.warning("review analysis failed: %s", exc)
            return None
        by_url = {e.url: e.id for e in evidence if e.url}
        known_urls = {s.url for s in output.sources}
        themes = []
        for t in analysis.themes[:8]:
            urls = [u for u in t.source_urls if u in known_urls]
            if not urls:
                continue  # unsupported theme
            themes.append(
                ReviewTheme(
                    kind=t.kind,
                    theme=t.theme,
                    mentions=min(len(urls), len(output.sources)),
                    sample_size=len(output.sources),
                    use_case=t.use_case,
                    evidence_ids=[by_url[u] for u in urls if u in by_url],
                )
            )
        dates = [s.published_at.date() for s in output.sources if s.published_at]
        return ReviewFindings(
            variant_id=output.subject[:64],
            themes=themes,
            reviews_analysed=len(output.sources),
            sources=[s.url for s in output.sources],
            earliest=min(dates) if dates else None,
            latest=max(dates) if dates else None,
            coverage=output.coverage if themes else ReviewCoverage.LIMITED,
        )


# --- offers from text ---


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def to_offer(
    item: ExtractedOffer, *, platform: Marketplace | None, verification: Verification
) -> Offer | None:
    try:
        payment = None
        if item.issuer:
            payment = PaymentRequirement(
                issuer=item.issuer, card_type=item.card_type or CardType.ANY, network=item.network
            )
        exchange = None
        if item.type is OfferType.EXCHANGE:
            maximum = _dec(item.exchange_max_value)
            if maximum is None:
                return None
            exchange = ExchangeTerms(
                max_value=maximum,
                min_value=_dec(item.exchange_min_value),
                bonus=_dec(item.exchange_bonus) or Decimal(0),
            )
        percent = _dec(item.percent)
        flat = None if percent is not None else _dec(item.flat_amount)
        # A percentage offer without a stated cap is incomplete: keep it but unverified.
        complete = not (percent is not None and _dec(item.max_discount) is None)
        return Offer(
            type=item.type,
            title=item.title[:200],
            platform=platform,
            provider=item.provider,
            flat_amount=flat,
            percent=percent,
            max_discount=_dec(item.max_discount),
            min_order_value=_dec(item.min_order_value),
            payment=payment,
            emi_rule=item.emi_rule,
            membership_required=item.membership_required,
            exchange=exchange,
            stacking=StackingTerms(
                exclusive=item.exclusive, combinable_with=frozenset(item.combinable_with)
            ),
            timing=BenefitTiming.DEFERRED if item.cashback_deferred else BenefitTiming.INSTANT,
            valid_from=_date(item.valid_from),
            valid_to=_date(item.valid_to),
            verification=verification if complete else Verification.UNVERIFIED,
        )
    except ValidationError:
        return None


async def parse_offer_text(
    llm: LLMClient,
    text: str,
    *,
    platform: Marketplace | None,
    usage: RunUsage,
    verification: Verification = Verification.USER_PROVIDED,
) -> tuple[list[Offer], list[str]]:
    prompt = load_prompt("deals/offer_extraction").render(
        platform=platform.value if platform else "unknown",
        text=wrap_untrusted(text[:4000], source="user-pasted offer text"),
    )
    extraction = await generate_structured(
        llm,
        LLMRole.EXTRACTION,
        [Message.system(system_prompt()), Message.user(prompt)],
        OfferExtraction,
        usage=usage,
    )
    offers: list[Offer] = []
    warnings: list[str] = []
    for item in extraction.offers:
        offer = to_offer(item, platform=platform, verification=verification)
        if offer is None:
            warnings.append(f"could not use offer '{item.title}': terms incomplete")
            continue
        offer = offer.model_copy(update={"terms_text": text[:1000]})
        if offer.verification is Verification.UNVERIFIED:
            warnings.append(f"'{offer.title}' has no stated cap; treated as unverified")
        offers.append(offer)
    return offers, warnings
