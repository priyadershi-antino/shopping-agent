"""Confirm or dismiss an action the agent proposed. Confirmation is the only path by which a
proposal turns into saved data: the matching write tool runs with `allow_side_effects=True`."""

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from shopping_agent.agent.state import ProposedAction
from shopping_agent.agent.store import SqlRunStore
from shopping_agent.errors import ToolInputError
from shopping_agent.runtime import Runtime


class ConfirmOverrides(BaseModel):
    """Optional changes the user makes while confirming."""

    target_price: Decimal | None = Field(default=None, gt=0)
    key: str | None = Field(default=None, max_length=80)
    value: Any = None


class ActionNotFoundError(ToolInputError):
    pass


async def _load(rt: Runtime, run_id: str, action_id: str) -> tuple[Any, ProposedAction]:
    store = SqlRunStore(rt.sessions)
    state = await store.load(run_id)
    if state is None:
        raise ActionNotFoundError(f"run {run_id} not found")
    action = next((a for a in state.proposed_actions if a.id == action_id), None)
    if action is None:
        raise ActionNotFoundError(f"action {action_id} not found in run {run_id}")
    if action.status != "pending":
        raise ToolInputError(f"this action was already {action.status}")
    return state, action


def tool_call_for(
    action: ProposedAction, overrides: ConfirmOverrides
) -> tuple[str, dict[str, Any]]:
    target = overrides.target_price or action.target_price
    if action.action == "track_price":
        args: dict[str, Any] = {"listing_id": action.listing_id}
        if target:
            args["target_effective"] = str(target)
        return "track_product", args
    if action.action == "price_alert":
        if not target:
            raise ToolInputError("a price alert needs a target price")
        return "create_price_alert", {
            "listing_id": action.listing_id,
            "target_effective": str(target),
        }
    if not overrides.key:
        raise ToolInputError("say which preference to save (key and value)")
    return "save_user_preference", {"key": overrides.key, "value": overrides.value}


async def confirm_action(
    rt: Runtime, run_id: str, action_id: str, overrides: ConfirmOverrides | None = None
) -> tuple[ProposedAction, Any]:
    state, action = await _load(rt, run_id, action_id)
    tool, args = tool_call_for(action, overrides or ConfirmOverrides())
    profile = await rt.load_profile()
    result = await rt.call_tool(tool, args, profile=profile, allow_side_effects=True)
    action.status = "confirmed"
    await SqlRunStore(rt.sessions).save(state)
    return action, result.data


async def dismiss_action(rt: Runtime, run_id: str, action_id: str) -> ProposedAction:
    state, action = await _load(rt, run_id, action_id)
    action.status = "dismissed"
    await SqlRunStore(rt.sessions).save(state)
    return action
