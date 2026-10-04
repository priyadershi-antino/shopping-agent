"""Shared tool helpers: ingesting provider data (with provenance) and compact summaries."""

import re
from collections import Counter
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from shopping_agent.db.models import ListingRow
from shopping_agent.db.repositories import (
    CatalogRepository,
    EvidenceRepository,
    OfferRepository,
    PriceRepository,
)
from shopping_agent.db.repositories.catalog import listing_to_domain
from shopping_agent.domain.common import Confidence, SourceType, Verification
from shopping_agent.domain.evidence import Evidence
from shopping_agent.domain.product import (
    Availability,
    Condition,
    Marketplace,
    VariantAttributes,
)
from shopping_agent.errors import ToolInputError
from shopping_agent.providers.product.base import ProductRef, ProviderListing
from shopping_agent.services.formatting import format_inr
from shopping_agent.services.freshness import DataKind, is_stale
from shopping_agent.services.normalization import (
    detect_brand,
    detect_condition,
    extract_attributes,
    group_listings,
    normalize_brand,
    normalize_model_number,
    normalize_specs,
)
from shopping_agent.services.variant_gate import NON_NEW, listing_risk_flags
from shopping_agent.tools.base import ToolContext


class ListingSummary(BaseModel):
    listing_id: str
    product_id: str | None
    variant_id: str | None
    marketplace: Marketplace
    seller: str | None
    title: str
    price: Decimal
    mrp: Decimal | None
    availability: Availability
    condition: Condition
    attributes: VariantAttributes
    rating: float | None
    rating_count: int | None
    url: str | None
    observed_at: datetime
    stale: bool
    source: str
    verification: Verification


class Candidate(BaseModel):
    key: str
    confidence: Confidence
    title: str
    brand: str | None
    model_number: str | None
    category: str | None
    product_id: str | None
    variant_id: str | None
    attributes: VariantAttributes
    lowest_price: Decimal
    listings: list[ListingSummary]
    notes: list[str] = Field(default_factory=list)
    possible_matches: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    over_budget: bool = False


def summarize(row: ListingRow, ctx: ToolContext) -> ListingSummary:
    return ListingSummary(
        listing_id=row.id,
        product_id=row.product_id,
        variant_id=row.variant_id,
        marketplace=Marketplace(row.marketplace),
        seller=row.seller_name,
        title=row.title_raw,
        price=row.current_price,
        mrp=row.mrp,
        availability=Availability(row.availability),
        condition=Condition(row.condition),
        attributes=VariantAttributes.model_validate(row.attributes or {}),
        rating=row.rating,
        rating_count=row.rating_count,
        url=row.url,
        observed_at=row.last_observed_at,
        stale=is_stale(
            DataKind.PRICE, row.last_observed_at, ctx.clock(), sale_active=ctx.sale_active
        ),
        source=row.source_name,
        verification=Verification(row.verification),
    )


def listing_attributes(pl: ProviderListing) -> VariantAttributes:
    attrs = extract_attributes(pl.title, pl.category)
    specs = normalize_specs(pl.specs)
    if isinstance(specs.get("ram_gb"), int):
        attrs.ram_gb = int(specs["ram_gb"])
    if isinstance(specs.get("storage_gb"), int):
        attrs.storage_gb = int(specs["storage_gb"])
    return attrs


def price_evidence(row: ListingRow) -> Evidence:
    verification = Verification(row.verification)
    return Evidence(
        claim=(
            f"{row.marketplace} listing by {row.seller_name or 'unknown seller'} "
            f"priced at {format_inr(row.current_price)}"
        ),
        subject=row.id,
        attribute="price",
        value=str(row.current_price),
        source_type=SourceType(row.source_type),
        source_name=row.source_name,
        url=row.url,
        retrieved_at=row.last_observed_at,
        verification=verification,
        confidence=Confidence.HIGH if verification is Verification.VERIFIED else Confidence.MEDIUM,
    )


def attribute_evidence(row: ListingRow, subject: str) -> list[Evidence]:
    attrs = VariantAttributes.model_validate(row.attributes or {})
    out: list[Evidence] = []
    for name, label in (("ram_gb", "RAM"), ("storage_gb", "storage")):
        value = getattr(attrs, name)
        if value is not None:
            out.append(
                Evidence(
                    claim=f"{row.source_name} listing states {label} {value} GB",
                    subject=subject,
                    attribute=name,
                    value=value,
                    source_type=SourceType(row.source_type),
                    source_name=row.source_name,
                    url=row.url,
                    retrieved_at=row.last_observed_at,
                    verification=Verification(row.verification),
                    confidence=Confidence.MEDIUM,
                )
            )
    return out


def _lock_order(pl: ProviderListing) -> tuple[str, str, str, str, str]:
    return (
        normalize_brand(pl.brand or detect_brand(pl.title)) or "",
        normalize_model_number(pl.model_number) or "",
        pl.marketplace.value,
        pl.external_id or "",
        pl.url or pl.title,
    )


async def ingest(
    ctx: ToolContext,
    session: AsyncSession,
    listings: list[ProviderListing],
    *,
    replace_offers: bool = False,
) -> tuple[list[ListingRow], list[Evidence]]:
    """Persist provider listings, their price observations and structured offers.

    With `replace_offers` (a full refresh of specific listings), offers the provider no
    longer shows for a listing are marked inactive. User-pasted offers are never touched."""
    catalog = CatalogRepository(session)
    prices = PriceRepository(session)
    offers = OfferRepository(session)
    rows: list[ListingRow] = []
    evidence: list[Evidence] = []
    now = ctx.clock()
    # A fixed order (product, then listing) makes concurrent ingests lock rows in the same
    # order, so two parallel searches wait for each other instead of deadlocking.
    for pl in sorted(listings, key=_lock_order):
        attrs = listing_attributes(pl)
        brand = pl.brand or detect_brand(pl.title)
        condition = (
            pl.condition if pl.condition is not Condition.UNKNOWN else detect_condition(pl.title)
        )
        product_id = await catalog.ensure_product(brand, pl.model_number, pl.title, pl.category)
        variant_id = (
            await catalog.ensure_variant(product_id, brand, pl.model_number, attrs, pl.category)
            if product_id
            else None
        )
        row = await catalog.upsert_listing(
            pl,
            attrs=attrs,
            brand=brand,
            condition=condition,
            product_id=product_id,
            variant_id=variant_id,
        )
        await prices.record(
            row.id,
            price=pl.price,
            mrp=pl.mrp,
            availability=pl.availability,
            source=pl.provider,
            observed_at=pl.observed_at,
        )
        kept = [
            await offers.upsert(offer, source=pl.provider, now=now, listing_id=row.id)
            for offer in pl.offers
        ]
        if replace_offers:
            await offers.deactivate_missing(listing_id=row.id, source=pl.provider, keep=kept)
        rows.append(row)
        evidence.append(price_evidence(row))
    await EvidenceRepository(session).add_many(evidence, run_id=ctx.run_id)
    return rows, evidence


def build_candidates(
    rows: list[ListingRow], ctx: ToolContext, *, max_price: Decimal | None = None
) -> list[Candidate]:
    by_id = {r.id: r for r in rows}
    groups = group_listings([listing_to_domain(r) for r in rows])
    candidates: list[Candidate] = []
    for group in groups:
        members = [by_id[i] for i in group.listing_ids]
        domain = [listing_to_domain(m) for m in members]
        merged = VariantAttributes()
        for d in domain:
            for name in ("ram_gb", "storage_gb", "connectivity", "region", "model_year"):
                if getattr(merged, name) is None and getattr(d.attributes, name) is not None:
                    setattr(merged, name, getattr(d.attributes, name))
        flags: list[str] = []
        for d in domain:
            peers = [o.price for o in domain if o.id != d.id]
            flags.extend(
                f"{d.marketplace.value} ({d.seller_name or 'unknown seller'}): {f.message}"
                for f in listing_risk_flags(d, peer_prices=peers)
            )
        head = members[0]
        new_members = [m for m in members if Condition(m.condition) not in NON_NEW]
        lowest = min(m.current_price for m in (new_members or members))
        notes = list(group.notes)
        if new_members and len(new_members) < len(members):
            notes.append("lowest_price excludes renewed/refurbished/used listings")
        known_variants = Counter(m.variant_id for m in members if m.variant_id)
        variant_id = known_variants.most_common(1)[0][0] if known_variants else None
        candidates.append(
            Candidate(
                key=group.key,
                confidence=group.confidence,
                title=head.title_raw,
                brand=head.brand,
                model_number=head.model_number,
                category=head.category,
                product_id=head.product_id,
                variant_id=variant_id,
                attributes=merged,
                lowest_price=lowest,
                listings=sorted((summarize(m, ctx) for m in members), key=lambda s: s.price),
                notes=notes,
                possible_matches=group.possible_matches,
                risk_flags=flags,
                over_budget=max_price is not None and lowest > max_price,
            )
        )
    return candidates


def short_name(title: str, max_words: int = 8) -> str:
    """Readable product name from a listing title: text before the first '(' ',' '|' or ' - '."""
    head = re.split(r"\s[-–|]\s|[(,|]", title, maxsplit=1)[0]
    words = head.split()
    return " ".join(words[:max_words]) or title[:80]


def ref_from_row(row: ListingRow) -> ProductRef:
    return ProductRef(
        name=row.title_raw,
        brand=row.brand,
        model_number=row.model_number,
        category=row.category,
        marketplace=Marketplace(row.marketplace),
        external_id=row.external_id,
        provider_token=row.provider_token,
    )


async def load_listing(session: AsyncSession, listing_id: str) -> ListingRow:
    row = await CatalogRepository(session).get_listing(listing_id)
    if row is None:
        raise ToolInputError(f"unknown listing_id '{listing_id}'")
    return row
