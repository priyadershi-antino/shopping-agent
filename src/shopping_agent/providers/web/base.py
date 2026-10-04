from datetime import datetime
from typing import Protocol

from pydantic import BaseModel


class WebResult(BaseModel):
    title: str
    url: str
    snippet: str  # untrusted
    published_at: datetime | None = None
    score: float | None = None
    provider: str


class WebSearchProvider(Protocol):
    name: str

    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        include_domains: list[str] | None = None,
        recent_days: int | None = None,
    ) -> list[WebResult]: ...
