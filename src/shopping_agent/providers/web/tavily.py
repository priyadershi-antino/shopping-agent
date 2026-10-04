"""Tavily web search."""

from datetime import datetime
from typing import Any

from shopping_agent.providers.http import ProviderHttp
from shopping_agent.providers.web.base import WebResult

TAVILY_SEARCH_URL = "https://api.tavily.com/search"


def _parse_date(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _time_range(days: int | None) -> str | None:
    if days is None:
        return None
    if days <= 1:
        return "day"
    if days <= 7:
        return "week"
    if days <= 31:
        return "month"
    return "year"


class TavilySearchProvider:
    name = "tavily"

    def __init__(self, http: ProviderHttp, api_key: str) -> None:
        self._http = http
        self._api_key = api_key

    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        include_domains: list[str] | None = None,
        recent_days: int | None = None,
    ) -> list[WebResult]:
        body: dict[str, Any] = {
            "query": query,
            "max_results": max(1, min(max_results, 20)),
            "search_depth": "basic",
            "country": "india",
            "include_published_date": True,
        }
        if include_domains:
            body["include_domains"] = include_domains
        if time_range := _time_range(recent_days):
            body["time_range"] = time_range
        data = await self._http.request_json(
            "POST",
            TAVILY_SEARCH_URL,
            json=body,
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
        results: list[WebResult] = []
        for item in (data or {}).get("results") or []:
            if not isinstance(item, dict) or not item.get("url"):
                continue
            results.append(
                WebResult(
                    title=str(item.get("title") or item["url"]),
                    url=str(item["url"]),
                    snippet=str(item.get("content") or ""),
                    published_at=_parse_date(item.get("published_date")),
                    score=item.get("score") if isinstance(item.get("score"), int | float) else None,
                    provider=self.name,
                )
            )
        return results
