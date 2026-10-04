"""API dependencies: runtime access, optional API-key check, shared response models."""

import hmac
from typing import Any

from fastapi import HTTPException, Request, status
from pydantic import BaseModel, Field

from shopping_agent.domain.evidence import Evidence
from shopping_agent.runtime import Runtime
from shopping_agent.tools.base import ToolResult

API_KEY_HEADER = "X-API-Key"
API_KEY_COOKIE = "sa_key"


def get_runtime(request: Request) -> Runtime:
    runtime: Runtime = request.app.state.runtime
    return runtime


def require_api_key(request: Request) -> None:
    """When API_KEY is set, require it as a header or cookie (constant-time comparison)."""
    expected = get_runtime(request).settings.api_key.get_secret_value()
    if not expected:
        return
    supplied = request.headers.get(API_KEY_HEADER) or request.cookies.get(API_KEY_COOKIE) or ""
    if not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing or invalid API key")


class ToolResponse(BaseModel):
    data: Any | None = None
    errors: list[str] = Field(default_factory=list)
    partial: bool = False
    evidence: list[Evidence] = Field(default_factory=list)

    @classmethod
    def of(cls, result: ToolResult[Any]) -> "ToolResponse":
        return cls(
            data=result.data.model_dump(mode="json") if result.data is not None else None,
            errors=result.errors,
            partial=result.partial,
            evidence=result.evidence,
        )
