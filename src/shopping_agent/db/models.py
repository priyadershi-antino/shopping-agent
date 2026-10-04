"""ORM models. Tables are added milestone by milestone, each with its own migration."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from shopping_agent.db.base import Base, Timestamps, UUIDPk, utcnow
from shopping_agent.domain.evidence import new_id

Money = Numeric(12, 2)


class User(UUIDPk, Timestamps, Base):
    __tablename__ = "users"

    display_name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str | None] = mapped_column(String(320), unique=True)


# --- Catalog ---


class ProductRow(Timestamps, Base):
    """A product model (brand + model number). Variants hold RAM/storage/connectivity."""

    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("brand", "model_number"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    brand: Mapped[str] = mapped_column(String(80))  # normalized
    model_number: Mapped[str] = mapped_column(String(80))  # normalized
    canonical_name: Mapped[str] = mapped_column(String(300))
    category: Mapped[str | None] = mapped_column(String(60))


class VariantRow(Timestamps, Base):
    __tablename__ = "variants"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    product_id: Mapped[str] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    identity_key: Mapped[str] = mapped_column(String(200), unique=True)
    ram_gb: Mapped[int | None] = mapped_column(Integer)
    storage_gb: Mapped[int | None] = mapped_column(Integer)
    connectivity: Mapped[str | None] = mapped_column(String(20))
    region: Mapped[str | None] = mapped_column(String(30))
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class ListingRow(Timestamps, Base):
    """One seller's offer to sell on one marketplace, with its latest observed price."""

    __tablename__ = "listings"
    __table_args__ = (Index("ix_listings_marketplace_external_id", "marketplace", "external_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source_key: Mapped[str] = mapped_column(String(250), unique=True)
    product_id: Mapped[str | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), index=True
    )
    variant_id: Mapped[str | None] = mapped_column(
        ForeignKey("variants.id", ondelete="SET NULL"), index=True
    )
    marketplace: Mapped[str] = mapped_column(String(30))
    external_id: Mapped[str | None] = mapped_column(String(80))
    url: Mapped[str | None] = mapped_column(Text)
    title_raw: Mapped[str] = mapped_column(Text)
    brand: Mapped[str | None] = mapped_column(String(80))
    model_number: Mapped[str | None] = mapped_column(String(80))
    category: Mapped[str | None] = mapped_column(String(60))
    seller_name: Mapped[str | None] = mapped_column(String(200))
    condition: Mapped[str] = mapped_column(String(20))
    warranty: Mapped[str | None] = mapped_column(String(200))
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    specs: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    offer_texts: Mapped[list[str]] = mapped_column(JSONB, default=list)  # raw, untrusted
    rating: Mapped[float | None] = mapped_column(Float)
    rating_count: Mapped[int | None] = mapped_column(Integer)
    current_price: Mapped[Decimal] = mapped_column(Money)
    mrp: Mapped[Decimal | None] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    availability: Mapped[str] = mapped_column(String(20))
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    provider: Mapped[str] = mapped_column(String(40))
    provider_token: Mapped[str | None] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(40))
    source_name: Mapped[str] = mapped_column(String(200))
    verification: Mapped[str] = mapped_column(String(20))


class PriceObservationRow(Base):
    __tablename__ = "price_observations"
    __table_args__ = (Index("ix_price_observations_listing_time", "listing_id", "observed_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    listing_id: Mapped[str] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"))
    price: Mapped[Decimal] = mapped_column(Money)
    mrp: Mapped[Decimal | None] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    availability: Mapped[str] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(40))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# --- Offers, evidence, sale events ---


class OfferRow(Base):
    """An offer with its full structured terms. Scope: platform-wide, or one listing/variant."""

    __tablename__ = "offers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    type: Mapped[str] = mapped_column(String(20))
    platform: Mapped[str | None] = mapped_column(String(30), index=True)
    provider: Mapped[str | None] = mapped_column(String(80))
    listing_id: Mapped[str | None] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), index=True
    )
    variant_id: Mapped[str | None] = mapped_column(
        ForeignKey("variants.id", ondelete="CASCADE"), index=True
    )
    terms: Mapped[dict[str, Any]] = mapped_column(JSONB)
    verification: Mapped[str] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(60))
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class EvidenceRow(Base):
    __tablename__ = "evidence"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(String(32), index=True)
    subject: Mapped[str | None] = mapped_column(String(64), index=True)
    attribute: Mapped[str | None] = mapped_column(String(80))
    claim: Mapped[str] = mapped_column(Text)
    value: Mapped[Any] = mapped_column(JSONB, nullable=True)
    source_type: Mapped[str] = mapped_column(String(40))
    source_name: Mapped[str] = mapped_column(String(200))
    url: Mapped[str | None] = mapped_column(Text)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    verification: Mapped[str] = mapped_column(String(20))
    confidence: Mapped[str] = mapped_column(String(10))


# --- User profile (high-level eligibility only: never card numbers or credentials) ---


class PaymentMethodRow(Base):
    __tablename__ = "payment_methods"
    __table_args__ = (UniqueConstraint("user_id", "issuer", "card_type", "network"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    issuer: Mapped[str] = mapped_column(String(80))
    card_type: Mapped[str] = mapped_column(String(10))
    network: Mapped[str] = mapped_column(String(30), default="")
    label: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MembershipRow(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("user_id", "name"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(80))


class ExchangeDeviceRow(Base):
    __tablename__ = "exchange_devices"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    category: Mapped[str] = mapped_column(String(60))
    model: Mapped[str | None] = mapped_column(String(120))
    condition: Mapped[str | None] = mapped_column(String(60))


class PreferenceRow(Base):
    __tablename__ = "user_preferences"
    __table_args__ = (UniqueConstraint("user_id", "key"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    key: Mapped[str] = mapped_column(String(80))
    value: Mapped[Any] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AgentRunRow(Base):
    """One agent run: structured state and usage for tracing, debugging and evaluation."""

    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    status: Mapped[str] = mapped_column(String(30), index=True)
    intent: Mapped[str | None] = mapped_column(String(30))
    request: Mapped[str] = mapped_column(Text)
    demo_data: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    state: Mapped[dict[str, Any]] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)


class ToolCallRow(Base):
    __tablename__ = "tool_calls"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True)
    tool: Mapped[str] = mapped_column(String(60))
    args: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20))
    latency_ms: Mapped[int] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    cached: Mapped[bool] = mapped_column(Boolean, default=False)
    evidence_count: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SaleEventRow(Timestamps, Base):
    __tablename__ = "sale_events"
    __table_args__ = (UniqueConstraint("platform", "name", "start_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    platform: Mapped[str] = mapped_column(String(30), index=True)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    phases: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    partner_banks: Mapped[list[str]] = mapped_column(JSONB, default=list)
    categories: Mapped[list[str]] = mapped_column(JSONB, default=list)
    official_sources: Mapped[list[str]] = mapped_column(JSONB, default=list)
    verification: Mapped[str] = mapped_column(String(20))
    last_verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# --- Tracking, alerts and notifications (M6) ---


class WatchItemRow(Base):
    """A listing the user tracks, with targets and the state of the last check."""

    __tablename__ = "watch_items"
    __table_args__ = (
        UniqueConstraint("user_id", "listing_id"),
        Index("ix_watch_items_due", "active", "next_check_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    listing_id: Mapped[str] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"))
    label: Mapped[str] = mapped_column(String(300))
    priority: Mapped[str] = mapped_column(String(10), default="normal")  # user priority
    target_price: Mapped[Decimal | None] = mapped_column(Money)  # listed price at or below
    target_effective: Mapped[Decimal | None] = mapped_column(Money)  # best price for my cards
    target_drop_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))  # vs reference
    reference_price: Mapped[Decimal] = mapped_column(Money)  # listed price when tracking began
    notify_back_in_stock: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_offer_changes: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_sale_start: Mapped[bool] = mapped_column(Boolean, default=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_check_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    check_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    # Snapshot of the last check, compared with the next one to detect changes.
    last_price: Mapped[Decimal | None] = mapped_column(Money)
    last_effective: Mapped[Decimal | None] = mapped_column(Money)
    last_availability: Mapped[str | None] = mapped_column(String(20))
    last_offers: Mapped[dict[str, str]] = mapped_column(JSONB, default=dict)
    monitor_priority: Mapped[str] = mapped_column(String(10), default="normal")


class EffectivePriceSnapshotRow(Base):
    """Best price for the user's own cards at each check: effective-price history."""

    __tablename__ = "effective_price_snapshots"
    __table_args__ = (Index("ix_effective_snapshots_watch_time", "watch_item_id", "observed_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    watch_item_id: Mapped[str] = mapped_column(ForeignKey("watch_items.id", ondelete="CASCADE"))
    listing_price: Mapped[Decimal] = mapped_column(Money)
    best_effective: Mapped[Decimal] = mapped_column(Money)
    scenario: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AlertEventRow(Base):
    """Something worth telling the user. `dedup_key` makes each event fire once."""

    __tablename__ = "alert_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    watch_item_id: Mapped[str | None] = mapped_column(
        ForeignKey("watch_items.id", ondelete="SET NULL"), index=True
    )
    kind: Mapped[str] = mapped_column(String(30))
    dedup_key: Mapped[str] = mapped_column(String(250), unique=True)
    title: Mapped[str] = mapped_column(String(300))
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class NotificationRow(Base):
    """Delivery of an alert on one channel (in_app, email, telegram)."""

    __tablename__ = "notifications"
    __table_args__ = (UniqueConstraint("alert_event_id", "channel"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    alert_event_id: Mapped[str] = mapped_column(ForeignKey("alert_events.id", ondelete="CASCADE"))
    channel: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(10), index=True)  # pending, sent, failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
