"""tracking alerts notifications

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-04 01:51:24.279710
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "watch_items",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("listing_id", sa.String(length=32), nullable=False),
        sa.Column("label", sa.String(length=300), nullable=False),
        sa.Column("priority", sa.String(length=10), nullable=False),
        sa.Column("target_price", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("target_effective", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("target_drop_pct", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("reference_price", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("notify_back_in_stock", sa.Boolean(), nullable=False),
        sa.Column("notify_offer_changes", sa.Boolean(), nullable=False),
        sa.Column("notify_sale_start", sa.Boolean(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("check_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_price", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("last_effective", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("last_availability", sa.String(length=20), nullable=True),
        sa.Column("last_offers", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("monitor_priority", sa.String(length=10), nullable=False),
        sa.ForeignKeyConstraint(
            ["listing_id"],
            ["listings.id"],
            name=op.f("fk_watch_items_listing_id_listings"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_watch_items")),
        sa.UniqueConstraint("user_id", "listing_id", name=op.f("uq_watch_items_user_id")),
    )
    op.create_index("ix_watch_items_due", "watch_items", ["active", "next_check_at"], unique=False)
    op.create_index(op.f("ix_watch_items_user_id"), "watch_items", ["user_id"], unique=False)
    op.create_table(
        "alert_events",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("watch_item_id", sa.String(length=32), nullable=True),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("dedup_key", sa.String(length=250), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["watch_item_id"],
            ["watch_items.id"],
            name=op.f("fk_alert_events_watch_item_id_watch_items"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alert_events")),
        sa.UniqueConstraint("dedup_key", name=op.f("uq_alert_events_dedup_key")),
    )
    op.create_index(
        op.f("ix_alert_events_created_at"), "alert_events", ["created_at"], unique=False
    )
    op.create_index(op.f("ix_alert_events_user_id"), "alert_events", ["user_id"], unique=False)
    op.create_index(
        op.f("ix_alert_events_watch_item_id"), "alert_events", ["watch_item_id"], unique=False
    )
    op.create_table(
        "effective_price_snapshots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("watch_item_id", sa.String(length=32), nullable=False),
        sa.Column("listing_price", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("best_effective", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("scenario", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["watch_item_id"],
            ["watch_items.id"],
            name=op.f("fk_effective_price_snapshots_watch_item_id_watch_items"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_effective_price_snapshots")),
    )
    op.create_index(
        "ix_effective_snapshots_watch_time",
        "effective_price_snapshots",
        ["watch_item_id", "observed_at"],
        unique=False,
    )
    op.create_table(
        "notifications",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("alert_event_id", sa.String(length=32), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["alert_event_id"],
            ["alert_events.id"],
            name=op.f("fk_notifications_alert_event_id_alert_events"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
        sa.UniqueConstraint(
            "alert_event_id", "channel", name=op.f("uq_notifications_alert_event_id")
        ),
    )
    op.create_index(op.f("ix_notifications_status"), "notifications", ["status"], unique=False)
    op.create_index(op.f("ix_notifications_user_id"), "notifications", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_notifications_user_id"), table_name="notifications")
    op.drop_index(op.f("ix_notifications_status"), table_name="notifications")
    op.drop_table("notifications")
    op.drop_index("ix_effective_snapshots_watch_time", table_name="effective_price_snapshots")
    op.drop_table("effective_price_snapshots")
    op.drop_index(op.f("ix_alert_events_watch_item_id"), table_name="alert_events")
    op.drop_index(op.f("ix_alert_events_user_id"), table_name="alert_events")
    op.drop_index(op.f("ix_alert_events_created_at"), table_name="alert_events")
    op.drop_table("alert_events")
    op.drop_index(op.f("ix_watch_items_user_id"), table_name="watch_items")
    op.drop_index("ix_watch_items_due", table_name="watch_items")
    op.drop_table("watch_items")
