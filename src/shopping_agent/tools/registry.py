"""Tool registry: validation, permissions, timeouts, error containment and tracing."""

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.exc import DBAPIError

from shopping_agent.domain.evidence import new_id
from shopping_agent.errors import ShoppingAgentError
from shopping_agent.tools.base import Tool, ToolContext, ToolResult, utcnow

log = logging.getLogger(__name__)

CallStatus = Literal["ok", "partial", "error", "invalid", "denied", "timeout", "unknown_tool"]


class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]
    side_effect: bool


class ToolCallRecord(BaseModel):
    id: str = Field(default_factory=new_id)
    tool: str
    args: dict[str, Any]
    status: CallStatus
    latency_ms: int
    error: str | None = None
    cached: bool = False
    evidence_count: int = 0
    started_at: datetime


class ToolRegistry:
    def __init__(self, tools: list[Tool[Any, Any]]) -> None:
        self._tools: dict[str, Tool[Any, Any]] = {}
        for tool in tools:
            if tool.name in self._tools:
                raise ValueError(f"duplicate tool name {tool.name}")
            self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return list(self._tools)

    def get(self, name: str) -> Tool[Any, Any] | None:
        return self._tools.get(name)

    def specs(self, *, include_side_effects: bool = False) -> list[ToolSpec]:
        return [
            ToolSpec(
                name=t.name,
                description=t.description,
                parameters=t.parameters_schema(),
                side_effect=t.side_effect,
            )
            for t in self._tools.values()
            if include_side_effects or not t.side_effect
        ]

    async def execute(
        self,
        name: str,
        raw_args: dict[str, Any],
        ctx: ToolContext,
        *,
        allow_side_effects: bool = False,
    ) -> tuple[ToolResult[Any], ToolCallRecord]:
        started = utcnow()
        t0 = time.perf_counter()

        def finish(
            status: CallStatus, result: ToolResult[Any], error: str | None = None
        ) -> tuple[ToolResult[Any], ToolCallRecord]:
            record = ToolCallRecord(
                tool=name,
                args=raw_args,
                status=status,
                latency_ms=int((time.perf_counter() - t0) * 1000),
                error=error,
                cached=result.cached,
                evidence_count=len(result.evidence),
                started_at=started,
            )
            log.info(
                "tool_call",
                extra={
                    "run_id": ctx.run_id,
                    "tool": name,
                    "status": status,
                    "latency_ms": record.latency_ms,
                    "cached": record.cached,
                    "evidence": record.evidence_count,
                },
            )
            return result, record

        tool = self._tools.get(name)
        if tool is None:
            msg = f"unknown tool '{name}'"
            return finish("unknown_tool", ToolResult(errors=[msg]), msg)
        if tool.side_effect and not allow_side_effects:
            msg = f"'{name}' changes saved data and needs the user's confirmation"
            return finish("denied", ToolResult(errors=[msg]), msg)
        try:
            args = tool.input_model.model_validate(raw_args)
        except ValidationError as exc:
            msg = "invalid arguments: " + "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'input'}: {e['msg']}" for e in exc.errors()
            )
            return finish("invalid", ToolResult(errors=[msg]), msg)
        try:
            async with asyncio.timeout(tool.timeout_s):
                try:
                    result = await tool.run(ctx, args)
                except DBAPIError as exc:
                    if "deadlock" not in str(exc).lower():
                        raise
                    log.warning("tool %s hit a database deadlock; retrying once", name)
                    result = await tool.run(ctx, args)  # tool writes are idempotent upserts
        except TimeoutError:
            msg = f"'{name}' timed out after {tool.timeout_s:.0f}s"
            return finish("timeout", ToolResult(errors=[msg], partial=True), msg)
        except ShoppingAgentError as exc:
            return finish("error", ToolResult(errors=[str(exc)]), str(exc))
        except Exception:
            log.exception("tool %s crashed", name)
            msg = f"'{name}' failed with an internal error"
            return finish("error", ToolResult(errors=[msg]), msg)
        status: CallStatus = "partial" if result.partial or result.errors else "ok"
        return finish(status, result)


def result_for_model(result: ToolResult[Any], *, max_evidence: int = 40) -> str:
    """Compact JSON for an LLM: data, errors, and evidence ids/claims for citation."""
    payload: dict[str, Any] = {
        "data": result.data.model_dump(mode="json", exclude_none=True) if result.data else None,
        "partial": result.partial,
    }
    if result.errors:
        payload["errors"] = result.errors
    if result.evidence:
        payload["evidence"] = [
            {"id": e.id, "claim": e.claim, "source": e.source_name, "verification": e.verification}
            for e in result.evidence[:max_evidence]
        ]
    return json.dumps(payload, ensure_ascii=False, default=str)
