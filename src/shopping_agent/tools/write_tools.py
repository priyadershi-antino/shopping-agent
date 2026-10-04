"""Tools that change saved data. The agent can only *propose* them; they run after the user
confirms (`allow_side_effects=True`), through the API, CLI or web UI."""

import re
from decimal import Decimal
from typing import Any, Self

from pydantic import BaseModel, Field, model_validator

from shopping_agent.db.repositories.profile import ProfileRepository
from shopping_agent.db.repositories.tracking import WatchRepository
from shopping_agent.errors import ToolInputError
from shopping_agent.services.monitor_priority import UserPriority
from shopping_agent.tools.base import Tool, ToolContext, ToolResult
from shopping_agent.tools.common import load_listing, short_name, summarize
from shopping_agent.tracking.views import WatchView

SENSITIVE_KEY = re.compile(r"card.?(no|num)|cvv|cvc|otp|pin|password|passcode|upi|netbank", re.I)
LONG_DIGITS = re.compile(r"\d[\d\s-]{10,}\d")


class TrackProductInput(BaseModel):
    listing_id: str
    target_price: Decimal | None = Field(default=None, gt=0, description="Listed price target")
    target_effective: Decimal | None = Field(
        default=None, gt=0, description="Target for the best price with the user's own cards"
    )
    target_drop_pct: Decimal | None = Field(default=None, gt=0, lt=90)
    priority: UserPriority = UserPriority.NORMAL


class TrackProductTool(Tool[TrackProductInput, WatchView]):
    name = "track_product"
    description = (
        "Start tracking a listing's price (and the best price for the user's cards), with "
        "optional targets. Needs the user's confirmation."
    )
    side_effect = True
    input_model = TrackProductInput
    output_model = WatchView

    async def run(self, ctx: ToolContext, args: TrackProductInput) -> ToolResult[WatchView]:
        async with ctx.sessions() as session, session.begin():
            row = await load_listing(session, args.listing_id)
            watch = await WatchRepository(session).upsert(
                listing_id=row.id,
                label=short_name(row.title_raw),
                reference_price=row.current_price,
                now=ctx.clock(),
                priority=args.priority.value,
                target_price=args.target_price,
                target_effective=args.target_effective,
                target_drop_pct=args.target_drop_pct,
            )
            view = WatchView.of(watch, summarize(row, ctx))
        return ToolResult(data=view)


class PriceAlertInput(BaseModel):
    listing_id: str
    target_effective: Decimal | None = Field(
        default=None,
        gt=0,
        description="Alert when the best price for the user's cards is at or below",
    )
    target_price: Decimal | None = Field(
        default=None, gt=0, description="Alert when the listed price is at or below"
    )

    @model_validator(mode="after")
    def _one_target(self) -> Self:
        if self.target_effective is None and self.target_price is None:
            raise ValueError("give target_effective or target_price")
        return self


class CreatePriceAlertTool(Tool[PriceAlertInput, WatchView]):
    name = "create_price_alert"
    description = (
        "Alert the user when a listing's price (or the best price for their cards) reaches a "
        "target. Tracks the listing if it isn't tracked yet. Needs the user's confirmation."
    )
    side_effect = True
    input_model = PriceAlertInput
    output_model = WatchView

    async def run(self, ctx: ToolContext, args: PriceAlertInput) -> ToolResult[WatchView]:
        return await TrackProductTool().run(
            ctx,
            TrackProductInput(
                listing_id=args.listing_id,
                target_effective=args.target_effective,
                target_price=args.target_price,
            ),
        )


class PreferenceInput(BaseModel):
    key: str = Field(min_length=2, max_length=80, pattern=r"^[A-Za-z][A-Za-z0-9_ ]*$")
    value: Any


class PreferenceOutput(BaseModel):
    key: str
    value: Any


class SaveUserPreferenceTool(Tool[PreferenceInput, PreferenceOutput]):
    name = "save_user_preference"
    description = (
        "Save a shopping preference (e.g. preferred_brands, typical_budget). Never card "
        "numbers, CVV, OTP, PINs or passwords. Needs the user's confirmation."
    )
    side_effect = True
    input_model = PreferenceInput
    output_model = PreferenceOutput

    async def run(self, ctx: ToolContext, args: PreferenceInput) -> ToolResult[PreferenceOutput]:
        key = args.key.strip().lower().replace(" ", "_")
        if SENSITIVE_KEY.search(key) or LONG_DIGITS.search(str(args.value)):
            raise ToolInputError("this looks like payment or login data, which is never stored")
        async with ctx.sessions() as session, session.begin():
            await ProfileRepository(session).set_preference(key, args.value)
        return ToolResult(data=PreferenceOutput(key=key, value=args.value))


def write_tools() -> list[Tool[Any, Any]]:
    return [TrackProductTool(), CreatePriceAlertTool(), SaveUserPreferenceTool()]
