"""Products, variants and listings."""

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shopping_agent.db.models import ListingRow, ProductRow, VariantRow
from shopping_agent.domain.evidence import new_id
from shopping_agent.domain.product import (
    Availability,
    Condition,
    Listing,
    Marketplace,
    VariantAttributes,
)
from shopping_agent.providers.product.base import ProviderListing
from shopping_agent.services.normalization import (
    identity_key,
    normalize_brand,
    normalize_model_number,
)

# Categories where RAM and storage define the variant; without both we link to the product only.
MEMORY_DEFINED_CATEGORIES = frozenset({"laptop", "smartphone", "tablet"})


def listing_to_domain(row: ListingRow) -> Listing:
    return Listing(
        id=row.id,
        variant_id=row.variant_id,
        marketplace=Marketplace(row.marketplace),
        seller_name=row.seller_name,
        url=row.url,
        external_id=row.external_id,
        title_raw=row.title_raw,
        brand=row.brand,
        model_number=row.model_number,
        price=row.current_price,
        mrp=row.mrp,
        currency=row.currency,
        availability=Availability(row.availability),
        condition=Condition(row.condition),
        warranty=row.warranty,
        attributes=VariantAttributes.model_validate(row.attributes or {}),
        observed_at=row.last_observed_at,
    )


def variant_attributes(row: VariantRow) -> VariantAttributes:
    return VariantAttributes.model_validate(row.attributes or {})


class CatalogRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def ensure_product(
        self, brand: str | None, model_number: str | None, name: str, category: str | None
    ) -> str | None:
        b, m = normalize_brand(brand), normalize_model_number(model_number)
        if not b or not m:
            return None
        stmt = (
            pg_insert(ProductRow)
            .values(
                id=new_id(), brand=b, model_number=m, canonical_name=name[:300], category=category
            )
            .on_conflict_do_update(
                index_elements=["brand", "model_number"],
                set_={
                    "category": func.coalesce(ProductRow.category, category),
                    "updated_at": func.now(),
                },
            )
            .returning(ProductRow.id)
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def ensure_variant(
        self,
        product_id: str,
        brand: str | None,
        model_number: str | None,
        attrs: VariantAttributes,
        category: str | None,
    ) -> str | None:
        if category in MEMORY_DEFINED_CATEGORIES and (
            attrs.ram_gb is None or attrs.storage_gb is None
        ):
            return None
        key = identity_key(brand, model_number, attrs)
        if key is None:
            return None
        stmt = (
            pg_insert(VariantRow)
            .values(
                id=new_id(),
                product_id=product_id,
                identity_key=key,
                ram_gb=attrs.ram_gb,
                storage_gb=attrs.storage_gb,
                connectivity=attrs.connectivity.value if attrs.connectivity else None,
                region=attrs.region,
                attributes=attrs.model_dump(mode="json", exclude_none=True),
            )
            .on_conflict_do_update(index_elements=["identity_key"], set_={"updated_at": func.now()})
            .returning(VariantRow.id)
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def upsert_listing(
        self,
        pl: ProviderListing,
        *,
        attrs: VariantAttributes,
        brand: str | None,
        condition: Condition,
        product_id: str | None,
        variant_id: str | None,
    ) -> ListingRow:
        values = {
            "source_key": pl.source_key,
            "product_id": product_id,
            "variant_id": variant_id,
            "marketplace": pl.marketplace.value,
            "external_id": pl.external_id,
            "url": pl.url,
            "title_raw": pl.title,
            "brand": brand,
            "model_number": pl.model_number,
            "category": pl.category,
            "seller_name": pl.seller_name,
            "condition": condition.value,
            "attributes": attrs.model_dump(mode="json", exclude_none=True),
            "specs": dict(pl.specs),
            "offer_texts": list(pl.offer_texts),
            "rating": pl.rating,
            "rating_count": pl.rating_count,
            "current_price": pl.price,
            "mrp": pl.mrp,
            "currency": "INR",
            "availability": pl.availability.value,
            "last_observed_at": pl.observed_at,
            "provider": pl.provider,
            "provider_token": pl.provider_token,
            "source_type": pl.source_type.value,
            "source_name": pl.source_name,
            "verification": pl.verification.value,
        }
        excluded = pg_insert(ListingRow).excluded
        keep_known = {
            name: func.coalesce(getattr(excluded, name), getattr(ListingRow, name))
            for name in (
                "product_id",
                "variant_id",
                "external_id",
                "url",
                "brand",
                "model_number",
                "category",
                "seller_name",
                "rating",
                "rating_count",
                "mrp",
                "provider_token",
            )
        }
        overwrite = {
            name: getattr(excluded, name)
            for name in values
            if name not in keep_known and name != "source_key"
        }
        stmt = (
            pg_insert(ListingRow)
            .values(id=new_id(), **values)
            .on_conflict_do_update(
                index_elements=["source_key"],
                set_={**overwrite, **keep_known, "updated_at": func.now()},
            )
            .returning(ListingRow)
        )
        result = await self.session.scalars(stmt, execution_options={"populate_existing": True})
        return result.one()

    async def get_listing(self, listing_id: str) -> ListingRow | None:
        return await self.session.get(ListingRow, listing_id)

    async def get_variant(self, variant_id: str) -> VariantRow | None:
        return await self.session.get(VariantRow, variant_id)

    async def get_product(self, product_id: str) -> ProductRow | None:
        return await self.session.get(ProductRow, product_id)

    async def find_by_external(
        self, marketplace: Marketplace, external_id: str
    ) -> ListingRow | None:
        stmt = select(ListingRow).where(
            ListingRow.marketplace == marketplace.value,
            func.lower(ListingRow.external_id) == external_id.lower(),
        )
        return (await self.session.scalars(stmt)).first()

    async def listings_for(
        self, *, product_id: str | None = None, variant_id: str | None = None
    ) -> list[ListingRow]:
        stmt = select(ListingRow)
        if variant_id:
            stmt = stmt.where(ListingRow.variant_id == variant_id)
        elif product_id:
            stmt = stmt.where(ListingRow.product_id == product_id)
        else:
            return []
        return list(await self.session.scalars(stmt.order_by(ListingRow.current_price)))
