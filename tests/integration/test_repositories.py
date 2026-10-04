from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shopping_agent.db.repositories import (
    CatalogRepository,
    EvidenceRepository,
    OfferRepository,
    PriceRepository,
    SaleEventRepository,
)
from shopping_agent.domain.common import Confidence, SourceType, Verification
from shopping_agent.domain.evidence import Evidence
from shopping_agent.domain.product import Availability, Condition, Marketplace, VariantAttributes
from shopping_agent.domain.sale import SaleEvent
from shopping_agent.providers.product.base import ProviderListing
from tests.factories import NOW, bank_offer, coupon

pytestmark = pytest.mark.db

Sessions = async_sessionmaker[AsyncSession]


def provider_listing(**kwargs: object) -> ProviderListing:
    data: dict[str, object] = {
        "provider": "test",
        "source_key": "amazon:B0TEST0001",
        "marketplace": Marketplace.AMAZON,
        "external_id": "B0TEST0001",
        "title": "Acme Nimbus 15 16GB 512GB",
        "brand": "Acme",
        "model_number": "NB15-G4",
        "category": "laptop",
        "price": Decimal("74990"),
        "source_type": SourceType.MARKETPLACE_LISTING,
        "source_name": "test",
        "verification": Verification.SOURCE_REPORTED,
        "observed_at": NOW,
    }
    data.update(kwargs)
    return ProviderListing.model_validate(data)


async def upsert(session: AsyncSession, pl: ProviderListing, attrs: VariantAttributes) -> str:
    repo = CatalogRepository(session)
    product = await repo.ensure_product(pl.brand, pl.model_number, pl.title, pl.category)
    assert product is not None
    variant = await repo.ensure_variant(product, pl.brand, pl.model_number, attrs, pl.category)
    row = await repo.upsert_listing(
        pl,
        attrs=attrs,
        brand=pl.brand,
        condition=Condition.NEW,
        product_id=product,
        variant_id=variant,
    )
    return row.id


async def test_listing_upsert_is_idempotent_and_keeps_known_fields(db: Sessions) -> None:
    attrs = VariantAttributes(ram_gb=16, storage_gb=512)
    async with db() as session, session.begin():
        first = await upsert(session, provider_listing(seller_name="Seller A"), attrs)
    async with db() as session, session.begin():
        second = await upsert(
            session, provider_listing(price=Decimal("73990"), seller_name=None), attrs
        )
    assert first == second
    async with db() as session:
        row = await CatalogRepository(session).get_listing(first)
        assert row is not None
        assert row.current_price == Decimal("73990.00")
        assert row.seller_name == "Seller A"  # unknown in the update; not erased
        assert row.variant_id is not None
        found = await CatalogRepository(session).find_by_external(Marketplace.AMAZON, "b0test0001")
        assert found is not None
        assert found.id == first


async def test_variant_needs_memory_attributes_for_laptops(db: Sessions) -> None:
    async with db() as session, session.begin():
        repo = CatalogRepository(session)
        product = await repo.ensure_product("Acme", "NB15-G4", "x", "laptop")
        assert product is not None
        assert (
            await repo.ensure_variant(product, "Acme", "NB15-G4", VariantAttributes(), "laptop")
            is None
        )
        assert (
            await repo.ensure_variant(product, "Acme", "NB15-G4", VariantAttributes(), "headphones")
            is not None
        )
        assert await repo.ensure_product("Acme", None, "x", "laptop") is None


async def test_price_observations_skip_identical_repeats(db: Sessions) -> None:
    async with db() as session, session.begin():
        listing_id = await upsert(session, provider_listing(), VariantAttributes())
        prices = PriceRepository(session)

        async def record(price: int, after: timedelta) -> bool:
            return await prices.record(
                listing_id,
                price=Decimal(price),
                mrp=None,
                availability=Availability.IN_STOCK,
                source="t",
                observed_at=NOW + after,
            )

        assert await record(74990, timedelta(0))
        assert not await record(74990, timedelta(minutes=5))  # identical repeat
        assert await record(73990, timedelta(minutes=6))  # price changed
        assert await record(73990, timedelta(hours=2))  # same price, later
        history = await prices.history([listing_id], since=NOW - timedelta(days=1))
    assert [o.price for o in history] == [Decimal(74990), Decimal(73990), Decimal(73990)]


async def test_offers_scope_and_validity(db: Sessions) -> None:
    async with db() as session, session.begin():
        listing_id = await upsert(session, provider_listing(), VariantAttributes())
        repo = OfferRepository(session)
        platform_wide = bank_offer(platform=Marketplace.AMAZON)
        await repo.upsert(platform_wide, source="test", now=NOW)
        await repo.upsert(platform_wide, source="test", now=NOW)  # same fingerprint
        await repo.upsert(
            coupon(platform=Marketplace.AMAZON), source="user", now=NOW, listing_id=listing_id
        )
        await repo.upsert(
            coupon("500", platform=Marketplace.AMAZON, valid_to=NOW - timedelta(days=1)),
            source="test",
            now=NOW,
        )
        await repo.upsert(bank_offer("SBI"), source="test", now=NOW)  # flipkart offer
        found = await repo.active_for(platform=Marketplace.AMAZON, now=NOW, listing_id=listing_id)
        other = await repo.active_for(platform=Marketplace.AMAZON, now=NOW, listing_id="nope")
    assert sorted(o.title for o, _ in found) == sorted([platform_wide.title, "Coupon 2000 off"])
    assert [o.title for o, _ in other] == [platform_wide.title]


async def test_evidence_roundtrip(db: Sessions) -> None:
    item = Evidence(
        claim="RAM is 16GB",
        subject="L1",
        attribute="ram_gb",
        value=16,
        source_type=SourceType.MANUFACTURER,
        source_name="Acme",
        retrieved_at=NOW,
        verification=Verification.VERIFIED,
        confidence=Confidence.HIGH,
    )
    async with db() as session, session.begin():
        repo = EvidenceRepository(session)
        await repo.add_many([item], run_id="run1")
        await repo.add_many([item], run_id="run1")  # duplicate ignored
    async with db() as session:
        assert await EvidenceRepository(session).for_subject("L1") == [item]


async def test_sale_events_upsert_and_list(db: Sessions) -> None:
    event = SaleEvent(
        name="Big Sale",
        platform=Marketplace.FLIPKART,
        start=NOW + timedelta(days=5),
        end=NOW + timedelta(days=10),
        partner_banks=["HDFC"],
        verification=Verification.SOURCE_REPORTED,
        last_verified_at=NOW,
    )
    async with db() as session, session.begin():
        repo = SaleEventRepository(session)
        first = await repo.upsert(event)
        second = await repo.upsert(event.model_copy(update={"verification": Verification.VERIFIED}))
    assert first == second
    async with db() as session:
        events = await SaleEventRepository(session).list(platform=Marketplace.FLIPKART)
    assert len(events) == 1
    assert events[0].verification is Verification.VERIFIED
    assert events[0].partner_banks == ["HDFC"]
