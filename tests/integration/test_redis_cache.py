import uuid

import pytest

from shopping_agent.providers.cache import RedisCache

pytestmark = pytest.mark.redis


async def test_redis_roundtrip_and_incr(redis_url: str) -> None:
    cache = RedisCache.from_url(redis_url, prefix=f"sa-test-{uuid.uuid4().hex}:")
    try:
        assert await cache.ping()
        await cache.set("k", "v", ttl_s=30)
        assert await cache.get("k") == "v"
        assert await cache.incr("counter", ttl_s=30) == 1
        assert await cache.incr("counter", ttl_s=30) == 2
        await cache.delete("k")
        await cache.delete("counter")
        assert await cache.get("k") is None
    finally:
        await cache.close()
