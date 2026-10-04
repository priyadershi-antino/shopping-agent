"""REST API end to end: the real app on the test database, demo catalog and a scripted model."""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from shopping_agent.api.app import create_app
from shopping_agent.config import Settings
from shopping_agent.llm.types import Message, ResponseSchema, ToolDef
from shopping_agent.runtime import Runtime
from tests.fakes import scripted_client

pytestmark = pytest.mark.db

DEMO_AMAZON = "https://www.amazon.in/Acme-Nimbus/dp/B0DEMO0001?tag=aff-21&ref=sr_1"
ASK = "laptop under 80k for coding and gaming, I have an HDFC credit card"


@pytest.fixture
async def client(runtime: Runtime) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(runtime=runtime)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c,
    ):
        yield c


def sse_events(text: str) -> list[tuple[str, Any]]:
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


async def test_health(client: httpx.AsyncClient) -> None:
    body = (await client.get("/v1/health")).json()
    assert body["status"] == "ok"
    assert body["demo_data"] is True


async def test_chat_json_and_runs(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/chat", json={"message": ASK, "stream": False})
    assert response.status_code == 200, response.text
    run = response.json()
    assert run["status"] == "completed"
    assert run["demo_data"] is True
    assert run["answer"]["recommendations"]
    top = run["items"][0]
    assert float(top["best_eligible"]) < float(top["listing"]["price"])  # HDFC offer applied
    assert top["scenarios"][0]["label"] == "A"
    assert run["sources"], "price evidence for the shortlist is exposed"
    assert run["proposed_actions"][0]["status"] == "pending"
    assert run["unverified_figures"] == 1

    listed = (await client.get("/v1/runs")).json()
    assert [r["run_id"] for r in listed] == [run["run_id"]]
    again = (await client.get(f"/v1/runs/{run['run_id']}")).json()
    assert again["answer"] == run["answer"]
    assert (await client.get("/v1/runs/nope")).status_code == 404


async def test_chat_platforms_are_validated_and_applied(client: httpx.AsyncClient) -> None:
    bad = await client.post(
        "/v1/chat", json={"message": ASK, "stream": False, "platforms": ["ebay"]}
    )
    assert bad.status_code == 422
    ok = await client.post(
        "/v1/chat", json={"message": ASK, "stream": False, "platforms": ["flipkart"]}
    )
    assert ok.status_code == 200, ok.text
    markets = {i["listing"]["marketplace"] for i in ok.json()["items"]}
    assert markets == {"flipkart"}


async def test_chat_streams_progress(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/chat", json={"message": ASK})
    assert response.headers["content-type"].startswith("text/event-stream")
    events = sse_events(response.text)
    kinds = [k for k, _ in events]
    assert kinds[0] == "stage"
    assert "tool_call" in kinds and "tool_result" in kinds
    assert kinds[-2:] == ["done", "final"]
    final = events[-1][1]
    assert final["status"] == "completed"
    follow = await client.post(
        "/v1/chat",
        json={"message": "and a cheaper one?", "previous_run_id": final["run_id"], "stream": False},
    )
    assert follow.status_code == 200
    missing = await client.post(
        "/v1/chat", json={"message": "hi there", "previous_run_id": "nope", "stream": False}
    )
    assert missing.status_code == 404


async def test_product_endpoints(client: httpx.AsyncClient) -> None:
    found = (
        await client.get("/v1/search", params={"q": "gaming laptop", "max_price": 80000})
    ).json()
    assert found["data"]["demo_data"] is True
    listing_id = found["data"]["candidates"][0]["listings"][0]["listing_id"]

    assert (await client.get(f"/v1/listings/{listing_id}")).json()["listing_id"] == listing_id
    details = (await client.get(f"/v1/listings/{listing_id}/details")).json()
    assert details["data"]["specs"]
    sellers = (await client.get(f"/v1/listings/{listing_id}/sellers")).json()
    assert sellers["data"]["matching"]
    offers = (await client.get(f"/v1/listings/{listing_id}/offers")).json()
    assert offers["data"]["scenarios"][0]["label"] == "A"
    history = (await client.get(f"/v1/listings/{listing_id}/price-history?days=30")).json()
    assert history["data"]["listings"][0]["baseline"]["coverage_days"] == 1

    assert (await client.get("/v1/listings/nope")).status_code == 404
    bad = await client.get("/v1/listings/nope/offers")
    assert bad.status_code == 400
    assert "unknown listing_id" in bad.json()["detail"]


async def test_resolve_link(client: httpx.AsyncClient) -> None:
    body = (await client.post("/v1/products/resolve-link", json={"url": DEMO_AMAZON})).json()
    assert body["data"]["status"] == "resolved"
    assert body["data"]["parsed"]["canonical_url"] == "https://www.amazon.in/dp/B0DEMO0001"


async def test_offers_with_saved_card(client: httpx.AsyncClient) -> None:
    link = (await client.post("/v1/products/resolve-link", json={"url": DEMO_AMAZON})).json()
    listing_id = link["data"]["listings"][0]["listing_id"]
    before = (await client.get(f"/v1/listings/{listing_id}/offers")).json()["data"]
    await client.post("/v1/profile/payment-methods", json={"issuer": "SBI", "card_type": "credit"})
    after = (await client.get(f"/v1/listings/{listing_id}/offers")).json()["data"]

    def best(d: dict[str, Any]) -> float:
        return min(float(s["net_after_cashback"]) for s in d["scenarios"] if not s["conditional"])

    assert best(after) < best(before)
    payments = [r["payment"] for r in after["card_matrix"]]
    assert payments == ["Without card offers", "SBI credit card"]


async def test_profile_crud(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/v1/profile/payment-methods", json={"issuer": "ICICI", "card_type": "debit"}
    )
    assert r.json()["profile"]["payment_methods"][0]["issuer"] == "ICICI"
    assert (
        await client.post("/v1/profile/payment-methods", json={"issuer": "X", "card_type": "any"})
    ).status_code in (400, 422)
    await client.post("/v1/profile/memberships", json={"name": "Amazon Prime"})
    await client.post(
        "/v1/profile/exchange-devices", json={"category": "smartphone", "model": "Old"}
    )
    r = await client.put("/v1/profile/preferences/prefers_emi", json={"value": True})
    body = r.json()
    assert body["profile"]["memberships"] == ["amazon prime"]
    assert body["profile"]["prefers_emi"] is True
    assert body["preferences"] == {"prefers_emi": True}

    r = await client.delete(
        "/v1/profile/payment-methods", params={"issuer": "icici", "card_type": "debit"}
    )
    assert r.json()["profile"]["payment_methods"] == []
    r = await client.delete("/v1/profile")
    assert r.json()["profile"]["memberships"] == []
    assert r.json()["preferences"] == {}


async def test_calculators_and_sales(client: httpx.AsyncClient) -> None:
    emi = (
        await client.post(
            "/v1/offers/emi-cost", json={"price": 60000, "tenure_months": 6, "annual_rate_pct": 15}
        )
    ).json()
    assert float(emi["extra_vs_full_payment"]) > 0
    plan = (
        await client.post(
            "/v1/budget/plan",
            json={
                "budget": 100000,
                "slots": [
                    {"name": "phone", "options": [{"id": "p", "name": "P", "unit_price": 35000}]},
                    {"name": "tablet", "options": [{"id": "t", "name": "T", "unit_price": 28000}]},
                ],
            },
        )
    ).json()
    assert plan["feasible_count"] == 1
    sales = (await client.get("/v1/sale-events")).json()
    assert sales["data"]["events"] == []


async def test_parse_offer_and_save(client: httpx.AsyncClient, runtime: Runtime) -> None:
    extraction: dict[str, Any] = {
        "offers": [
            {
                "type": "bank",
                "title": "10% off on SBI credit cards",
                "provider": "SBI",
                "flat_amount": None,
                "percent": 10,
                "max_discount": 1500,
                "min_order_value": 5000,
                "issuer": "SBI",
                "card_type": "credit",
                "network": None,
                "emi_rule": "both",
                "membership_required": None,
                "exchange_max_value": None,
                "exchange_min_value": None,
                "exchange_bonus": None,
                "combinable_with": [],
                "exclusive": False,
                "cashback_deferred": False,
                "valid_from": None,
                "valid_to": None,
            }
        ]
    }

    def handler(_: list[Message], __: list[ToolDef] | None, schema: ResponseSchema | None) -> str:
        assert schema is not None and schema.name == "OfferExtraction"
        return json.dumps(extraction)

    runtime.llm_override, _ = scripted_client(handler)
    link = (await client.post("/v1/products/resolve-link", json={"url": DEMO_AMAZON})).json()
    listing_id = link["data"]["listings"][0]["listing_id"]
    text = "10% instant discount up to ₹1,500 on SBI credit cards, min order ₹5,000"
    r = await client.post(
        "/v1/offers/parse", json={"text": text, "listing_id": listing_id, "save": True}
    )
    assert r.status_code == 200, r.text
    parsed = r.json()
    assert parsed["saved"] is True
    assert parsed["offers"][0]["verification"] == "user_provided"
    offers = (await client.get(f"/v1/listings/{listing_id}/offers")).json()["data"]["offers"]
    assert any(o["source"] == "user" for o in offers)
    no_listing = await client.post("/v1/offers/parse", json={"text": text, "save": True})
    assert no_listing.status_code == 400


async def test_api_key_protects_api_and_pages(runtime: Runtime) -> None:
    runtime.settings = Settings(log_json=False, api_key=SecretStr("s3cret"))
    app = create_app(runtime=runtime)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c,
    ):
        assert (await c.get("/v1/runs")).status_code == 401
        assert (await c.get("/v1/runs", headers={"X-API-Key": "s3cret"})).status_code == 200
        assert (await c.get("/v1/health")).status_code == 200  # health stays open
        page = await c.get("/")
        assert page.status_code == 303
        assert page.headers["location"] == "/ui/key"
        assert (await c.get("/ui/key")).status_code == 200
        assert (await c.post("/ui/key", json={"value": "wrong"})).status_code == 401
        ok = await c.post("/ui/key", json={"value": "s3cret"})
        assert ok.status_code == 200
        assert "httponly" in ok.headers["set-cookie"].lower()
        assert (await c.get("/")).status_code == 200  # cookie now sent by the client
