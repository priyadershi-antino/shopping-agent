"""Catalog tools: search, link resolution, details, cross-seller listings, alternatives."""

from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

from shopping_agent.db.repositories import CatalogRepository, EvidenceRepository
from shopping_agent.db.repositories.catalog import listing_to_domain, variant_attributes
from shopping_agent.domain.common import Confidence, SourceType, Verification
from shopping_agent.domain.evidence import Evidence
from shopping_agent.domain.product import VariantAttributes
from shopping_agent.domain.requirements import Constraint, ConstraintOp
from shopping_agent.errors import ToolInputError
from shopping_agent.providers.product.base import ProductQuery, ProductRef
from shopping_agent.services.conflicts import Conflict, detect_conflicts
from shopping_agent.services.normalization import normalize_specs
from shopping_agent.services.requirement_match import Level, RequirementCheck, check_constraint
from shopping_agent.services.url_parser import IdType, UrlParseError, parse_product_url
from shopping_agent.services.variant_gate import (
    NON_NEW,
    GateDecision,
    check_variant,
    listing_risk_flags,
)
from shopping_agent.tools.base import Tool, ToolContext, ToolResult
from shopping_agent.tools.common import (
    Candidate,
    ListingSummary,
    attribute_evidence,
    build_candidates,
    ingest,
    load_listing,
    ref_from_row,
    summarize,
)

# Search a little above the budget so offers that bring a product under it aren't missed.
OFFER_HEADROOM = Decimal("1.15")


# --- search_products ---


class SearchProductsInput(BaseModel):
    query: str = Field(min_length=2, max_length=200, description="Product search terms")
    category: str | None = Field(
        default=None, description="e.g. laptop, smartphone, tablet, monitor, headphones"
    )
    max_price: Decimal | None = Field(default=None, gt=0, description="Budget ceiling in INR")
    min_price: Decimal | None = Field(default=None, gt=0)
    limit: int = Field(default=10, ge=1, le=20)


class SearchProductsOutput(BaseModel):
    candidates: list[Candidate]
    total_listings: int
    providers: list[str]
    demo_data: bool
    note: str | None = None


class SearchProductsTool(Tool[SearchProductsInput, SearchProductsOutput]):
    name = "search_products"
    description = (
        "Search products across marketplaces. Returns candidates grouped by product variant, "
        "each with per-seller listings (price, seller, availability, condition) and risk flags. "
        "Prices are observations with timestamps, not guarantees."
    )
    input_model = SearchProductsInput
    output_model = SearchProductsOutput

    async def run(
        self, ctx: ToolContext, args: SearchProductsInput
    ) -> ToolResult[SearchProductsOutput]:
        query = ProductQuery(
            query=args.query,
            category=args.category,
            min_price=args.min_price,
            max_price=args.max_price * OFFER_HEADROOM if args.max_price else None,
            limit=min(args.limit * 3, 50),
            marketplaces=sorted(ctx.platforms) if ctx.platforms else None,
        )
        gathered = await ctx.providers.search_products(query, sale_active=ctx.sale_active)
        async with ctx.sessions() as session, session.begin():
            rows, evidence = await ingest(
                ctx, session, [x for x in gathered.value if ctx.allows(x.marketplace)]
            )
        candidates = build_candidates(rows, ctx, max_price=args.max_price)[: args.limit]
        note = None
        if args.max_price and any(c.over_budget for c in candidates):
            note = "Candidates marked over_budget may fit only with eligible offers."
        return ToolResult(
            data=SearchProductsOutput(
                candidates=candidates,
                total_listings=len(rows),
                providers=gathered.providers,
                demo_data=ctx.providers.demo_mode,
                note=note,
            ),
            evidence=evidence,
            errors=gathered.errors,
            partial=gathered.partial,
            cached=gathered.cached,
        )


# --- resolve_product_link ---


class ResolveLinkInput(BaseModel):
    url: str = Field(
        min_length=8, max_length=2000, description="A product page URL pasted by the user"
    )


class ParsedLink(BaseModel):
    marketplace: str
    external_id: str | None
    id_type: str
    canonical_url: str
    title_hint: str | None


class ResolveLinkOutput(BaseModel):
    status: Literal["resolved", "needs_confirmation", "needs_full_link", "not_found"]
    parsed: ParsedLink
    listings: list[ListingSummary] = Field(default_factory=list)
    candidates: list[Candidate] = Field(default_factory=list)
    message: str


class ResolveProductLinkTool(Tool[ResolveLinkInput, ResolveLinkOutput]):
    name = "resolve_product_link"
    description = (
        "Identify the product behind a pasted product link (Amazon, Flipkart, Croma, "
        "manufacturer, …). Tracking parameters are removed. If the exact listing can't be "
        "found, returns likely candidates that the user must confirm."
    )
    input_model = ResolveLinkInput
    output_model = ResolveLinkOutput

    async def run(self, ctx: ToolContext, args: ResolveLinkInput) -> ToolResult[ResolveLinkOutput]:
        try:
            parsed = parse_product_url(args.url)
        except UrlParseError as exc:
            return ToolResult(errors=[f"could not read the link: {exc}"])
        info = ParsedLink(
            marketplace=parsed.marketplace.value,
            external_id=parsed.external_id,
            id_type=parsed.id_type.value,
            canonical_url=parsed.canonical_url,
            title_hint=parsed.title_hint,
        )
        if parsed.id_type is IdType.SHORT_LINK:
            return ToolResult(
                data=ResolveLinkOutput(
                    status="needs_full_link",
                    parsed=info,
                    message=(
                        "This is a shortened link. Open it in your browser and paste the full "
                        "product URL from the address bar."
                    ),
                )
            )

        errors: list[str] = []
        evidence: list[Evidence] = []
        if parsed.external_id and parsed.id_type is not IdType.SLUG:
            gathered = await ctx.providers.lookup(
                parsed.marketplace, parsed.external_id, sale_active=ctx.sale_active
            )
            errors.extend(gathered.errors)
            async with ctx.sessions() as session, session.begin():
                rows, evidence = await ingest(ctx, session, gathered.value)
                if not rows:
                    existing = await CatalogRepository(session).find_by_external(
                        parsed.marketplace, parsed.external_id
                    )
                    rows = [existing] if existing else []
            if rows:
                stale_note = "" if gathered.value else " (from our saved data; live lookup failed)"
                return ToolResult(
                    data=ResolveLinkOutput(
                        status="resolved",
                        parsed=info,
                        listings=[summarize(r, ctx) for r in rows],
                        message=f"Found the exact listing{stale_note}.",
                    ),
                    evidence=evidence,
                    errors=errors,
                    partial=bool(errors),
                )

        if parsed.title_hint:
            gathered_search = await ctx.providers.search_products(
                ProductQuery(query=parsed.title_hint, limit=15), sale_active=ctx.sale_active
            )
            errors.extend(gathered_search.errors)
            async with ctx.sessions() as session, session.begin():
                rows, evidence = await ingest(ctx, session, gathered_search.value)
            candidates = build_candidates(rows, ctx)[:5]
            if candidates:
                return ToolResult(
                    data=ResolveLinkOutput(
                        status="needs_confirmation",
                        parsed=info,
                        candidates=candidates,
                        message=(
                            "Couldn't look up this exact listing; these products match the "
                            "link's title. Ask the user to confirm which one it is."
                        ),
                    ),
                    evidence=evidence,
                    errors=errors,
                    partial=bool(errors),
                )
        return ToolResult(
            data=ResolveLinkOutput(
                status="not_found",
                parsed=info,
                message="No matching product found from the available data sources.",
            ),
            errors=errors,
            partial=bool(errors),
        )


# --- get_product_details ---


class ProductDetailsInput(BaseModel):
    listing_id: str | None = None
    name: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _one(self) -> Self:
        if not self.listing_id and not self.name:
            raise ValueError("give listing_id or name")
        return self


class ProductDetailsOutput(BaseModel):
    name: str
    brand: str | None
    model_number: str | None
    category: str | None
    specs: dict[str, str | int | float | bool]
    source_name: str
    source_type: SourceType
    verification: Verification
    official_url: str | None
    conflicts: list[Conflict] = Field(default_factory=list)


class GetProductDetailsTool(Tool[ProductDetailsInput, ProductDetailsOutput]):
    name = "get_product_details"
    description = (
        "Official/aggregated specifications for a product, each with its source. When a "
        "listing_id is given, the listing's own claims are compared with the specifications "
        "and any disagreement is returned as a conflict."
    )
    input_model = ProductDetailsInput
    output_model = ProductDetailsOutput

    async def run(
        self, ctx: ToolContext, args: ProductDetailsInput
    ) -> ToolResult[ProductDetailsOutput]:
        row = None
        if args.listing_id:
            async with ctx.sessions() as session:
                row = await load_listing(session, args.listing_id)
            ref = ref_from_row(row)
        else:
            ref = ProductRef(name=args.name)
        gathered = await ctx.providers.product_details(ref, sale_active=ctx.sale_active)
        details = gathered.value
        if details is None:
            return ToolResult(
                errors=[*gathered.errors, "no specifications found for this product"],
                partial=True,
            )
        specs = normalize_specs(details.specs)
        subject = row.id if row is not None else f"name:{details.name}"[:64]
        confidence = (
            Confidence.HIGH if details.verification is Verification.VERIFIED else Confidence.MEDIUM
        )
        evidence = [
            Evidence(
                claim=f"{details.source_name}: {key} = {value}",
                subject=subject,
                attribute=key,
                value=value,
                source_type=details.source_type,
                source_name=details.source_name,
                url=details.url or details.official_url,
                retrieved_at=details.retrieved_at,
                verification=details.verification,
                confidence=confidence,
            )
            for key, value in specs.items()
        ]
        conflicts: list[Conflict] = []
        if row is not None:
            listing_claims = attribute_evidence(row, subject)
            conflicts = detect_conflicts([*evidence, *listing_claims])
            evidence.extend(listing_claims)
        async with ctx.sessions() as session, session.begin():
            await EvidenceRepository(session).add_many(evidence, run_id=ctx.run_id)
        return ToolResult(
            data=ProductDetailsOutput(
                name=details.name,
                brand=details.brand,
                model_number=details.model_number,
                category=details.category,
                specs=specs,
                source_name=details.source_name,
                source_type=details.source_type,
                verification=details.verification,
                official_url=details.official_url,
                conflicts=conflicts,
            ),
            evidence=evidence,
            errors=gathered.errors,
            partial=gathered.partial,
            cached=gathered.cached,
        )


# --- get_listings ---


class GetListingsInput(BaseModel):
    listing_id: str = Field(description="A listing of the exact variant you want to compare")


class GatedListing(BaseModel):
    listing: ListingSummary
    decision: GateDecision
    unverified: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)


class RejectedListing(BaseModel):
    listing: ListingSummary
    reasons: list[str]


class GetListingsOutput(BaseModel):
    target_model_number: str | None
    target_attributes: VariantAttributes
    matching: list[GatedListing]
    rejected: list[RejectedListing]
    providers: list[str]


class GetListingsTool(Tool[GetListingsInput, GetListingsOutput]):
    name = "get_listings"
    description = (
        "Current listings of the same product across sellers and marketplaces. Each listing "
        "passes the variant gate (RAM, storage, connectivity, condition, region, model) or is "
        "rejected with a reason — rejected listings must not be used in price comparisons."
    )
    input_model = GetListingsInput
    output_model = GetListingsOutput

    async def run(self, ctx: ToolContext, args: GetListingsInput) -> ToolResult[GetListingsOutput]:
        async with ctx.sessions() as session:
            ref_row = await load_listing(session, args.listing_id)
            target = VariantAttributes.model_validate(ref_row.attributes or {})
            if ref_row.variant_id:
                variant = await CatalogRepository(session).get_variant(ref_row.variant_id)
                if variant is not None:
                    target = variant_attributes(variant)
        gathered = await ctx.providers.product_listings(
            ref_from_row(ref_row), sale_active=ctx.sale_active
        )
        async with ctx.sessions() as session, session.begin():
            fresh, evidence = await ingest(
                ctx, session, [x for x in gathered.value if ctx.allows(x.marketplace)]
            )
            known = (
                await CatalogRepository(session).listings_for(product_id=ref_row.product_id)
                if ref_row.product_id
                else []
            )
        # The listing being examined stays; other sellers must be on the user's platforms.
        rows = {
            r.id: r
            for r in [ref_row, *known, *fresh]
            if r.id == ref_row.id or ctx.allows(r.marketplace)
        }
        matching: list[GatedListing] = []
        rejected: list[RejectedListing] = []
        gated = []
        for row in rows.values():
            domain = listing_to_domain(row)
            result = check_variant(domain, target, target_model_number=ref_row.model_number)
            gated.append((row, domain, result))
        passed_prices = [d.price for _, d, r in gated if r.decision is not GateDecision.REJECT]
        for row, domain, result in gated:
            summary = summarize(row, ctx)
            if result.decision is GateDecision.REJECT:
                rejected.append(RejectedListing(listing=summary, reasons=result.reasons))
                continue
            peers = [p for p in passed_prices if p != domain.price]
            matching.append(
                GatedListing(
                    listing=summary,
                    decision=result.decision,
                    unverified=result.unverified,
                    risk_flags=[f.message for f in listing_risk_flags(domain, peer_prices=peers)],
                )
            )
        matching.sort(key=lambda g: g.listing.price)
        return ToolResult(
            data=GetListingsOutput(
                target_model_number=ref_row.model_number,
                target_attributes=target,
                matching=matching,
                rejected=rejected,
                providers=gathered.providers,
            ),
            evidence=evidence,
            errors=gathered.errors,
            partial=gathered.partial,
            cached=gathered.cached,
        )


# --- find_alternatives ---

KeepAttribute = Literal["ram_gb", "storage_gb"]


def _default_keep() -> list[KeepAttribute]:
    return ["ram_gb", "storage_gb"]


class FindAlternativesInput(BaseModel):
    listing_id: str = Field(description="The reference product's listing")
    cheaper_by: Decimal | None = Field(
        default=None, gt=0, description="At least this much cheaper (INR)"
    )
    max_price: Decimal | None = Field(default=None, gt=0)
    keep_at_least: list[KeepAttribute] = Field(
        default_factory=_default_keep,
        description="Attributes that must be at least as good as the reference",
    )
    query: str | None = Field(
        default=None, max_length=200, description="Optional extra search terms"
    )
    limit: int = Field(default=5, ge=1, le=10)


class Alternative(BaseModel):
    candidate: Candidate
    price_difference: Decimal  # negative = cheaper than the reference
    comparison: list[RequirementCheck]


class FindAlternativesOutput(BaseModel):
    reference: ListingSummary
    price_ceiling: Decimal | None
    alternatives: list[Alternative]
    providers: list[str]


class FindAlternativesTool(Tool[FindAlternativesInput, FindAlternativesOutput]):
    name = "find_alternatives"
    description = (
        "Find alternatives to a reference product, optionally cheaper by an amount, keeping key "
        "attributes at least as good. Each alternative includes a transparent comparison."
    )
    input_model = FindAlternativesInput
    output_model = FindAlternativesOutput

    async def run(
        self, ctx: ToolContext, args: FindAlternativesInput
    ) -> ToolResult[FindAlternativesOutput]:
        async with ctx.sessions() as session:
            ref = await load_listing(session, args.listing_id)
        ref_attrs = VariantAttributes.model_validate(ref.attributes or {})
        ceiling = args.max_price
        if args.cheaper_by is not None:
            target = ref.current_price - args.cheaper_by
            ceiling = min(ceiling, target) if ceiling else target
        if ceiling is not None and ceiling <= 0:
            raise ToolInputError("the price ceiling is not positive")
        terms = [args.query or "", ref.category or ""]
        if ref_attrs.ram_gb and "ram_gb" in args.keep_at_least:
            terms.append(f"{ref_attrs.ram_gb}GB RAM")
        query = " ".join(t for t in terms if t).strip() or ref.title_raw
        gathered = await ctx.providers.search_products(
            ProductQuery(
                query=query,
                category=ref.category,
                max_price=ceiling,
                limit=30,
                marketplaces=sorted(ctx.platforms) if ctx.platforms else None,
            ),
            sale_active=ctx.sale_active,
        )
        async with ctx.sessions() as session, session.begin():
            rows, evidence = await ingest(
                ctx, session, [x for x in gathered.value if ctx.allows(x.marketplace)]
            )
        constraints = [
            Constraint(attribute=a, op=ConstraintOp.GTE, value=getattr(ref_attrs, a))
            for a in args.keep_at_least
            if getattr(ref_attrs, a) is not None
        ]
        alternatives: list[Alternative] = []
        for cand in build_candidates(rows, ctx):
            if ref.product_id and cand.product_id == ref.product_id:
                continue
            if ceiling is not None and cand.lowest_price > ceiling:
                continue
            if any(s.condition in NON_NEW for s in cand.listings):
                continue
            facts = cand.attributes.model_dump(exclude_none=True)
            checks = [check_constraint(c, facts, Level.HARD) for c in constraints]
            alternatives.append(
                Alternative(
                    candidate=cand,
                    price_difference=cand.lowest_price - ref.current_price,
                    comparison=checks,
                )
            )
        alternatives.sort(
            key=lambda a: (
                sum(1 for c in a.comparison if c.status.value == "fails"),
                sum(1 for c in a.comparison if c.status.value == "unknown"),
                a.candidate.lowest_price,
            )
        )
        return ToolResult(
            data=FindAlternativesOutput(
                reference=summarize(ref, ctx),
                price_ceiling=ceiling,
                alternatives=alternatives[: args.limit],
                providers=gathered.providers,
            ),
            evidence=evidence,
            errors=gathered.errors,
            partial=gathered.partial,
            cached=gathered.cached,
        )
