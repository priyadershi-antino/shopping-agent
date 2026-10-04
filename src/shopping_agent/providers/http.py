"""HTTP helper for provider APIs: error mapping and retries for transient failures.

Error messages never include request URLs (some providers put API keys in query strings).
"""

from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from shopping_agent.errors import ProviderError, RateLimitedError


def _transient(exc: BaseException) -> bool:
    return isinstance(exc, ProviderError) and exc.transient


class ProviderHttp:
    def __init__(
        self,
        provider: str,
        client: httpx.AsyncClient,
        *,
        attempts: int = 3,
        max_wait_s: float = 4.0,
        timeout: httpx.Timeout | None = None,
        retry_timeouts: bool = True,
    ) -> None:
        self.provider = provider
        self._client = client
        self._attempts = attempts
        self._max_wait_s = max_wait_s
        # Overrides the shared client's timeout (LLM calls are far slower than data lookups).
        self._timeout = timeout
        self._retry_timeouts = retry_timeouts

    def _raise_for(self, response: httpx.Response) -> None:
        status = response.status_code
        if status == 429:
            raise RateLimitedError(self.provider)
        if status in (401, 403):
            raise ProviderError(self.provider, "authentication failed (check the API key)")
        if status >= 500:
            raise ProviderError(self.provider, f"server error {status}", transient=True)
        if status >= 400:
            raise ProviderError(self.provider, f"request rejected ({status})")

    async def _send(self, method: str, url: str, **kwargs: Any) -> Any:
        if self._timeout is not None:
            kwargs.setdefault("timeout", self._timeout)
        try:
            response = await self._client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise ProviderError(self.provider, "timed out", transient=self._retry_timeouts) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                self.provider, f"network error ({type(exc).__name__})", transient=True
            ) from exc
        self._raise_for(response)
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError(self.provider, "invalid JSON response") from exc

    async def request_json(self, method: str, url: str, **kwargs: Any) -> Any:
        retrying = AsyncRetrying(
            stop=stop_after_attempt(self._attempts),
            wait=wait_exponential(multiplier=0.5, max=self._max_wait_s),
            retry=retry_if_exception(_transient),
            reraise=True,
        )
        async for attempt in retrying:
            with attempt:
                return await self._send(method, url, **kwargs)
        raise AssertionError("unreachable")  # pragma: no cover
