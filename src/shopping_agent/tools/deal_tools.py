"""Deal tools: offers and effective-price scenarios, price history, sale events."""

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Self

from pydantic import BaseModel, Field, model_validator

from shopping_agent.db.repositories import (
    CatalogRepository,
    EvidenceRepository,
    OfferRepository,
    PriceRepository,
    SaleEventRepository,
)
from shopping_agent.db.repositories.catalog import listing_to_domain
from shopping_agent.domain.common import Confidence, SourceType, Verification
from shopping_agent.domain.evidence import Evidence
from shopping_agent.domain.offer import (
    Eligibility,
    Offer,
    PriceScenario,
    StackingPair,
)
from shopping_agent.domain.price import PreSaleHike, PriceBaseline
from shopping_agent.domain.product import Marketplace
from shopping_agent.domain.sale import SaleEvent, SaleState
from shopping_agent.errors import ToolInputError
from shopping_agent.security.untrusted import wrap_untrusted
from shopping_agent.services.formatting import format_inr
from shopping_agent.services.freshness import DataKind, is_stale
from shopping_agent.services.offer_engine import (
    CardRow,
    ExcludedOffer,
    build_scenarios,
    card_matrix,
)
from shopping_agent.services.price_baseline import (
    compute_baseline,
    daily_closes,
    describe_low,
    detect_pre_sale_hike,
)
from shopping_agent.services.sale_mode import SaleStatus, sale_status
from shopping_agent.tools.base import Tool, ToolContext, ToolResult
from shopping_agent.tools.common import ListingSummary, load_listing, summarize

# --- find_offers ---


class FindOffersInput(BaseModel):
    listing_id: str


class OfferView(BaseModel):
    offer: Offer
    eligibility: Eligibility
    source: str
    last_seen_at: datetime


class FindOffersOutput(BaseModel):
    listing: ListingSummary
    offers: list[OfferView]
    scenarios: list[PriceScenario]
    stacking: list[StackingPair]
    card_matrix: list[CardRow]
    excluded: list[ExcludedOffer]
    unstructured_offer_texts: list[str] = Field(
        default_factory=list, description="Raw offer text from the listing (untrusted, unparsed)"
    )
    notes: list[str] = Field(default_factory=list)


class FindOffersTool(Tool[FindOffersInput, FindOffersOutput]):
    name = "find_offers"
    description = (
        "Offers for a listing (bank, coupon, cashback, exchange, membership), checked against "
        "the user's payment profile, with a stacking matrix and effective-price scenarios "
        "computed deterministically: A standard, B best single eligible offer, C best eligible "
        "combination, D best price with conditions. Never add offers together yourself."
    )
    input_model = FindOffersInput
    output_model = FindOffersOutput

    async def run(self, ctx: ToolContext, args: FindOffersInput) -> ToolResult[FindOffersOutput]:
        now = ctx.clock()
        async with ctx.sessions() as session:
            row = await load_listing(session, args.listing_id)
            stored = await OfferRepository(session).active_for(
                platform=Marketplace(row.marketplace),
                now=now,
                listing_id=row.id,
                variant_id=row.variant_id,
            )
        offers: list[Offer] = []
        meta: dict[str, tuple[str, datetime]] = {}
        for offer, offer_row in stored:
            stale = is_stale(
                DataKind.OFFER, offer_row.last_seen_at, now, sale_active=ctx.sale_active
            )
            offers.append(offer.model_copy(update={"stale": stale or offer.stale}))
            meta[offer.id] = (offer_row.source, offer_row.last_seen_at)

        listing = listing_to_domain(row)
        result = build_scenarios(listing, offers, ctx.profile, now=now, category=row.category)
        cards = (
            card_matrix(listing, offers, ctx.profile, now=now, category=row.category)
            if ctx.profile.payment_methods
            else []
        )
        elig = {e.offer_id: e for e in result.eligibility}
        views = [
            OfferView(
                offer=o, eligibility=elig[o.id], source=meta[o.id][0], last_seen_at=meta[o.id][1]
            )
            for o in offers
        ]
        notes: list[str] = []
        if not ctx.profile.payment_methods:
            notes.append(
                "No saved payment methods: card offers are shown as conditional. Ask which "
                "cards the user has (bank and credit/debit only — never card numbers)."
            )
        if any(o.stale for o in offers):
            notes.append("Some offers were last seen a while ago and may have changed.")
        if not offers:
            notes.append("No structured offers found for this listing.")

        evidence = [
            Evidence(
                claim=f"Offer: {o.title}" + (f" — terms: {o.terms_text}" if o.terms_text else ""),
                subject=row.id,
                attribute=f"offer:{o.type.value}",
                value=o.id,
                source_type=SourceType.USER_INPUT
                if meta[o.id][0] == "user"
                else SourceType.MARKETPLACE_LISTING,
                source_name=meta[o.id][0],
                retrieved_at=meta[o.id][1],
                verification=o.verification,
                confidence=Confidence.MEDIUM,
            )
            for o in offers
        ]
        evidence.extend(
            Evidence(
                claim=(
                    f"Scenario {s.label} ({s.name}): "
                    f"pay {format_inr(s.upfront_conservative)} at checkout"
                    + (f" (best case {format_inr(s.upfront_best)})" if s.is_range else "")
                    + (
                        f", {format_inr(s.deferred_cashback)} cashback later"
                        if s.deferred_cashback
                        else ""
                    )
                ),
                subject=row.id,
                attribute=f"scenario_{s.label}",
                value=str(s.net_after_cashback),
                source_type=SourceType.COMPUTED,
                source_name="offer engine",
                retrieved_at=now,
                verification=s.verification,
                confidence=Confidence.HIGH,
            )
            for s in result.scenarios
        )
        async with ctx.sessions() as session, session.begin():
            await EvidenceRepository(session).add_many(evidence, run_id=ctx.run_id)
        return ToolResult(
            data=FindOffersOutput(
                listing=summarize(row, ctx),
                offers=views,
                scenarios=result.scenarios,
                stacking=result.matrix.pairs,
                card_matrix=cards,
                excluded=result.excluded,
                unstructured_offer_texts=[
                    wrap_untrusted(t, source=row.source_name, url=row.url)
                    for t in row.offer_texts or []
                ],
                notes=notes,
            ),
            evidence=evidence,
        )


# --- get_price_history ---


class PriceHistoryInput(BaseModel):
    listing_id: str | None = None
    variant_id: str | None = None
    product_id: str | None = None
    days: int = Field(default=90, ge=1, le=365)

    @model_validator(mode="after")
    def _one(self) -> Self:
        if sum(bool(v) for v in (self.listing_id, self.variant_id, self.product_id)) != 1:
            raise ValueError("give exactly one of listing_id, variant_id or product_id")
        return self


class DailyPoint(BaseModel):
    date: date
    price: Decimal


class ListingHistory(BaseModel):
    listing: ListingSummary
    baseline: PriceBaseline | None
    low_note: str | None
    daily: list[DailyPoint]
    pre_sale_hike: PreSaleHike | None = None


class PriceHistoryOutput(BaseModel):
    period_days: int
    listings: list[ListingHistory]
    total_observations: int
    coverage_note: str


class GetPriceHistoryTool(Tool[PriceHistoryInput, PriceHistoryOutput]):
    name = "get_price_history"
    description = (
        "Observed price history with baseline stats (min/max/average/median, trend, coverage "
        "tier) per listing, and pre-sale price-hike detection when a sale is near. History "
        "covers only our own observations — never call anything 'lowest ever'."
    )
    input_model = PriceHistoryInput
    output_model = PriceHistoryOutput

    async def run(
        self, ctx: ToolContext, args: PriceHistoryInput
    ) -> ToolResult[PriceHistoryOutput]:
        now = ctx.clock()
        since = now - timedelta(days=args.days)
        async with ctx.sessions() as session:
            catalog = CatalogRepository(session)
            if args.listing_id:
                rows = [await load_listing(session, args.listing_id)]
            else:
                rows = await catalog.listings_for(
                    product_id=args.product_id, variant_id=args.variant_id
                )
                if not rows:
                    raise ToolInputError("no listings recorded for that product/variant")
            observations = await PriceRepository(session).history([r.id for r in rows], since=since)
            events = await SaleEventRepository(session).list(ending_after=now - timedelta(days=3))

        histories: list[ListingHistory] = []
        evidence: list[Evidence] = []
        for row in rows:
            obs = [o for o in observations if o.listing_id == row.id]
            baseline = compute_baseline(obs)
            platform_events = [e for e in events if e.platform.value == row.marketplace]
            status = sale_status(platform_events, now)
            hike = None
            if status.event is not None and status.state in {
                SaleState.PRE_SALE,
                SaleState.EARLY_ACCESS,
                SaleState.ACTIVE,
            }:
                hike = detect_pre_sale_hike(obs, status.event.start)
            histories.append(
                ListingHistory(
                    listing=summarize(row, ctx),
                    baseline=baseline,
                    low_note=describe_low(baseline) if baseline else None,
                    daily=[DailyPoint(date=d, price=p) for d, p in daily_closes(obs).items()],
                    pre_sale_hike=hike,
                )
            )
            if baseline is not None:
                evidence.append(
                    Evidence(
                        claim=(
                            f"Observed {row.marketplace} prices over {baseline.coverage_days} days "
                            f"({baseline.observation_count} observations): low "
                            f"{format_inr(baseline.minimum)}, high {format_inr(baseline.maximum)}, "
                            f"average {format_inr(baseline.average)}"
                        ),
                        subject=row.id,
                        attribute="price_history",
                        value=str(baseline.minimum),
                        source_type=SourceType.PRICE_TRACKING,
                        source_name="our price tracking",
                        retrieved_at=now,
                        verification=Verification.VERIFIED,
                        confidence=Confidence.HIGH,
                    )
                )
        if observations:
            first = min(o.observed_at for o in observations)
            span = (now - first).days + 1
            note = (
                f"History covers only our own observations since {first:%d %b %Y} "
                f"(about {span} days). It is not complete market history."
            )
        else:
            note = "No price history yet. Tracking this product will start building it."
        return ToolResult(
            data=PriceHistoryOutput(
                period_days=args.days,
                listings=histories,
                total_observations=len(observations),
                coverage_note=note,
            ),
            evidence=evidence,
        )


# --- get_sale_events ---


class SaleEventsInput(BaseModel):
    platform: Marketplace | None = None
    include_past: bool = False


class SaleEventView(BaseModel):
    event: SaleEvent
    state_now: SaleState
    verified: bool
    needs_reverification: bool


class SaleEventsOutput(BaseModel):
    events: list[SaleEventView]
    current: SaleStatus
    note: str | None = None


class GetSaleEventsTool(Tool[SaleEventsInput, SaleEventsOutput]):
    name = "get_sale_events"
    description = (
        "Known sale events (e.g. Big Billion Days, Great Indian Festival) with phases, partner "
        "banks and verification status, plus the current sale state. Dates are only as good as "
        "their verification — unverified or stale entries must be presented as unconfirmed."
    )
    input_model = SaleEventsInput
    output_model = SaleEventsOutput

    async def run(self, ctx: ToolContext, args: SaleEventsInput) -> ToolResult[SaleEventsOutput]:
        now = ctx.clock()
        async with ctx.sessions() as session:
            events = await SaleEventRepository(session).list(
                platform=args.platform,
                ending_after=None if args.include_past else now - timedelta(days=3),
            )
        views = [
            SaleEventView(
                event=e,
                state_now=sale_status([e], now).state,
                verified=e.verification is Verification.VERIFIED,
                needs_reverification=is_stale(DataKind.SALE_EVENT, e.last_verified_at, now),
            )
            for e in events
        ]
        note = None
        if not views:
            note = (
                "No sale events recorded. Check official sources with search_web "
                "(purpose=sale_events) and treat any dates found as unconfirmed until verified."
            )
        evidence = [
            Evidence(
                claim=f"{e.name} on {e.platform.value}: {e.start:%d %b %Y} – {e.end:%d %b %Y}",
                subject=e.id,
                attribute="sale_dates",
                value=e.start.isoformat(),
                source_type=SourceType.MARKETPLACE_OFFICIAL
                if e.verification is Verification.VERIFIED
                else SourceType.NEWS,
                source_name=", ".join(e.official_sources) or "recorded sale event",
                retrieved_at=e.last_verified_at,
                verification=e.verification,
                confidence=Confidence.HIGH
                if e.verification is Verification.VERIFIED
                else Confidence.LOW,
            )
            for e in events
        ]
        return ToolResult(
            data=SaleEventsOutput(
                events=views, current=sale_status(events, now, platform=args.platform), note=note
            ),
            evidence=evidence,
        )
