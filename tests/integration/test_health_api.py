import httpx

from shopping_agent.api.app import create_app
from shopping_agent.config import Settings
from shopping_agent.providers.cache import InMemoryCache


async def test_health_reports_component_status() -> None:
    app = create_app(Settings(log_json=False), cache=InMemoryCache())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client,
    ):
        response = await client.get("/v1/health")
    body = response.json()
    assert body["cache"] is True
    # DB may or may not be reachable in this environment; the endpoint must reflect it.
    assert response.status_code == (200 if body["database"] else 503)
    assert body["status"] == ("ok" if body["database"] else "degraded")
