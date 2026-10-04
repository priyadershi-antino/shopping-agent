"""Per-provider daily request budgets (IST calendar day), backed by cache counters."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone

from shopping_agent.errors import QuotaExhaustedError
from shopping_agent.providers.cache import Cache

IST = timezone(timedelta(hours=5, minutes=30))
_TWO_DAYS = 2 * 24 * 3600


class QuotaGuard:
    def __init__(
        self,
        cache: Cache,
        daily_limits: dict[str, int],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._cache = cache
        self._limits = daily_limits
        self._clock = clock

    def _key(self, provider: str) -> str:
        day = self._clock().astimezone(IST).date().isoformat()
        return f"quota:{provider}:{day}"

    def limit(self, provider: str) -> int | None:
        return self._limits.get(provider)

    async def acquire(self, provider: str) -> None:
        """Count one request; raise if the provider's daily budget is already spent."""
        limit = self._limits.get(provider)
        if limit is None:
            return
        key = self._key(provider)
        used = await self._cache.incr(key, _TWO_DAYS)
        if used > limit:
            await self._cache.incr(key, _TWO_DAYS, by=-1)  # a refused request is not spent
            raise QuotaExhaustedError(provider)

    async def used(self, provider: str) -> int:
        value = await self._cache.get(self._key(provider))
        return int(value) if value else 0
