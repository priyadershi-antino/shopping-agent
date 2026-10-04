from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

from shopping_agent.domain.common import INR


class Budget(BaseModel):
    currency: str = INR
    minimum: Decimal | None = None
    maximum: Decimal | None = None
    target: Decimal | None = None  # "around 80k"
    approximate: bool = False


class ConstraintOp(StrEnum):
    EQ = "eq"
    GTE = "gte"
    LTE = "lte"
    IN = "in"
    NOT_IN = "not_in"
    CONTAINS = "contains"
    QUALITATIVE = "qualitative"  # e.g. "good battery" — not machine-checkable


ConstraintValue = str | int | float | bool | list[str]


class Constraint(BaseModel):
    attribute: str  # e.g. "ram_gb", "brand", "battery"
    op: ConstraintOp
    value: ConstraintValue
    note: str | None = None


class Requirements(BaseModel):
    category: str | None = None
    budget: Budget = Field(default_factory=Budget)
    use_cases: list[str] = Field(default_factory=list)
    hard_constraints: list[Constraint] = Field(default_factory=list)
    strong_preferences: list[Constraint] = Field(default_factory=list)
    nice_to_have: list[Constraint] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
