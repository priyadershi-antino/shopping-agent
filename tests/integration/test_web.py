"""Web UI pages render with real data (demo catalog, test database, scripted model)."""

from collections.abc import AsyncIterator

import httpx
import pytest

from shopping_agent.api.app import create_app
from shopping_agent.demo import seed_demo
from shopping_agent.runtime import Runtime
from tests.factories import NOW

pytestmark = pytest.mark.db

DEMO_FLIPKART = "https://www.flipkart.com/acme-nimbus/p/itmdemo?pid=COMDEMO000000001&affid=x"


@pytest.fixture
async def client(runtime: Runtime) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(runtime=runtime)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c,
    ):
        yield c


async def test_home_and_static(client: httpx.AsyncClient) -> None:
    home = await client.get("/")
    assert home.status_code == 200
    assert "What are you shopping for?" in home.text
    assert "Demo data." in home.text
    assert (await client.get("/static/app.js")).status_code == 200
    assert (await client.get("/static/app.css")).status_code == 200


async def test_run_page_and_fragment(client: httpx.AsyncClient) -> None:
    run = (
        await client.post(
            "/v1/chat",
            json={"message": "laptop under 80k, I have an HDFC credit card", "stream": False},
        )
    ).json()
    page = await client.get(f"/runs/{run['run_id']}")
    assert page.status_code == 200
    html = page.text
    assert "Prices and requirement check" in html
    assert "Best price for you" in html
    assert "Suggested next steps" in html
    assert "[unverified]" in html  # grounding marker is shown, escaped as text
    fragment = (await client.get(f"/ui/runs/{run['run_id']}")).text
    assert "<html" not in fragment
    assert "Sources" in fragment
    assert (await client.get("/runs/nope")).status_code == 404


async def test_link_flow_and_listing_page_with_history(
    client: httpx.AsyncClient, runtime: Runtime
) -> None:
    await seed_demo(runtime.providers, runtime.sessions, now=NOW, days=45)
    assert "Check a product link" in (await client.get("/link")).text
    jump = await client.get("/link", params={"url": DEMO_FLIPKART})
    assert jump.status_code == 303
    listing_url = jump.headers["location"]
    assert listing_url.startswith("/listing/")

    page = await client.get(listing_url)
    assert page.status_code == 200
    html = page.text
    assert '<svg class="price-chart"' in html
    assert "Show as table" in html
    assert "Lowest observed" in html
    assert "Offers and effective price" in html
    assert "Same variant elsewhere" in html
    assert "Specifications" in html
    assert "lowest ever" not in html.lower()

    bad = await client.get("/link", params={"url": "https://example.com/some/product"})
    assert bad.status_code == 200
    assert "Check a product link" in bad.text


async def test_profile_page(client: httpx.AsyncClient) -> None:
    await client.post("/v1/profile/payment-methods", json={"issuer": "HDFC", "card_type": "credit"})
    html = (await client.get("/profile")).text
    assert "HDFC credit card" in html
    assert "Never enter card numbers" in html


async def test_budget_page(client: httpx.AsyncClient) -> None:
    assert "Budget planner" in (await client.get("/budget")).text
    items = "\n".join(
        [
            "phone: Nova Pixelon 9 = 30,599",
            "phone: Acme Volt 5G = 18,999",
            "tablet: Zenith Tab S = 27,999",
        ]
    )
    html = (await client.get("/budget", params={"budget": "1L", "items": items})).text
    assert "2 combination(s) fit ₹1,00,000" in html
    bad = (await client.get("/budget", params={"budget": "1L", "items": "nonsense"})).text
    assert "use &#39;slot: product name = price&#39;" in bad
