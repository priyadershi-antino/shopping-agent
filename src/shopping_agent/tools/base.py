"""Tool contract shared by every tool.

A tool declares typed input/output models, a timeout and whether it has side effects.
It returns a `ToolResult` carrying data, evidence (provenance), errors, and a `partial` flag
so the agent can continue with incomplete results instead of failing.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shopping_agent.domain.evidence import Evidence
from shopping_agent.domain.offer import UserProfile
from shopping_agent.domain.product import Marketplace
from shopping_agent.domain.review import ReviewFindings
from shopping_agent.llm.schema import tool_parameters
from shopping_agent.providers.registry import ProviderRegistry


def utcnow() -> datetime:
    return datetime.now(UTC)


class ReviewAnalyzerFn(Protocol):
    """Optional LLM-backed theme analysis used by `research_reviews`."""

    async def __call__(
        self, output: Any, evidence: list[Evidence], use_case: str | None
    ) -> ReviewFindings | None: ...


@dataclass
class ToolContext:
    providers: ProviderRegistry
    sessions: async_sessionmaker[AsyncSession]
    profile: UserProfile = field(default_factory=UserProfile)
    clock: Callable[[], datetime] = utcnow
    sale_active: bool = False
    run_id: str | None = None
    fetch_max_chars: int = 8_000
    review_analyzer: ReviewAnalyzerFn | None = None
    # Platforms whose listings may be used (None = all). Set per request by the user.
    platforms: frozenset[Marketplace] | None = None

    def allows(self, marketplace: Marketplace | str) -> bool:
        return self.platforms is None or Marketplace(marketplace) in self.platforms


class ToolResult[O: BaseModel](BaseModel):
    data: O | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    partial: bool = False
    cached: bool = False


class Tool[I: BaseModel, O: BaseModel](ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    # Live providers can take 10-20 s per request (with retries), so the default is generous.
    timeout_s: ClassVar[float] = 60.0
    side_effect: ClassVar[bool] = False
    input_model: type[I]
    output_model: type[O]

    @abstractmethod
    async def run(self, ctx: ToolContext, args: I) -> ToolResult[O]: ...

    def parameters_schema(self) -> dict[str, Any]:
        return tool_parameters(self.input_model)
