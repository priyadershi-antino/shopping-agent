"""Cache abstraction.

Values are strings (callers serialise JSON). `incr` with a TTL backs provider
quota budgets and rate-limit counters.
"""

from typing import Protocol


class Cache(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, ttl_s: int) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def incr(self, key: str, ttl_s: int, by: int = 1) -> int:
        """Add `by` to a counter (negative to give back); the TTL is applied when it is created."""
        ...

    async def ping(self) -> bool: ...

    async def close(self) -> None: ...
