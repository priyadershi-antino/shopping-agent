"""Your saved profile: cards (bank + credit/debit only), memberships, exchange devices,
preferences. Everything can be viewed, changed and deleted."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from shopping_agent.api.deps import get_runtime, require_api_key
from shopping_agent.db.repositories.profile import KNOWN_PREFERENCES, ProfileRepository
from shopping_agent.domain.offer import CardType, ExchangeDevice, PaymentMethod, UserProfile
from shopping_agent.runtime import Runtime

router = APIRouter(prefix="/profile", tags=["profile"], dependencies=[Depends(require_api_key)])


class ProfileView(BaseModel):
    profile: UserProfile
    preferences: dict[str, Any]
    known_preferences: dict[str, str] = KNOWN_PREFERENCES


class PaymentMethodIn(BaseModel):
    issuer: str = Field(min_length=2, max_length=80, description="Bank name, e.g. HDFC")
    card_type: CardType
    network: str | None = Field(default=None, max_length=30)
    label: str | None = Field(default=None, max_length=120)


class NameIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)


class PreferenceIn(BaseModel):
    value: Any


async def _view(runtime: Runtime) -> ProfileView:
    async with runtime.sessions() as session:
        repo = ProfileRepository(session)
        return ProfileView(profile=await repo.load(), preferences=await repo.preferences())


@router.get("")
async def get_profile(runtime: Runtime = Depends(get_runtime)) -> ProfileView:
    return await _view(runtime)


@router.delete("")
async def forget_profile(runtime: Runtime = Depends(get_runtime)) -> ProfileView:
    async with runtime.sessions() as session, session.begin():
        await ProfileRepository(session).delete_all()
    return await _view(runtime)


@router.post("/payment-methods")
async def add_payment_method(
    body: PaymentMethodIn, runtime: Runtime = Depends(get_runtime)
) -> ProfileView:
    if body.card_type is CardType.ANY:
        raise HTTPException(400, "card_type must be credit or debit")
    async with runtime.sessions() as session, session.begin():
        await ProfileRepository(session).add_payment_method(
            PaymentMethod(
                issuer=body.issuer, card_type=body.card_type, network=body.network, label=body.label
            )
        )
    return await _view(runtime)


@router.delete("/payment-methods")
async def remove_payment_method(
    issuer: str, card_type: CardType, runtime: Runtime = Depends(get_runtime)
) -> ProfileView:
    async with runtime.sessions() as session, session.begin():
        await ProfileRepository(session).remove_payment_method(issuer, card_type)
    return await _view(runtime)


@router.post("/memberships")
async def add_membership(body: NameIn, runtime: Runtime = Depends(get_runtime)) -> ProfileView:
    async with runtime.sessions() as session, session.begin():
        await ProfileRepository(session).add_membership(body.name)
    return await _view(runtime)


@router.delete("/memberships/{name}")
async def remove_membership(name: str, runtime: Runtime = Depends(get_runtime)) -> ProfileView:
    async with runtime.sessions() as session, session.begin():
        await ProfileRepository(session).remove_membership(name)
    return await _view(runtime)


@router.post("/exchange-devices")
async def add_exchange_device(
    body: ExchangeDevice, runtime: Runtime = Depends(get_runtime)
) -> ProfileView:
    async with runtime.sessions() as session, session.begin():
        await ProfileRepository(session).add_exchange_device(body)
    return await _view(runtime)


@router.delete("/exchange-devices")
async def clear_exchange_devices(runtime: Runtime = Depends(get_runtime)) -> ProfileView:
    async with runtime.sessions() as session, session.begin():
        await ProfileRepository(session).clear_exchange_devices()
    return await _view(runtime)


@router.put("/preferences/{key}")
async def set_preference(
    key: str, body: PreferenceIn, runtime: Runtime = Depends(get_runtime)
) -> ProfileView:
    async with runtime.sessions() as session, session.begin():
        await ProfileRepository(session).set_preference(key, body.value)
    return await _view(runtime)


@router.delete("/preferences/{key}")
async def delete_preference(key: str, runtime: Runtime = Depends(get_runtime)) -> ProfileView:
    async with runtime.sessions() as session, session.begin():
        await ProfileRepository(session).delete_preference(key)
    return await _view(runtime)
