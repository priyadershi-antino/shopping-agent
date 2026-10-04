"""The user's saved profile: payment methods (bank + card type only), memberships, exchange
devices and preferences. Everything here can be listed, changed and deleted by the user."""

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shopping_agent.db.base import utcnow
from shopping_agent.db.models import (
    ExchangeDeviceRow,
    MembershipRow,
    PaymentMethodRow,
    PreferenceRow,
)
from shopping_agent.domain.evidence import new_id
from shopping_agent.domain.offer import CardType, ExchangeDevice, PaymentMethod, UserProfile

LOCAL_USER = "local"

# Preference keys with a defined meaning. Others are stored as free-form preferences.
KNOWN_PREFERENCES = {
    "prefers_emi": "bool: prefer EMI over full payment",
    "preferred_brands": "list of brands to favour",
    "avoided_brands": "list of brands to avoid",
    "preferred_marketplaces": "list of marketplaces to favour",
    "typical_budget": "number: usual budget in rupees",
    "important_features": "list of features that matter",
}


class ProfileRepository:
    def __init__(self, session: AsyncSession, user_id: str = LOCAL_USER) -> None:
        self.session = session
        self.user_id = user_id

    async def load(self) -> UserProfile:
        methods = await self.payment_methods()
        memberships = {
            r.name
            for r in await self.session.scalars(
                select(MembershipRow).where(MembershipRow.user_id == self.user_id)
            )
        }
        devices = [
            ExchangeDevice(category=r.category, model=r.model, condition=r.condition)
            for r in await self.session.scalars(
                select(ExchangeDeviceRow).where(ExchangeDeviceRow.user_id == self.user_id)
            )
        ]
        prefers_emi = await self.get_preference("prefers_emi")
        return UserProfile(
            payment_methods=methods,
            memberships=memberships,
            exchange_devices=devices,
            prefers_emi=prefers_emi if isinstance(prefers_emi, bool) else None,
        )

    # payment methods

    async def payment_methods(self) -> list[PaymentMethod]:
        rows = await self.session.scalars(
            select(PaymentMethodRow)
            .where(PaymentMethodRow.user_id == self.user_id)
            .order_by(PaymentMethodRow.created_at)
        )
        return [
            PaymentMethod(
                issuer=r.issuer,
                card_type=CardType(r.card_type),
                network=r.network or None,
                label=r.label,
            )
            for r in rows
        ]

    async def add_payment_method(self, method: PaymentMethod) -> None:
        if method.card_type is CardType.ANY:
            raise ValueError("card type must be credit or debit")
        await self.session.execute(
            pg_insert(PaymentMethodRow)
            .values(
                id=new_id(),
                user_id=self.user_id,
                issuer=method.issuer.strip(),
                card_type=method.card_type.value,
                network=(method.network or "").strip().lower(),
                label=method.label,
                created_at=utcnow(),
            )
            .on_conflict_do_nothing()
        )

    async def remove_payment_method(self, issuer: str, card_type: CardType) -> int:
        result = await self.session.execute(
            delete(PaymentMethodRow).where(
                PaymentMethodRow.user_id == self.user_id,
                PaymentMethodRow.issuer.ilike(issuer.strip()),
                PaymentMethodRow.card_type == card_type.value,
            )
        )
        return int(getattr(result, "rowcount", 0) or 0)

    # memberships

    async def add_membership(self, name: str) -> None:
        await self.session.execute(
            pg_insert(MembershipRow)
            .values(id=new_id(), user_id=self.user_id, name=name.strip().lower())
            .on_conflict_do_nothing()
        )

    async def remove_membership(self, name: str) -> int:
        result = await self.session.execute(
            delete(MembershipRow).where(
                MembershipRow.user_id == self.user_id, MembershipRow.name == name.strip().lower()
            )
        )
        return int(getattr(result, "rowcount", 0) or 0)

    # exchange devices

    async def add_exchange_device(self, device: ExchangeDevice) -> None:
        self.session.add(
            ExchangeDeviceRow(
                user_id=self.user_id,
                category=device.category.strip().lower(),
                model=device.model,
                condition=device.condition,
            )
        )
        await self.session.flush()

    async def clear_exchange_devices(self) -> int:
        result = await self.session.execute(
            delete(ExchangeDeviceRow).where(ExchangeDeviceRow.user_id == self.user_id)
        )
        return int(getattr(result, "rowcount", 0) or 0)

    # preferences

    async def set_preference(self, key: str, value: Any) -> None:
        key = key.strip().lower()
        await self.session.execute(
            pg_insert(PreferenceRow)
            .values(id=new_id(), user_id=self.user_id, key=key, value=value, updated_at=utcnow())
            .on_conflict_do_update(
                index_elements=["user_id", "key"], set_={"value": value, "updated_at": utcnow()}
            )
        )

    async def get_preference(self, key: str) -> Any:
        row = await self.session.scalar(
            select(PreferenceRow).where(
                PreferenceRow.user_id == self.user_id, PreferenceRow.key == key.strip().lower()
            )
        )
        return row.value if row else None

    async def preferences(self) -> dict[str, Any]:
        rows = await self.session.scalars(
            select(PreferenceRow)
            .where(PreferenceRow.user_id == self.user_id)
            .order_by(PreferenceRow.key)
        )
        return {r.key: r.value for r in rows}

    async def delete_preference(self, key: str) -> int:
        result = await self.session.execute(
            delete(PreferenceRow).where(
                PreferenceRow.user_id == self.user_id, PreferenceRow.key == key.strip().lower()
            )
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def delete_all(self) -> None:
        """Forget everything stored about the user."""
        for model in (PaymentMethodRow, MembershipRow, ExchangeDeviceRow, PreferenceRow):
            await self.session.execute(delete(model).where(model.user_id == self.user_id))
