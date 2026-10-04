"""catalog prices offers evidence sale events

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-03 22:08:15.309221
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "evidence",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=True),
        sa.Column("subject", sa.String(length=64), nullable=True),
        sa.Column("attribute", sa.String(length=80), nullable=True),
        sa.Column("claim", sa.Text(), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("source_type", sa.String(length=40), nullable=False),
        sa.Column("source_name", sa.String(length=200), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verification", sa.String(length=20), nullable=False),
        sa.Column("confidence", sa.String(length=10), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evidence")),
    )
    op.create_index(op.f("ix_evidence_run_id"), "evidence", ["run_id"], unique=False)
    op.create_index(op.f("ix_evidence_subject"), "evidence", ["subject"], unique=False)
    op.create_table(
        "products",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("brand", sa.String(length=80), nullable=False),
        sa.Column("model_number", sa.String(length=80), nullable=False),
        sa.Column("canonical_name", sa.String(length=300), nullable=False),
        sa.Column("category", sa.String(length=60), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_products")),
        sa.UniqueConstraint("brand", "model_number", name=op.f("uq_products_brand")),
    )
    op.create_table(
        "sale_events",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("platform", sa.String(length=30), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("phases", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("partner_banks", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("categories", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("official_sources", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("verification", sa.String(length=20), nullable=False),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sale_events")),
        sa.UniqueConstraint("platform", "name", "start_at", name=op.f("uq_sale_events_platform")),
    )
    op.create_index(op.f("ix_sale_events_platform"), "sale_events", ["platform"], unique=False)
    op.create_table(
        "variants",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("product_id", sa.String(length=32), nullable=False),
        sa.Column("identity_key", sa.String(length=200), nullable=False),
        sa.Column("ram_gb", sa.Integer(), nullable=True),
        sa.Column("storage_gb", sa.Integer(), nullable=True),
        sa.Column("connectivity", sa.String(length=20), nullable=True),
        sa.Column("region", sa.String(length=30), nullable=True),
        sa.Column("attributes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_variants_product_id_products"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_variants")),
        sa.UniqueConstraint("identity_key", name=op.f("uq_variants_identity_key")),
    )
    op.create_index(op.f("ix_variants_product_id"), "variants", ["product_id"], unique=False)
    op.create_table(
        "listings",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("source_key", sa.String(length=250), nullable=False),
        sa.Column("product_id", sa.String(length=32), nullable=True),
        sa.Column("variant_id", sa.String(length=32), nullable=True),
        sa.Column("marketplace", sa.String(length=30), nullable=False),
        sa.Column("external_id", sa.String(length=80), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("title_raw", sa.Text(), nullable=False),
        sa.Column("brand", sa.String(length=80), nullable=True),
        sa.Column("model_number", sa.String(length=80), nullable=True),
        sa.Column("category", sa.String(length=60), nullable=True),
        sa.Column("seller_name", sa.String(length=200), nullable=True),
        sa.Column("condition", sa.String(length=20), nullable=False),
        sa.Column("warranty", sa.String(length=200), nullable=True),
        sa.Column("attributes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("specs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("offer_texts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("rating", sa.Float(), nullable=True),
        sa.Column("rating_count", sa.Integer(), nullable=True),
        sa.Column("current_price", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("mrp", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("availability", sa.String(length=20), nullable=False),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("provider_token", sa.Text(), nullable=True),
        sa.Column("source_type", sa.String(length=40), nullable=False),
        sa.Column("source_name", sa.String(length=200), nullable=False),
        sa.Column("verification", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_listings_product_id_products"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["variant_id"],
            ["variants.id"],
            name=op.f("fk_listings_variant_id_variants"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_listings")),
        sa.UniqueConstraint("source_key", name=op.f("uq_listings_source_key")),
    )
    op.create_index(
        "ix_listings_marketplace_external_id",
        "listings",
        ["marketplace", "external_id"],
        unique=False,
    )
    op.create_index(op.f("ix_listings_product_id"), "listings", ["product_id"], unique=False)
    op.create_index(op.f("ix_listings_variant_id"), "listings", ["variant_id"], unique=False)
    op.create_table(
        "offers",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("type", sa.String(length=20), nullable=False),
        sa.Column("platform", sa.String(length=30), nullable=True),
        sa.Column("provider", sa.String(length=80), nullable=True),
        sa.Column("listing_id", sa.String(length=32), nullable=True),
        sa.Column("variant_id", sa.String(length=32), nullable=True),
        sa.Column("terms", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("verification", sa.String(length=20), nullable=False),
        sa.Column("source", sa.String(length=60), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["listing_id"],
            ["listings.id"],
            name=op.f("fk_offers_listing_id_listings"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["variant_id"],
            ["variants.id"],
            name=op.f("fk_offers_variant_id_variants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_offers")),
        sa.UniqueConstraint("fingerprint", name=op.f("uq_offers_fingerprint")),
    )
    op.create_index(op.f("ix_offers_listing_id"), "offers", ["listing_id"], unique=False)
    op.create_index(op.f("ix_offers_platform"), "offers", ["platform"], unique=False)
    op.create_index(op.f("ix_offers_variant_id"), "offers", ["variant_id"], unique=False)
    op.create_table(
        "price_observations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("listing_id", sa.String(length=32), nullable=False),
        sa.Column("price", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("mrp", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("availability", sa.String(length=20), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["listing_id"],
            ["listings.id"],
            name=op.f("fk_price_observations_listing_id_listings"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_price_observations")),
    )
    op.create_index(
        "ix_price_observations_listing_time",
        "price_observations",
        ["listing_id", "observed_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_price_observations_listing_time", table_name="price_observations")
    op.drop_table("price_observations")
    op.drop_index(op.f("ix_offers_variant_id"), table_name="offers")
    op.drop_index(op.f("ix_offers_platform"), table_name="offers")
    op.drop_index(op.f("ix_offers_listing_id"), table_name="offers")
    op.drop_table("offers")
    op.drop_index(op.f("ix_listings_variant_id"), table_name="listings")
    op.drop_index(op.f("ix_listings_product_id"), table_name="listings")
    op.drop_index("ix_listings_marketplace_external_id", table_name="listings")
    op.drop_table("listings")
    op.drop_index(op.f("ix_variants_product_id"), table_name="variants")
    op.drop_table("variants")
    op.drop_index(op.f("ix_sale_events_platform"), table_name="sale_events")
    op.drop_table("sale_events")
    op.drop_table("products")
    op.drop_index(op.f("ix_evidence_subject"), table_name="evidence")
    op.drop_index(op.f("ix_evidence_run_id"), table_name="evidence")
    op.drop_table("evidence")
