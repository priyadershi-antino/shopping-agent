"""Safe page fetcher: allowlist + public-IP check on every hop, size and type limits."""

from collections.abc import Iterable
from datetime import UTC, datetime
from urllib.parse import urljoin

import httpx
from pydantic import BaseModel

from shopping_agent.errors import ProviderError, UnsafeUrlError
from shopping_agent.providers.web.html_text import html_to_text
from shopping_agent.security.url_guard import (
    Resolver,
    check_resolves_public,
    check_url,
    system_resolver,
)

_TEXT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
MAX_REDIRECTS = 3


class FetchedPage(BaseModel):
    url: str
    final_url: str
    title: str
    text: str
    fetched_at: datetime


class SafeFetcher:
    def __init__(
        self,
        client: httpx.AsyncClient,
        allowlist: Iterable[str],
        *,
        max_bytes: int = 2_000_000,
        resolver: Resolver = system_resolver,
    ) -> None:
        self._client = client
        self._allowlist = frozenset(d.lower() for d in allowlist)
        self._max_bytes = max_bytes
        self._resolver = resolver

    @property
    def allowlist(self) -> frozenset[str]:
        return self._allowlist

    async def fetch(self, url: str) -> FetchedPage:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            parts = check_url(current, self._allowlist)
            await check_resolves_public(parts.hostname or "", self._resolver)
            try:
                async with self._client.stream(
                    "GET",
                    current,
                    follow_redirects=False,
                    headers={"Accept": "text/html,text/plain;q=0.9"},
                ) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise ProviderError("fetch", "redirect without location")
                        current = urljoin(current, location)
                        continue
                    if response.status_code >= 400:
                        raise ProviderError(
                            "fetch",
                            f"HTTP {response.status_code}",
                            transient=response.status_code >= 500,
                        )
                    content_type = response.headers.get("content-type", "").lower()
                    if not content_type.startswith(_TEXT_TYPES):
                        raise UnsafeUrlError(f"unsupported content type {content_type or '?'}")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > self._max_bytes:
                            raise UnsafeUrlError("page is too large")
                    encoding = response.encoding or "utf-8"
            except httpx.TimeoutException as exc:
                raise ProviderError("fetch", "timed out", transient=True) from exc
            except httpx.HTTPError as exc:
                raise ProviderError("fetch", type(exc).__name__, transient=True) from exc

            raw = body.decode(encoding, errors="replace")
            if content_type.startswith("text/plain"):
                title, text = "", raw
            else:
                title, text = html_to_text(raw)
            return FetchedPage(
                url=url, final_url=current, title=title, text=text, fetched_at=datetime.now(UTC)
            )
        raise UnsafeUrlError("too many redirects")
