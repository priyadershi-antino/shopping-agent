"""Ask the agent (optionally streamed as Server-Sent Events) and read past runs."""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from shopping_agent.agent.orchestrator import AgentEvent
from shopping_agent.agent.store import SqlRunStore
from shopping_agent.agent.views import RunView, follow_up, run_view
from shopping_agent.api.deps import get_runtime, require_api_key
from shopping_agent.db.models import AgentRunRow
from shopping_agent.domain.product import parse_marketplaces
from shopping_agent.runtime import Runtime

router = APIRouter(tags=["agent"], dependencies=[Depends(require_api_key)])


class ChatRequest(BaseModel):
    message: str = Field(min_length=2, max_length=4000)
    previous_run_id: str | None = Field(
        default=None,
        description="Continue from an earlier run (its request and answer are used as context)",
    )
    stream: bool = Field(default=True, description="Stream progress as Server-Sent Events")
    platforms: list[str] | None = Field(
        default=None,
        description=(
            "Use listings from these platforms only (amazon, flipkart, croma, reliance_digital, "
            "vijay_sales, tata_cliq, other). Default: the server's DEFAULT_PLATFORMS."
        ),
    )

    @field_validator("platforms")
    @classmethod
    def _known_platforms(cls, value: list[str] | None) -> list[str] | None:
        parse_marketplaces(value or [])  # raises ValueError for unknown names
        return value


class RunSummary(BaseModel):
    run_id: str
    status: str
    intent: str | None
    request: str
    demo_data: bool
    created_at: str
    summary: dict[str, Any]


def sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@router.post("/chat", response_model=None)
async def chat(body: ChatRequest, request: Request) -> RunView | StreamingResponse:
    runtime = get_runtime(request)
    context, carry = None, list[str]()
    if body.previous_run_id:
        previous = await SqlRunStore(runtime.sessions).load(body.previous_run_id)
        if previous is None:
            raise HTTPException(404, "previous run not found")
        context, carry = follow_up(previous)
    agent = runtime.agent()
    profile = await runtime.load_profile()

    if not body.stream:
        state = await agent.run(
            body.message,
            profile=profile,
            context=context,
            carry_listing_ids=carry,
            platforms=body.platforms,
        )
        return run_view(state)

    queue: asyncio.Queue[tuple[str, Any] | None] = asyncio.Queue()

    async def on_event(event: AgentEvent) -> None:
        await queue.put((event.type, event.model_dump(mode="json")))

    async def run() -> None:
        try:
            state = await agent.run(
                body.message,
                profile=profile,
                context=context,
                carry_listing_ids=carry,
                platforms=body.platforms,
                on_event=on_event,
            )
            await queue.put(("final", run_view(state).model_dump(mode="json")))
        finally:
            await queue.put(None)

    task = asyncio.create_task(run())
    # Keep a reference: the run continues (and is saved) even if the client disconnects.
    background: set[asyncio.Task[None]] = request.app.state.background
    background.add(task)
    task.add_done_callback(background.discard)

    async def stream() -> AsyncIterator[str]:
        while (item := await queue.get()) is not None:
            yield sse(*item)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/runs")
async def list_runs(limit: int = 20, runtime: Runtime = Depends(get_runtime)) -> list[RunSummary]:
    async with runtime.sessions() as session:
        rows = await session.scalars(
            select(AgentRunRow).order_by(AgentRunRow.created_at.desc()).limit(min(limit, 100))
        )
        return [
            RunSummary(
                run_id=r.id,
                status=r.status,
                intent=r.intent,
                request=r.request,
                demo_data=r.demo_data,
                created_at=r.created_at.isoformat(),
                summary=r.summary,
            )
            for r in rows
        ]


@router.get("/runs/{run_id}")
async def get_run(run_id: str, runtime: Runtime = Depends(get_runtime)) -> RunView:
    state = await SqlRunStore(runtime.sessions).load(run_id)
    if state is None:
        raise HTTPException(404, "run not found")
    return run_view(state)
