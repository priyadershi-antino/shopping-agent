from shopping_agent.providers.cache import InMemoryCache


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_get_set_and_expiry() -> None:
    clock = FakeClock()
    cache = InMemoryCache(clock)
    await cache.set("price:1", "74999", ttl_s=60)
    assert await cache.get("price:1") == "74999"
    clock.now += 61
    assert await cache.get("price:1") is None


async def test_incr_keeps_original_window() -> None:
    clock = FakeClock()
    cache = InMemoryCache(clock)
    assert await cache.incr("quota:serpapi", ttl_s=60) == 1
    clock.now += 30
    assert await cache.incr("quota:serpapi", ttl_s=60) == 2
    clock.now += 31  # first window ends at t=60 regardless of the second incr
    assert await cache.incr("quota:serpapi", ttl_s=60) == 1


async def test_delete() -> None:
    cache = InMemoryCache()
    await cache.set("k", "v", ttl_s=10)
    await cache.delete("k")
    assert await cache.get("k") is None
