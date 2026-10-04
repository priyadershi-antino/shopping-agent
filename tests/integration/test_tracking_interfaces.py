"""Watchlist, alerts and sales through the REST API, web pages and CLI."""

from collections.abc import AsyncIterator
from datetime import timedelta

import httpx
import pytest
from rich.console import Console

from shopping_agent.api.app import create_app
from shopping_agent.cli import main as cli
from shopping_agent.runtime import Runtime
from tests.factories import NOW

pytestmark = pytest.mark.db

SWIFT_FK = "https://www.flipkart.com/nova-swift-16/p/itmdemo?pid=COMDEMO000000004"


@pytest.fixture
async def client(runtime: Runtime) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(runtime=runtime)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c,
    ):
        yield c


async def test_watchlist_api(client: httpx.AsyncClient) -> None:
    await client.post("/v1/profile/payment-methods", json={"issuer": "HDFC", "card_type": "credit"})
    created = await client.post("/v1/watchlist", json={"url": SWIFT_FK, "target_effective": 66000})
    assert created.status_code == 200, created.text
    watch = created.json()
    assert watch["listing"]["marketplace"] == "flipkart"
    assert float(watch["target_effective"]) == 66000

    check = (await client.post(f"/v1/watchlist/{watch['id']}/check")).json()
    assert check["ok"] and float(check["best_effective"]) == 64990
    assert check["alerts"] == ["Nova Swift 16 NS16-R5: ₹64,990 for you"]

    listed = (await client.get("/v1/watchlist")).json()
    assert [w["id"] for w in listed] == [watch["id"]]
    history = (await client.get(f"/v1/watchlist/{watch['id']}/effective-history")).json()
    assert [float(h["best_effective"]) for h in history] == [64990]

    patched = (
        await client.patch(
            f"/v1/watchlist/{watch['id']}", json={"priority": "high", "active": False}
        )
    ).json()
    assert patched["priority"] == "high" and patched["active"] is False

    inbox = (await client.get("/v1/notifications")).json()
    assert inbox["unread"] == 1
    assert inbox["items"][0]["alert"]["kind"] == "target_effective"
    assert (await client.post("/v1/notifications/read", json={})).json() == {"marked": 1}
    assert (await client.get("/v1/notifications?unread_only=true")).json()["unread"] == 0
    assert len((await client.get("/v1/alerts")).json()) == 1

    bad = await client.post("/v1/watchlist", json={})
    assert bad.status_code == 422
    assert (await client.delete(f"/v1/watchlist/{watch['id']}")).json() == {"deleted": True}
    assert (await client.delete(f"/v1/watchlist/{watch['id']}")).status_code == 404


async def test_sale_endpoints_and_worker(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/sale-brief")).status_code == 404
    no_sale = (await client.get("/v1/sale-prep")).json()
    assert no_sale["event"] is None

    start = (NOW + timedelta(days=2)).isoformat()
    end = (NOW + timedelta(days=8)).isoformat()
    event = await client.post(
        "/v1/sale-events",
        json={
            "name": "Big Billion Days",
            "platform": "flipkart",
            "start": start,
            "end": end,
            "early_access_hours": 24,
            "early_access_for": "flipkart plus",
            "partner_banks": ["HDFC"],
        },
    )
    assert event.status_code == 200, event.text
    assert event.json()["verification"] == "user_provided"
    assert (
        await client.post(
            "/v1/sale-events",
            json={"name": "Bad", "platform": "flipkart", "start": end, "end": start},
        )
    ).status_code == 422

    await client.post("/v1/watchlist", json={"url": SWIFT_FK})
    plan = (await client.get("/v1/sale-prep?platform=flipkart")).json()
    assert plan["event"]["name"] == "Big Billion Days"
    titles = [s["title"] for s in plan["steps"]]
    assert "Early access" in titles and "Check bank offers" in titles

    tick = (await client.post("/v1/worker/run-once")).json()
    assert tick["checked"] == 1
    assert tick["sale_alerts"] == [
        "Big Billion Days on Flipkart starts 04 Oct"
    ]  # early access opens a day earlier
    brief = (await client.get("/v1/sale-brief")).json()
    assert brief["items"][0]["label"] == "Nova Swift 16 NS16-R5"


async def test_confirm_endpoint(client: httpx.AsyncClient) -> None:
    run = (
        await client.post(
            "/v1/chat",
            json={"message": "gaming laptop under 80k, HDFC credit card", "stream": False},
        )
    ).json()
    action = run["proposed_actions"][0]
    url = f"/v1/runs/{run['run_id']}/actions/{action['id']}"
    ok = await client.post(f"{url}/confirm", json={"target_price": 61000})
    assert ok.status_code == 200, ok.text
    assert ok.json()["action"]["status"] == "confirmed"
    assert float(ok.json()["result"]["target_effective"]) == 61000
    assert (await client.post(f"{url}/confirm")).status_code == 400
    assert (await client.post(f"/v1/runs/{run['run_id']}/actions/nope/dismiss")).status_code == 404
    page = (await client.get(f"/runs/{run['run_id']}")).text
    assert "confirmed" in page


async def test_watchlist_and_alert_pages(client: httpx.AsyncClient) -> None:
    created = (await client.post("/v1/watchlist", json={"url": SWIFT_FK})).json()
    await client.post(f"/v1/watchlist/{created['id']}/check")
    html = (await client.get("/watchlist")).text
    assert "Tracked products" in html and "Nova Swift 16" in html
    assert "Sale prep" in html
    listing = (await client.get(f"/listing/{created['listing']['listing_id']}")).text
    assert "tracking" in listing and "Change targets" in listing
    assert "Best price for you" in listing  # second chart series from effective snapshots
    alerts = (await client.get("/alerts")).text
    assert "Alerts" in alerts


@pytest.fixture
def out(monkeypatch: pytest.MonkeyPatch) -> Console:
    console = Console(record=True, width=160, highlight=False)
    monkeypatch.setattr(cli, "console", console)
    return console


async def test_cli_tracking_commands(runtime: Runtime, out: Console) -> None:
    await cli.watch_add(
        runtime, SWIFT_FK, target=None, effective_target="70000", drop_pct=None, priority="high"
    )
    await cli.watch_list(runtime)
    await cli.show_inbox(runtime, show_all=False, mark_read=True)
    await cli.show_inbox(runtime, show_all=False, mark_read=False)
    await cli.show_prep(runtime, None)
    await cli.show_brief(runtime)
    await cli.watch_check(runtime, None)
    text = out.export_text()
    assert "Tracking Nova Swift 16 NS16-R5" in text
    assert "Listed ₹68,990 · best for you ₹68,990" in text  # no saved card
    assert "Alert: Nova Swift 16 NS16-R5: ₹68,990 for you" in text
    assert "for you ≤ ₹70,000" in text
    assert "No new alerts." in text
    assert "No upcoming sale is recorded" in text
    assert "Checked 0 item(s)" in text
