"""user profile

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-04 01:00:45.619358
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "exchange_devices",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("category", sa.String(length=60), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=True),
        sa.Column("condition", sa.String(length=60), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_exchange_devices")),
    )
    op.create_index(
        op.f("ix_exchange_devices_user_id"), "exchange_devices", ["user_id"], unique=False
    )
    op.create_table(
        "memberships",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memberships")),
        sa.UniqueConstraint("user_id", "name", name=op.f("uq_memberships_user_id")),
    )
    op.create_index(op.f("ix_memberships_user_id"), "memberships", ["user_id"], unique=False)
    op.create_table(
        "payment_methods",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("issuer", sa.String(length=80), nullable=False),
        sa.Column("card_type", sa.String(length=10), nullable=False),
        sa.Column("network", sa.String(length=30), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payment_methods")),
        sa.UniqueConstraint(
            "user_id", "issuer", "card_type", "network", name=op.f("uq_payment_methods_user_id")
        ),
    )
    op.create_index(
        op.f("ix_payment_methods_user_id"), "payment_methods", ["user_id"], unique=False
    )
    op.create_table(
        "user_preferences",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_preferences")),
        sa.UniqueConstraint("user_id", "key", name=op.f("uq_user_preferences_user_id")),
    )
    op.create_index(
        op.f("ix_user_preferences_user_id"), "user_preferences", ["user_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_user_preferences_user_id"), table_name="user_preferences")
    op.drop_table("user_preferences")
    op.drop_index(op.f("ix_payment_methods_user_id"), table_name="payment_methods")
    op.drop_table("payment_methods")
    op.drop_index(op.f("ix_memberships_user_id"), table_name="memberships")
    op.drop_table("memberships")
    op.drop_index(op.f("ix_exchange_devices_user_id"), table_name="exchange_devices")
    op.drop_table("exchange_devices")
