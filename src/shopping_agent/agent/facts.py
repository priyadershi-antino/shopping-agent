"""Deterministic analysis of collected evidence, the facts pack for synthesis, the grounding
check and the fallback answer. No LLM calls here."""

import json
import re
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from shopping_agent.agent.schemas import FinalAnswer, Recommendation
from shopping_agent.agent.state import AgentState, FocusItem
from shopping_agent.domain.offer import PriceScenario
from shopping_agent.domain.product import Availability, Marketplace
from shopping_agent.domain.requirements import Requirements
from shopping_agent.services.formatting import format_inr
from shopping_agent.services.grounding import GroundingReport, check_grounding, mark_ungrounded
from shopping_agent.services.requirement_match import (
    CandidateRow,
    FactValue,
    Level,
    MatchStatus,
    match_requirements,
    order_candidates,
)
from shopping_agent.services.sale_mode import sale_status
from shopping_agent.services.timing import analyze_timing
from shopping_agent.services.variant_gate import NON_NEW, suspiciously_cheap
from shopping_agent.tools.common import Candidate, ListingSummary

UNVERIFIED_NOTE = "Figures marked [unverified] could not be traced to the collected data."


# --- choosing what to analyse ---


def seller_pool(candidate: Candidate, state: AgentState) -> list[ListingSummary]:
    """New, available listings of the candidate's variant (from the seller comparison if one
    was done), cheapest listed price first."""
    pool = list(candidate.listings)
    for ref_id, comparison in state.seller_comparisons.items():
        if any(s.listing_id == ref_id for s in candidate.listings):
            pool = [g.listing for g in comparison.matching]
            break
    usable = [
        s
        for s in pool
        if s.condition not in NON_NEW and s.availability is not Availability.OUT_OF_STOCK
    ]
    return sorted(usable, key=lambda s: s.price)


def representative_listing(candidate: Candidate, state: AgentState) -> ListingSummary | None:
    """The seller where this user pays least: lowest best price without extra conditions
    (offers checked against their cards), falling back to the listed price."""
    pool = seller_pool(candidate, state)
    return min(pool, key=lambda s: (best_prices(s, state)[0], s.price)) if pool else None


def subjects(state: AgentState) -> list[tuple[Candidate | None, ListingSummary]]:
    out: list[tuple[Candidate | None, ListingSummary]] = []
    covered: set[str] = set()
    for cand in state.candidates.values():
        rep = representative_listing(cand, state)
        if rep is not None:
            out.append((cand, rep))
            covered.update(s.listing_id for s in cand.listings)
    # Listings the user linked (or that were analysed directly) but aren't part of a candidate.
    for listing_id in [
        *state.linked_listing_ids,
        *state.carried_listing_ids,
        *state.offers,
        *state.seller_comparisons,
    ]:
        if listing_id not in covered and listing_id in state.listings:
            out.append((None, state.listings[listing_id]))
            covered.add(listing_id)
    return out


def _facts(
    cand: Candidate | None, listing: ListingSummary, state: AgentState
) -> dict[str, FactValue]:
    facts: dict[str, FactValue] = {
        k: (v.value if hasattr(v, "value") else v)
        for k, v in listing.attributes.model_dump(exclude_none=True).items()
        if k != "extra"
    }
    if cand is not None:
        for k, v in cand.attributes.model_dump(exclude_none=True).items():
            if k != "extra":
                facts.setdefault(k, v.value if hasattr(v, "value") else v)
        if cand.brand:
            facts["brand"] = cand.brand
        if cand.category:
            facts["category"] = cand.category
        listing_ids = [s.listing_id for s in cand.listings]
    else:
        listing_ids = [listing.listing_id]
    for lid in listing_ids:
        details = state.details.get(lid)
        if details:
            for k, v in details.specs.items():
                facts.setdefault(k, v)
            if details.brand:
                facts.setdefault("brand", details.brand)
    facts.setdefault("condition", listing.condition.value)
    # Requirements that name the product ("model = Nimbus 15") are matched against its title.
    model_number = cand.model_number if cand else None
    for key in ("model", "name", "product", "title"):
        facts.setdefault(key, f"{listing.title} {model_number or ''}".strip())
    return facts


def best_prices(listing: ListingSummary, state: AgentState) -> tuple[Decimal, Decimal | None]:
    offers = state.offers.get(listing.listing_id)
    if offers is None:
        return listing.price, None
    unconditional = [s for s in offers.scenarios if not s.conditional]
    conditional = [s for s in offers.scenarios if s.conditional]
    eligible = min((s.net_after_cashback for s in unconditional), default=listing.price)
    cond = min((s.upfront_conservative for s in conditional), default=None)
    return eligible, cond


def rank_subjects(
    state: AgentState, subjects_: list[tuple[Candidate | None, ListingSummary]], now: datetime
) -> list[FocusItem]:
    req = state.requirements or Requirements()
    rows: list[CandidateRow] = []
    by_id: dict[str, tuple[Candidate | None, ListingSummary, Decimal, Decimal | None]] = {}
    prices = {listing.listing_id: best_prices(listing, state)[0] for _, listing in subjects_}
    suspects = suspiciously_cheap(prices)
    for cand, listing in subjects_:
        eligible, conditional = best_prices(listing, state)
        checks = match_requirements(
            req,
            _facts(cand, listing, state),
            listing_price=listing.price,
            best_eligible=eligible,
            best_conditional=conditional,
        )
        rows.append(
            CandidateRow(
                variant_id=listing.listing_id,
                best_eligible=eligible,
                checks=checks,
                price_suspect=listing.listing_id in suspects,
            )
        )
        by_id[listing.listing_id] = (cand, listing, eligible, conditional)

    events = [v.event for v in state.sale_events.events] if state.sale_events else []
    target = (
        Decimal(str(state.analysis.target_price))
        if state.analysis and state.analysis.target_price
        else None
    )
    examined = set(state.examined_listing_ids)
    for cand, listing in subjects_:
        if cand is not None and examined & {s.listing_id for s in cand.listings}:
            examined.add(listing.listing_id)
    focus: list[FocusItem] = []
    for row in order_candidates(rows, examined=examined):
        cand, listing, eligible, conditional = by_id[row.variant_id]
        history = state.price_history.get(listing.listing_id)
        timing = None
        if history is not None or target is not None or events:
            timing = analyze_timing(
                current_effective=eligible,
                target=target,
                baseline=history.baseline if history else None,
                sale=sale_status(events, now, platform=Marketplace(listing.marketplace))
                if events
                else None,
                now=now,
                availability=listing.availability,
                pre_sale_hike=history.pre_sale_hike if history else None,
            )
        focus.append(
            FocusItem(
                listing=listing,
                candidate_key=cand.key if cand else None,
                checks=row.checks,
                best_eligible=eligible,
                best_conditional=conditional,
                timing=timing,
                price_suspect=row.price_suspect,
            )
        )
    return focus


# --- facts pack ---


def _scenario_fact(s: PriceScenario) -> dict[str, Any]:
    fact: dict[str, Any] = {
        "label": s.label,
        "name": s.name,
        "pay_at_checkout": format_inr(s.upfront_conservative),
        "offers_used": [a.title for a in s.applied],
        "conditional": s.conditional,
        "conditions": s.conditions,
        "verification": s.verification.value,
    }
    if s.is_range:
        fact["best_case_at_checkout"] = format_inr(s.upfront_best)
    if s.deferred_cashback:
        fact["cashback_later"] = format_inr(s.deferred_cashback)
        fact["net_after_cashback"] = format_inr(s.net_after_cashback)
    if s.base_price > s.net_after_cashback:
        fact["saving_vs_standard"] = format_inr(s.base_price - s.net_after_cashback)
    if s.stale:
        fact["stale"] = True
    return fact


def build_facts(state: AgentState, *, max_items: int = 5, max_evidence: int = 60) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    relevant_subjects: set[str] = set()
    for item in state.focus[:max_items]:
        listing = item.listing
        relevant_subjects.add(listing.listing_id)
        entry: dict[str, Any] = {
            "listing_id": listing.listing_id,
            "product": listing.title,
            "marketplace": listing.marketplace.value,
            "seller": listing.seller,
            "listed_price": format_inr(listing.price),
            "availability": listing.availability.value,
            "condition": listing.condition.value,
            "price_observed_at": listing.observed_at.isoformat(timespec="minutes"),
            "price_stale": listing.stale,
            "attributes": listing.attributes.model_dump(mode="json", exclude_none=True),
            "requirement_checks": [
                {k: v for k, v in c.model_dump(mode="json").items() if v is not None}
                for c in item.checks
            ],
            "best_unconditional_price": format_inr(item.best_eligible),
        }
        if listing.mrp:
            entry["mrp"] = format_inr(listing.mrp)
        if listing.rating is not None:
            entry["rating"] = f"{listing.rating}/5 ({listing.rating_count or '?'} ratings)"
        offers = state.offers.get(listing.listing_id)
        if offers:
            entry["offer_scenarios"] = [_scenario_fact(s) for s in offers.scenarios]
            if offers.card_matrix:
                entry["by_payment_method"] = {
                    row.payment: format_inr(row.best.net_after_cashback)
                    for row in offers.card_matrix
                }
            usable = [v for v in offers.offers if v.eligibility.status.value != "ineligible"]
            if usable:
                entry["offers_for_you"] = [
                    {
                        "offer": v.offer.title,
                        "status": v.eligibility.status.value,
                        **(
                            {"conditions": v.eligibility.conditions}
                            if v.eligibility.conditions
                            else {}
                        ),
                        **({"paid_later": True} if v.offer.timing.value == "deferred" else {}),
                    }
                    for v in usable
                ]
            titles = {v.offer.id: v.offer.title for v in offers.offers}
            not_combined = [
                f"{titles.get(p.a, p.a)} + {titles.get(p.b, p.b)}: {p.reason}"
                for p in offers.stacking
                if p.status.value != "combinable" and p.a in titles and p.b in titles
            ]
            if not_combined:
                entry["offers_not_combined"] = not_combined[:6]
            if offers.excluded:
                entry["offers_not_applicable"] = [
                    f"{e.title}: {'; '.join(e.reasons)}" for e in offers.excluded
                ]
            if offers.unstructured_offer_texts:
                entry["unparsed_offer_text"] = offers.unstructured_offer_texts[:3]
            if offers.notes:
                entry["offer_notes"] = offers.notes
        history = state.price_history.get(listing.listing_id)
        if history and history.baseline:
            b = history.baseline
            entry["price_history"] = {
                "coverage_days": b.coverage_days,
                "coverage": b.tier.value,
                "observed_low": format_inr(b.minimum),
                "observed_high": format_inr(b.maximum),
                "observed_average": format_inr(b.average),
                "trend": b.trend.value,
                "note": history.low_note,
            }
            if history.pre_sale_hike and history.pre_sale_hike.detected:
                entry["price_history"]["pre_sale_hike"] = history.pre_sale_hike.reason
        elif history:
            entry["price_history"] = {"note": "no observations yet"}
        if item.timing:
            entry["timing"] = {
                "advice": item.timing.advice.value,
                "summary": item.timing.summary,
                "signals": [s.detail for s in item.timing.signals],
            }
        comparison = state.seller_comparisons.get(listing.listing_id)
        if comparison:
            entry["other_sellers_same_variant"] = [
                {
                    "listing_id": g.listing.listing_id,
                    "marketplace": g.listing.marketplace.value,
                    "seller": g.listing.seller,
                    "price": format_inr(g.listing.price),
                    "unverified_attributes": g.unverified,
                    "risk_flags": g.risk_flags,
                }
                for g in comparison.matching
                if g.listing.listing_id != listing.listing_id
            ]
            entry["rejected_listings"] = [
                {
                    "product": r.listing.title,
                    "price": format_inr(r.listing.price),
                    "reasons": r.reasons,
                }
                for r in comparison.rejected
            ]
        if item.price_suspect:
            entry["price_warning"] = (
                "priced far below the other shortlisted items: verify the seller, condition and "
                "variant before trusting this price; do not present it as a top pick or bargain"
            )
        if item.candidate_key and item.candidate_key in state.candidates:
            cand = state.candidates[item.candidate_key]
            if cand.risk_flags:
                entry["risk_flags"] = cand.risk_flags
        details = state.details.get(listing.listing_id)
        if details:
            entry["specs"] = details.specs
            entry["spec_source"] = f"{details.source_name} ({details.verification.value})"
            if details.conflicts:
                entry["spec_conflicts"] = [c.explanation for c in details.conflicts]
        items.append(entry)

    reviews = []
    for review in state.reviews.values():
        r: dict[str, Any] = {
            "product": review.subject,
            "coverage": review.coverage.value,
            "ratings": [
                f"{x.marketplace}: {x.rating}/5 ({x.rating_count or '?'})" for x in review.ratings
            ],
            "note": review.note.split(" Content inside")[0],
        }
        if review.findings:
            r["themes"] = [
                {
                    "kind": t.kind.value,
                    "theme": t.theme,
                    "sources_mentioning": f"{t.mentions} of {t.sample_size}",
                    "evidence_ids": t.evidence_ids,
                }
                for t in review.findings.themes
            ]
        else:
            r["source_titles"] = [s.title for s in review.sources]
        reviews.append(r)

    evidence = sorted(
        state.evidence.values(),
        key=lambda e: (e.subject not in relevant_subjects, e.source_type.value == "search_result"),
    )[:max_evidence]
    facts: dict[str, Any] = {
        "data_mode": "DEMO data (fictional catalog)" if state.demo_data else "live data",
        "request": state.request,
        "requirements": (state.requirements or Requirements()).model_dump(
            mode="json", exclude_none=True, exclude_defaults=True
        ),
        "user_payment_methods": [m.describe() for m in state.profile.payment_methods],
        "candidates_in_order": items,
        "reviews": reviews,
        "evidence": [
            {
                "id": e.id,
                "claim": e.claim,
                "source": e.source_name,
                "verification": e.verification.value,
            }
            for e in evidence
        ],
    }
    if state.platforms:
        facts["platforms_searched"] = state.platforms
    if state.context:
        facts["earlier_conversation"] = state.context
    if state.link_results:
        facts["product_links"] = state.link_results
    if state.sale_events and state.sale_events.events:
        facts["sale_events"] = [
            {
                "name": v.event.name,
                "platform": v.event.platform.value,
                "dates": f"{v.event.start:%d %b %Y} – {v.event.end:%d %b %Y}",
                "state_now": v.state_now.value,
                "verified": v.verified,
                "partner_banks": v.event.partner_banks,
            }
            for v in state.sale_events.events
        ]
    if state.alternatives:
        facts["alternatives_reference"] = [
            {
                "reference": a.reference.title,
                "price_ceiling": format_inr(a.price_ceiling) if a.price_ceiling else None,
            }
            for a in state.alternatives
        ]
    gaps = list(dict.fromkeys(state.uncertainties + state.warnings))[:10]
    if gaps:
        facts["gaps_and_warnings"] = gaps
    if state.requirements and state.requirements.unknowns:
        facts["unknown_requirements"] = state.requirements.unknowns
    return facts


def facts_json(facts: dict[str, Any]) -> str:
    return json.dumps(facts, ensure_ascii=False, default=str, indent=1)


# --- grounding ---


def _amounts(state: AgentState) -> tuple[set[Decimal], set[Decimal]]:
    amounts: set[Decimal] = set()
    percents: set[Decimal] = set()

    def add(*values: Decimal | None) -> None:
        amounts.update(v for v in values if v is not None)

    for s in state.listings.values():
        add(s.price, s.mrp)
        if s.mrp and s.mrp > s.price:
            add(s.mrp - s.price)
        amounts.update(_numbers_in(s.title))  # model numbers, sizes, capacities
    for d in state.details.values():
        amounts.update(_numbers_in(" ".join(str(v) for v in d.specs.values())))
    for c in state.candidates.values():
        add(c.lowest_price)
    for o in state.offers.values():
        for offer_view in o.offers:
            off = offer_view.offer
            add(off.flat_amount, off.max_discount, off.min_order_value)
            if off.percent is not None:
                percents.add(off.percent)
            if off.exchange:
                add(off.exchange.max_value, off.exchange.min_value, off.exchange.bonus)
                add(off.exchange.max_value + off.exchange.bonus)
        for sc in o.scenarios:
            add(
                sc.base_price,
                sc.upfront_best,
                sc.upfront_conservative,
                sc.deferred_cashback,
                sc.net_after_cashback,
            )
            add(sc.base_price - sc.net_after_cashback, sc.base_price - sc.upfront_best)
            for applied in sc.applied:
                add(applied.benefit_low, applied.benefit_high)
        for row in o.card_matrix:
            add(row.best.net_after_cashback, row.best.upfront_conservative)
    for h in state.price_history.values():
        if h.baseline:
            b = h.baseline
            add(b.current, b.previous, b.minimum, b.maximum, b.average, b.median, b.mrp)
            percents.update(
                p
                for p in (b.change_vs_average_pct, b.change_vs_previous_pct, b.discount_vs_mrp_pct)
                if p is not None
            )
            percents.update(
                abs(p) for p in (b.change_vs_average_pct, b.change_vs_previous_pct) if p is not None
            )
        if h.pre_sale_hike:
            add(h.pre_sale_hike.recent_median, h.pre_sale_hike.prior_median)
            if h.pre_sale_hike.change_pct is not None:
                percents.add(h.pre_sale_hike.change_pct)
        add(*(p.price for p in h.daily))
    for f in state.focus:
        add(f.best_eligible, f.best_conditional)
        if f.timing:
            add(f.timing.suggested_alert_price)
    for a in state.alternatives:
        add(a.reference.price, a.price_ceiling)
        for alt in a.alternatives:
            add(alt.candidate.lowest_price, abs(alt.price_difference))
    if state.requirements:
        b2 = state.requirements.budget
        add(b2.minimum, b2.maximum, b2.target)
    if state.analysis and state.analysis.target_price:
        add(Decimal(str(state.analysis.target_price)))
    for item in state.focus:
        if item.best_eligible and state.requirements and state.requirements.budget.maximum:
            add(abs(state.requirements.budget.maximum - item.best_eligible))
    return amounts, percents


def _text_fields(answer: FinalAnswer) -> Iterable[tuple[str, str]]:
    yield "summary", answer.summary
    for i, r in enumerate(answer.recommendations):
        yield f"rec{i}.price", r.price_summary
        for j, w in enumerate(r.why):
            yield f"rec{i}.why{j}", w
        for j, t in enumerate(r.trade_offs):
            yield f"rec{i}.trade{j}", t
    for i, d in enumerate(answer.deal_insights):
        yield f"deal{i}", d
    if answer.timing:
        yield "timing", answer.timing
    for i, x in enumerate(answer.risks_and_conflicts):
        yield f"risk{i}", x
    for i, x in enumerate(answer.uncertainties):
        yield f"unc{i}", x


_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers_in(text: str) -> set[Decimal]:
    """Every number literally present in the facts (model numbers, specs, counts, prices)."""
    out: set[Decimal] = set()
    for token in _NUMBER.findall(text):
        try:
            out.add(Decimal(token.replace(",", "")))
        except InvalidOperation:
            continue
    return out


def ground_answer(
    state: AgentState, answer: FinalAnswer, *, facts_text: str = ""
) -> tuple[FinalAnswer, GroundingReport]:
    """Check every figure against collected data; mark untraceable ones; drop invalid ids."""
    amounts, percents = _amounts(state)
    literal = _numbers_in(facts_text)
    amounts |= literal
    percents |= literal
    checked = 0
    ungrounded = []
    replacements: dict[str, str] = {}
    for key, text in _text_fields(answer):
        report = check_grounding(text, amounts=amounts, percents=percents)
        checked += report.checked
        if not report.ok:
            ungrounded.extend(report.ungrounded)
            replacements[key] = mark_ungrounded(text, report)

    def fix(key: str, text: str) -> str:
        return replacements.get(key, text)

    known_evidence = set(state.evidence)
    recommendations: list[Recommendation] = []
    for i, r in enumerate(answer.recommendations):
        if r.listing_id not in state.listings:
            state.warnings.append(
                f"dropped a recommendation with unknown listing id {r.listing_id!r}"
            )
            continue
        recommendations.append(
            r.model_copy(
                update={
                    "price_summary": fix(f"rec{i}.price", r.price_summary),
                    "why": [fix(f"rec{i}.why{j}", w) for j, w in enumerate(r.why)],
                    "trade_offs": [fix(f"rec{i}.trade{j}", t) for j, t in enumerate(r.trade_offs)],
                    "evidence_ids": [e for e in r.evidence_ids if e in known_evidence],
                }
            )
        )
    uncertainties = [fix(f"unc{i}", x) for i, x in enumerate(answer.uncertainties)]
    if ungrounded and UNVERIFIED_NOTE not in uncertainties:
        uncertainties.append(UNVERIFIED_NOTE)
    grounded = answer.model_copy(
        update={
            "summary": fix("summary", answer.summary),
            "recommendations": recommendations,
            "deal_insights": [fix(f"deal{i}", d) for i, d in enumerate(answer.deal_insights)],
            "timing": fix("timing", answer.timing) if answer.timing else None,
            "risks_and_conflicts": [
                fix(f"risk{i}", x) for i, x in enumerate(answer.risks_and_conflicts)
            ],
            "uncertainties": uncertainties,
        }
    )
    return grounded, GroundingReport(checked=checked, ungrounded=ungrounded)


# --- fallback when the summary can't be generated ---


def fallback_answer(state: AgentState) -> FinalAnswer:
    recs: list[Recommendation] = []
    for i, item in enumerate(state.focus[:3]):
        offers = state.offers.get(item.listing.listing_id)
        best = min(
            (s for s in (offers.scenarios if offers else []) if not s.conditional),
            key=lambda s: s.net_after_cashback,
            default=None,
        )
        price = (
            f"{format_inr(best.upfront_conservative)} ({best.name})"
            if best
            else f"{format_inr(item.listing.price)} listed"
        )
        recs.append(
            Recommendation(
                listing_id=item.listing.listing_id,
                product=item.listing.title,
                verdict="top_pick" if i == 0 else "consider",
                price_summary=f"{price} on {item.listing.marketplace.value}",
                why=[
                    f"{c.requirement}: {c.status.value}"
                    for c in item.checks
                    if c.status is MatchStatus.MEETS
                ],
                trade_offs=[
                    f"{c.requirement}: {c.status.value}"
                    for c in item.checks
                    if c.status in {MatchStatus.FAILS, MatchStatus.PARTIAL}
                    or (c.status is MatchStatus.UNKNOWN and c.level is Level.HARD)
                ],
                evidence_ids=[],
            )
        )
    timing = next((f.timing.summary for f in state.focus if f.timing), None)
    return FinalAnswer(
        summary=(
            "The written summary could not be generated, so these are the computed results "
            "ranked by your requirements and effective price."
            if recs
            else "No suitable products were found from the available data."
        ),
        recommendations=recs,
        deal_insights=[],
        timing=timing,
        risks_and_conflicts=[c.explanation for c in state.conflicts][:5],
        uncertainties=list(dict.fromkeys(state.uncertainties))[:5],
        questions_for_user=[],
        suggested_actions=[],
    )
