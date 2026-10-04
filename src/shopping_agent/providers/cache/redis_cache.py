"""Redis-backed cache with native TTLs."""

from redis.asyncio import Redis
from redis.exceptions import RedisError


class RedisCache:
    def __init__(self, client: Redis, prefix: str = "sa:") -> None:
        self._client = client
        self._prefix = prefix

    @classmethod
    def from_url(cls, url: str, prefix: str = "sa:") -> "RedisCache":
        return cls(Redis.from_url(url, decode_responses=True), prefix)

    def _k(self, key: str) -> str:
        return f"{self._prefix}{key}"

    async def get(self, key: str) -> str | None:
        value = await self._client.get(self._k(key))
        if value is None:
            return None
        return value.decode() if isinstance(value, bytes) else str(value)

    async def set(self, key: str, value: str, ttl_s: int) -> None:
        await self._client.set(self._k(key), value, ex=ttl_s)

    async def delete(self, key: str) -> None:
        await self._client.delete(self._k(key))

    async def incr(self, key: str, ttl_s: int) -> int:
        k = self._k(key)
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.incr(k)
            pipe.expire(k, ttl_s, nx=True)
            count, _ = await pipe.execute()
        return int(count)

    async def ping(self) -> bool:
        try:
            return bool(await self._client.ping())
        except (RedisError, OSError):
            return False

    async def close(self) -> None:
        await self._client.aclose()
