"""Tracking end to end: write tools, price checks, alerts, offer changes, sales, delivery."""

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from shopping_agent.config import Settings
from shopping_agent.db.models import AlertEventRow, NotificationRow
from shopping_agent.db.repositories import SaleEventRepository
from shopping_agent.db.repositories.profile import ProfileRepository
from shopping_agent.db.repositories.tracking import (
    AlertRepository,
    NotificationRepository,
    WatchRepository,
)
from shopping_agent.domain.common import Verification
from shopping_agent.domain.offer import CardType, PaymentMethod
from shopping_agent.domain.product import Marketplace
from shopping_agent.domain.sale import SaleEvent
from shopping_agent.errors import QuotaExhaustedError, ToolInputError
from shopping_agent.providers.cache import InMemoryCache
from shopping_agent.providers.fixtures import (
    FixturePageFetcher,
    FixtureProductProvider,
    FixtureWebProvider,
)
from shopping_agent.providers.product.base import ProviderListing
from shopping_agent.providers.quota import QuotaGuard
from shopping_agent.providers.registry import ProviderRegistry
from shopping_agent.runtime import Runtime
from shopping_agent.tools import ToolRegistry, all_tools
from shopping_agent.tools.catalog_tools import ResolveLinkOutput
from shopping_agent.tracking.actions import ActionNotFoundError, confirm_action, dismiss_action
from shopping_agent.tracking.monitor import check_watch
from shopping_agent.tracking.notify import deliver_pending
from shopping_agent.tracking.sales import check_sales
from shopping_agent.tracking.views import WatchView
from shopping_agent.tracking.worker import run_once
from tests.factories import NOW
from tests.fakes import scripted_client
from tests.scenarios import discovery_handler

pytestmark = pytest.mark.db

SWIFT_FK = "https://www.flipkart.com/nova-swift-16/p/itmdemo?pid=COMDEMO000000004"
SWIFT_ID = "COMDEMO000000004"


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


class NoCache(InMemoryCache):
    """Every provider call is fresh, so price changes show up immediately."""

    async def get(self, key: str) -> str | None:
        return None


class ChangingProvider(FixtureProductProvider):
    """The demo catalog, with prices, offers and failures that tests can change."""

    def __init__(self, clock: Clock) -> None:
        super().__init__(clock=clock)
        self.price: dict[str, str] = {}
        self.offer_ids: dict[str, list[str]] = {}
        self.fail: Exception | None = None

    def _listing(self, item: dict[str, Any]) -> ProviderListing:
        ext = item.get("external_id")
        item = dict(item)
        if ext in self.price:
            item["price"] = self.price[ext]
        if ext in self.offer_ids:
            item["offer_ids"] = self.offer_ids[ext]
        return super()._listing(item)

    async def lookup(self, marketplace: Marketplace, external_id: str) -> list[ProviderListing]:
        if self.fail is not None:
            raise self.fail
        return await super().lookup(marketplace, external_id)


Tracker = tuple[Runtime, ChangingProvider, Clock]


@pytest.fixture
async def tracker(
    migrated_engine: AsyncEngine, db: async_sessionmaker[AsyncSession]
) -> AsyncIterator[Tracker]:
    clock = Clock(NOW)
    provider = ChangingProvider(clock)
    cache = NoCache()
    registry = ProviderRegistry(
        products=[provider],
        web=[FixtureWebProvider()],
        fetcher=FixturePageFetcher(),
        cache=cache,
        quota=QuotaGuard(cache, {}),
    )
    llm, _ = scripted_client(discovery_handler)
    async with httpx.AsyncClient() as http:
        yield (
            Runtime(
                settings=Settings(log_json=False, alert_cooldown_hours=0),
                engine=migrated_engine,
                sessions=db,
                cache=InMemoryCache(),
                http=http,
                providers=registry,
                tools=ToolRegistry(all_tools()),
                llm_override=llm,
                clock=clock,
            ),
            provider,
            clock,
        )


async def track_swift(rt: Runtime, **targets: str) -> WatchView:
    link = await rt.tool_data("resolve_product_link", {"url": SWIFT_FK}, ResolveLinkOutput)
    listing_id = link.listings[0].listing_id
    async with rt.sessions() as session, session.begin():
        await ProfileRepository(session).add_payment_method(
            PaymentMethod(issuer="HDFC", card_type=CardType.CREDIT)
        )
    return await rt.tool_data(
        "track_product",
        {"listing_id": listing_id, **targets},
        WatchView,
        allow_side_effects=True,
    )


async def alert_kinds(rt: Runtime) -> list[str]:
    async with rt.sessions() as session:
        return sorted(a.kind for a in await AlertRepository(session).recent())


async def test_write_tools_need_confirmation(tracker: Tracker) -> None:
    rt, _, _ = tracker
    link = await rt.tool_data("resolve_product_link", {"url": SWIFT_FK}, ResolveLinkOutput)
    _, record = await rt.tools.execute(
        "track_product", {"listing_id": link.listings[0].listing_id}, rt.tool_context()
    )
    assert record.status == "denied"
    assert "track_product" not in {s.name for s in rt.tools.specs()}  # never offered to the model
    with pytest.raises(ToolInputError, match="never stored"):
        await rt.call_tool(
            "save_user_preference",
            {"key": "card number", "value": "4111 1111 1111 1111"},
            allow_side_effects=True,
        )


async def test_price_and_offer_alerts(tracker: Tracker) -> None:
    rt, provider, clock = tracker
    watch = await track_swift(rt, target_effective="62000")

    first = await check_watch(rt, watch.id, now=clock())
    assert first.ok and first.best_effective == 64990  # HDFC 10% capped at ₹4,000
    assert first.alerts == []

    provider.price[SWIFT_ID] = "65990"
    clock.advance(hours=2)
    second = await check_watch(rt, watch.id, now=clock())
    assert second.best_effective == 61990
    assert second.alerts == ["Nova Swift 16 NS16-R5: ₹61,990 for you"]

    clock.advance(hours=2)
    third = await check_watch(rt, watch.id, now=clock())
    assert third.alerts == []  # still below target: no repeat

    provider.offer_ids[SWIFT_ID] = ["fx-hdfc-flipkart"]  # the exchange offer is withdrawn
    clock.advance(hours=2)
    fourth = await check_watch(rt, watch.id, now=clock())
    assert any(t.startswith("Offer ended") for t in fourth.alerts)

    assert await alert_kinds(rt) == ["offer_ended", "target_effective"]
    async with rt.sessions() as session:
        inbox = await NotificationRepository(session).inbox(unread_only=True)
        snaps = await WatchRepository(session).snapshots(watch.id)
        row = await WatchRepository(session).get(watch.id)
    assert len(inbox) == 2
    assert [s.best_effective for s in snaps] == [64990, 61990, 61990, 61990]
    assert row is not None and row.check_count == 4
    assert row.next_check_at > clock()


async def test_provider_quota_postpones_check(tracker: Tracker) -> None:
    rt, provider, clock = tracker
    watch = await track_swift(rt)
    provider.fail = QuotaExhaustedError("fixture")
    outcome = await check_watch(rt, watch.id, now=clock())
    assert not outcome.ok
    assert outcome.next_check_at is not None and outcome.next_check_at > clock() + timedelta(
        hours=1
    )
    async with rt.sessions() as session:
        row = await WatchRepository(session).get(watch.id)
    assert row is not None and "quota" in (row.last_error or "")


async def test_sale_announcements_fire_once(tracker: Tracker) -> None:
    rt, _, clock = tracker
    watch = await track_swift(rt, target_effective="66000")
    await check_watch(rt, watch.id, now=clock())
    start = clock() + timedelta(days=2)
    async with rt.sessions() as session, session.begin():
        await SaleEventRepository(session).upsert(
            SaleEvent(
                name="Festive Sale",
                platform=Marketplace.FLIPKART,
                start=start,
                end=start + timedelta(days=5),
                partner_banks=["HDFC"],
                verification=Verification.UNVERIFIED,
                last_verified_at=clock(),
            )
        )
    upcoming = await check_sales(rt, now=clock())
    assert [a.kind for a in upcoming] == ["sale_upcoming"]
    assert "(dates unconfirmed)" in upcoming[0].message
    assert await check_sales(rt, now=clock()) == []

    clock.now = start + timedelta(hours=1)
    started = await check_sales(rt, now=clock())
    assert [a.kind for a in started] == ["sale_start"]
    assert "Nova Swift 16 NS16-R5: listed ₹68,990" in started[0].message
    assert "₹64,990 for you with your cards (target met)" in started[0].message
    assert await check_sales(rt, now=clock()) == []


async def test_delivery_by_telegram_without_leaking_the_token(tracker: Tracker) -> None:
    rt, _, clock = tracker
    rt.settings = Settings(
        log_json=False,
        notify_channels="telegram",
        telegram_bot_token=SecretStr("123:SECRET"),
        telegram_chat_id="42",
    )
    async with rt.sessions() as session, session.begin():
        await AlertRepository(session).add(
            kind="target_effective",
            dedup_key="k1",
            title="Price met",
            message="m",
            now=clock(),
            channels=rt.settings.notify_channel_list(),
        )
    url = "https://api.telegram.org/bot123:SECRET/sendMessage"
    with respx.mock(assert_all_called=True) as mock:
        mock.post(url).respond(401, json={"ok": False})
        assert await deliver_pending(rt, now=clock()) == 0
        mock.post(url).respond(200, json={"ok": True})
        assert await deliver_pending(rt, now=clock()) == 1
    async with rt.sessions() as session:
        rows = list(await session.scalars(select(NotificationRow)))
    by_channel = {r.channel: r for r in rows}
    assert by_channel["telegram"].status == "sent"
    assert by_channel["telegram"].attempts == 1
    assert by_channel["in_app"].status == "sent"

    rt.settings = rt.settings.model_copy(update={"quiet_hours": "0-23:59"})
    async with rt.sessions() as session, session.begin():
        await AlertRepository(session).add(
            kind="x", dedup_key="k2", title="t", message="m", now=clock(), channels=["telegram"]
        )
    assert await deliver_pending(rt, now=clock()) == 0  # waits for quiet hours to end


async def test_confirming_a_proposed_alert(tracker: Tracker) -> None:
    rt, _, _ = tracker
    state = await rt.agent().run("laptop under 80k for gaming, I have an HDFC credit card")
    action = state.proposed_actions[0]
    assert action.action == "price_alert"

    confirmed, result = await confirm_action(rt, state.run_id, action.id)
    assert confirmed.status == "confirmed"
    assert isinstance(result, WatchView) and result.target_effective == 60000
    with pytest.raises(ToolInputError, match="already confirmed"):
        await confirm_action(rt, state.run_id, action.id)
    with pytest.raises(ActionNotFoundError):
        await dismiss_action(rt, state.run_id, "nope")
    reloaded = await rt.agent().store.load(state.run_id)  # type: ignore[union-attr]
    assert reloaded is not None and reloaded.proposed_actions[0].status == "confirmed"


async def test_worker_tick_checks_only_due_items(tracker: Tracker) -> None:
    rt, _, clock = tracker
    await track_swift(rt, target_effective="70000")
    report = await run_once(rt)
    assert (report.checked, report.failed) == (1, 0)
    assert len(report.alerts) == 1  # already below the target on the first check
    assert (await run_once(rt)).checked == 0  # next check is scheduled later
    clock.advance(days=1)
    assert (await run_once(rt)).checked == 1
    async with rt.sessions() as session:
        count = await session.scalar(select(func.count()).select_from(AlertEventRow))
    assert count == 1


async def test_a_crashing_check_does_not_block_the_tick(
    tracker: Tracker, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt, _, clock = tracker
    watch = await track_swift(rt)

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("unexpected")

    monkeypatch.setattr("shopping_agent.tracking.worker.check_watch", boom)
    report = await run_once(rt)
    assert (report.checked, report.failed) == (0, 1)
    async with rt.sessions() as session:
        row = await WatchRepository(session).get(watch.id)
    assert row is not None and row.last_error == "internal error during the check"
    assert row.next_check_at > clock()  # not retried on every tick
