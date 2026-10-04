"""In-process cache for tests and single-process development."""

import time
from collections.abc import Callable


class InMemoryCache:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._data: dict[str, tuple[str, float]] = {}

    def _live(self, key: str) -> tuple[str, float] | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        if entry[1] <= self._clock():
            del self._data[key]
            return None
        return entry

    async def get(self, key: str) -> str | None:
        entry = self._live(key)
        return entry[0] if entry else None

    async def set(self, key: str, value: str, ttl_s: int) -> None:
        self._data[key] = (value, self._clock() + ttl_s)

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def incr(self, key: str, ttl_s: int, by: int = 1) -> int:
        entry = self._live(key)
        if entry is None:
            self._data[key] = (str(by), self._clock() + ttl_s)
            return by
        count = int(entry[0]) + by
        self._data[key] = (str(count), entry[1])
        return count

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        self._data.clear()
