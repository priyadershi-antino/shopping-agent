"""Evaluation scenarios. One JSON object per line in evals/dataset.jsonl."""

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from shopping_agent.agent.schemas import Intent
from shopping_agent.domain.offer import CardType


class CardSetup(BaseModel):
    issuer: str
    card_type: CardType = CardType.CREDIT


class Setup(BaseModel):
    """State prepared before the request runs."""

    cards: list[CardSetup] = Field(default_factory=list, description="Saved before the run")
    memberships: list[str] = Field(default_factory=list)
    seed_history: bool = Field(default=True, description="Demo seed with synthetic history")
    outage: Literal["products", "web", "all"] | None = None
    previous: str | None = Field(default=None, description="A request run first (follow-ups)")


class GoldenPrice(BaseModel):
    """The best price without extra conditions that the shortlist must show for a listing."""

    marketplace: str
    title_contains: str
    best_eligible: float


class Expect(BaseModel):
    intents: list[Intent] = Field(default_factory=list, description="Any of these is correct")
    category: str | None = None
    budget_max: float | None = None
    issuers: list[str] | None = None  # payment-method issuers the request mentions
    clarification: bool = False
    tools: list[str] = Field(default_factory=list, description="All must be called")
    tools_any: list[str] = Field(default_factory=list, description="At least one must be called")
    top_models: list[str] = Field(
        default_factory=list, description="The top recommendation's title contains one of these"
    )
    top_must_be_new: bool = False
    golden: list[GoldenPrice] = Field(default_factory=list)
    link_resolved: bool | None = None
    no_recommendations: bool = False
    actions: list[str] = Field(default_factory=list, description="Proposed actions expected")
    action_target: float | None = None
    mention_any: list[list[str]] = Field(
        default_factory=list, description="Each group: the answer mentions at least one phrase"
    )
    forbidden: list[str] = Field(
        default_factory=list, description="Must not appear unless marked [unverified]"
    )


class Scenario(BaseModel):
    id: str
    request: str
    tags: list[str] = Field(default_factory=list)
    setup: Setup = Field(default_factory=Setup)
    expect: Expect = Field(default_factory=Expect)


def load_dataset(path: Path) -> list[Scenario]:
    scenarios: list[Scenario] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        try:
            scenarios.append(Scenario.model_validate(json.loads(line)))
        except ValueError as exc:
            raise ValueError(f"{path}:{number}: {exc}") from exc
    ids = [s.id for s in scenarios]
    if len(ids) != len(set(ids)):
        raise ValueError("scenario ids must be unique")
    return scenarios


def select(
    scenarios: list[Scenario], *, only: list[str] | None = None, tags: list[str] | None = None
) -> list[Scenario]:
    chosen = scenarios
    if only:
        wanted = set(only)
        chosen = [s for s in chosen if s.id in wanted]
    if tags:
        chosen = [s for s in chosen if set(tags) & set(s.tags)]
    return chosen
