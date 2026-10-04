"""`shop` command-line interface.

Each command is a thin wrapper around an async function that takes a `Runtime`, so the same
code paths are used by the API and are easy to test.
"""

import asyncio
import io
import json
import sys
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import typer
import uvicorn
from rich.console import Console
from sqlalchemy import select

from shopping_agent import __version__
from shopping_agent.agent.orchestrator import AgentEvent
from shopping_agent.agent.store import SqlRunStore
from shopping_agent.agent.views import follow_up, run_view
from shopping_agent.cli.render import (
    demo_banner,
    esc,
    inr,
    listing_line,
    render_history,
    render_link,
    render_offers,
    render_run,
)
from shopping_agent.config import get_settings
from shopping_agent.db.models import AgentRunRow
from shopping_agent.db.repositories.profile import KNOWN_PREFERENCES, ProfileRepository
from shopping_agent.domain.offer import CardType, ExchangeDevice, PaymentMethod
from shopping_agent.domain.product import parse_marketplaces
from shopping_agent.errors import ShoppingAgentError
from shopping_agent.llm.client import RunUsage
from shopping_agent.runtime import Runtime, open_runtime
from shopping_agent.services.offer_engine import no_cost_emi_true_cost
from shopping_agent.services.url_parser import UrlParseError, parse_product_url
from shopping_agent.tools.catalog_tools import ResolveLinkOutput
from shopping_agent.tools.deal_tools import FindOffersOutput, PriceHistoryOutput

app = typer.Typer(help="AI shopping research and deal-intelligence agent.", no_args_is_help=True)
profile_app = typer.Typer(
    help="Your saved cards (bank + type only), memberships, devices and preferences.",
    no_args_is_help=True,
)
app.add_typer(profile_app, name="profile")

console = Console(highlight=False)


@app.callback()
def _utf8_output() -> None:
    """Windows pipes default to cp1252, which has no ₹; write UTF-8 instead of crashing."""
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
        if encoding != "utf8" and isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")


# Tests replace this to run commands against a test database and a scripted model.
runtime_factory: Callable[[], AbstractAsyncContextManager[Runtime]] = open_runtime

TOOL_LABELS = {
    "resolve_product_link": "reading the product link",
    "search_products": "searching products",
    "get_product_details": "checking specifications",
    "get_listings": "comparing sellers",
    "find_offers": "checking offers",
    "research_reviews": "reading reviews",
    "get_price_history": "checking price history",
    "get_sale_events": "checking sale events",
    "find_alternatives": "looking for alternatives",
    "search_web": "searching the web",
    "fetch_page": "reading a page",
}


def run_with_runtime[T](fn: Callable[[Runtime], Awaitable[T]]) -> T:
    async def main() -> T:
        async with runtime_factory() as rt:
            return await fn(rt)

    try:
        return asyncio.run(main())
    except ShoppingAgentError as exc:
        console.print(f"[red]Error:[/] {esc(exc)}")
        raise typer.Exit(1) from exc


# --- agent ---


async def progress_printer(event: AgentEvent) -> None:
    if event.type == "stage":
        console.print(f"[bold]›[/] {esc(event.message)}")
    elif event.type == "tool_call":
        console.print(f"  [dim]… {esc(TOOL_LABELS.get(event.message, event.message))}[/]")
    elif event.type == "warning":
        console.print(f"  [yellow]! {esc(event.message)}[/]")


async def ask_once(
    rt: Runtime,
    request: str,
    *,
    previous_run_id: str | None = None,
    as_json: bool = False,
    platforms: list[str] | None = None,
) -> str:
    """Run the agent once and print the result. Returns the run id."""
    context, carry = None, list[str]()
    if previous_run_id:
        previous = await SqlRunStore(rt.sessions).load(previous_run_id)
        if previous is None:
            raise ShoppingAgentError(f"run {previous_run_id} not found")
        context, carry = follow_up(previous)
    profile = await rt.load_profile()
    state = await rt.agent().run(
        request,
        profile=profile,
        context=context,
        carry_listing_ids=carry,
        platforms=platforms,
        on_event=None if as_json else progress_printer,
    )
    view = run_view(state)
    if as_json:
        console.print_json(view.model_dump_json())
    else:
        render_run(console, view)
    return state.run_id


@app.command()
def ask(
    request: str = typer.Argument(..., help="What you want, in your own words."),
    follow_up: str | None = typer.Option(None, "--follow-up", help="Run id to continue from."),
    as_json: bool = typer.Option(False, "--json", help="Print the result as JSON."),
    platform: list[str] = typer.Option(
        [],
        "--platform",
        "-p",
        help="Only use this platform (repeatable): amazon, flipkart, croma, reliance_digital, "
        "vijay_sales, tata_cliq, other. Default: DEFAULT_PLATFORMS.",
    ),
) -> None:
    """Ask once, e.g. shop ask "laptop under 80k for coding, I have an HDFC credit card"."""
    try:
        parse_marketplaces(platform)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--platform") from exc
    run_with_runtime(
        lambda rt: ask_once(
            rt, request, previous_run_id=follow_up, as_json=as_json, platforms=platform
        )
    )


async def chat_loop(rt: Runtime, read: Callable[[], Awaitable[str]]) -> None:
    console.print(
        "[dim]Each message follows up on the previous answer. "
        "'new' starts a new topic; an empty line or 'exit' stops.[/]"
    )
    previous: str | None = None
    while True:
        text = (await read()).strip()
        if not text or text.lower() in {"exit", "quit", "q"}:
            return
        if text.lower() in {"new", "/new"}:
            previous = None
            console.print("[dim]Starting a new topic.[/]")
            continue
        previous = await ask_once(rt, text, previous_run_id=previous)


@app.command()
def chat() -> None:
    """Interactive conversation with follow-ups. Type 'new' to start a new topic."""

    async def read() -> str:
        try:
            return await asyncio.to_thread(console.input, "\n[bold cyan]You:[/] ")
        except EOFError:
            return ""

    run_with_runtime(lambda rt: chat_loop(rt, read))


@app.command()
def run(run_id: str = typer.Argument(..., help="Run id from `shop runs`.")) -> None:
    """Show a saved run."""

    async def show(rt: Runtime) -> None:
        state = await SqlRunStore(rt.sessions).load(run_id)
        if state is None:
            raise ShoppingAgentError(f"run {run_id} not found")
        render_run(console, run_view(state))

    run_with_runtime(show)


async def list_runs(rt: Runtime, limit: int) -> None:
    async with rt.sessions() as session:
        rows = list(
            await session.scalars(
                select(AgentRunRow).order_by(AgentRunRow.created_at.desc()).limit(limit)
            )
        )
    if not rows:
        console.print("No runs yet.")
    for r in rows:
        console.print(
            f"{r.id}  [dim]{r.created_at:%d %b %H:%M} · {r.status}[/]  {esc(r.request[:80])}"
        )


@app.command()
def runs(limit: int = typer.Option(10, min=1, max=100)) -> None:
    """List recent runs."""
    run_with_runtime(lambda rt: list_runs(rt, limit))


# --- products ---


async def resolve(rt: Runtime, ref: str) -> str:
    """A listing id, or a product URL resolved to its first listing id."""
    if not ref.lower().startswith(("http://", "https://", "www.")):
        return ref
    out = await rt.tool_data("resolve_product_link", {"url": ref}, ResolveLinkOutput)
    if out.status != "resolved" or not out.listings:
        render_link(console, out)
        raise ShoppingAgentError("the link could not be matched to a single listing")
    return out.listings[0].listing_id


async def show_link(rt: Runtime, url: str, days: int) -> None:
    if rt.providers.demo_mode:
        demo_banner(console)
    out = await rt.tool_data("resolve_product_link", {"url": url}, ResolveLinkOutput)
    render_link(console, out)
    if out.status != "resolved" or not out.listings:
        return
    lid = out.listings[0].listing_id
    profile = await rt.load_profile()
    offers = await rt.tool_data(
        "find_offers", {"listing_id": lid}, FindOffersOutput, profile=profile
    )
    console.print()
    render_offers(console, offers)
    history = await rt.tool_data(
        "get_price_history", {"listing_id": lid, "days": days}, PriceHistoryOutput
    )
    console.print()
    render_history(console, history, rows=8)


@app.command()
def link(
    url: str = typer.Argument(..., help="Amazon, Flipkart or manufacturer product link."),
    days: int = typer.Option(90, min=1, max=365, help="History period."),
) -> None:
    """Look up a product from its link: offers for your cards and price history."""
    run_with_runtime(lambda rt: show_link(rt, url, days))


async def show_history(rt: Runtime, ref: str, days: int) -> None:
    if rt.providers.demo_mode:
        demo_banner(console)
    lid = await resolve(rt, ref)
    out = await rt.tool_data(
        "get_price_history", {"listing_id": lid, "days": days}, PriceHistoryOutput
    )
    render_history(console, out)


@app.command()
def history(
    ref: str = typer.Argument(..., help="Listing id or product link."),
    days: int = typer.Option(90, min=1, max=365),
) -> None:
    """Observed price history with stats, sparkline and price changes."""
    run_with_runtime(lambda rt: show_history(rt, ref, days))


async def show_offers(rt: Runtime, ref: str) -> None:
    if rt.providers.demo_mode:
        demo_banner(console)
    lid = await resolve(rt, ref)
    profile = await rt.load_profile()
    out = await rt.tool_data("find_offers", {"listing_id": lid}, FindOffersOutput, profile=profile)
    console.print(listing_line(out.listing))
    render_offers(console, out)


@app.command()
def offers(ref: str = typer.Argument(..., help="Listing id or product link.")) -> None:
    """Offers and effective-price scenarios for your saved cards and memberships."""
    run_with_runtime(lambda rt: show_offers(rt, ref))


async def parse_offer(rt: Runtime, text: str, listing: str | None, save: bool) -> None:
    from shopping_agent.agent.analyzers import parse_offer_text
    from shopping_agent.db.repositories import CatalogRepository, OfferRepository
    from shopping_agent.domain.product import Marketplace

    platform = None
    lid = await resolve(rt, listing) if listing else None
    if lid:
        async with rt.sessions() as session:
            row = await CatalogRepository(session).get_listing(lid)
        if row is None:
            raise ShoppingAgentError(f"unknown listing {lid}")
        platform = Marketplace(row.marketplace)
    parsed, warnings = await parse_offer_text(rt.llm(), text, platform=platform, usage=RunUsage())
    for o in parsed:
        benefit = f"{o.percent}%" if o.percent is not None else inr(o.flat_amount)
        cap = f" up to {inr(o.max_discount)}" if o.max_discount else ""
        mov = f", min order {inr(o.min_order_value)}" if o.min_order_value else ""
        who = f", {o.payment.describe()}" if o.payment else ""
        console.print(f"• [bold]{esc(o.title)}[/] ({o.type.value}): {benefit}{cap}{mov}{esc(who)}")
    for w in warnings:
        console.print(f"[yellow]! {esc(w)}[/]")
    if save:
        if not lid:
            raise ShoppingAgentError("--save needs --listing")
        async with rt.sessions() as session, session.begin():
            repo = OfferRepository(session)
            for o in parsed:
                await repo.upsert(o, source="user", now=rt.clock(), listing_id=lid)
        console.print(f"Saved {len(parsed)} offer(s) for listing {lid} (marked user provided).")


@app.command("offer-parse")
def offer_parse(
    text: str = typer.Argument(..., help="Offer terms as shown on the page."),
    listing: str | None = typer.Option(None, help="Listing id or link the offer applies to."),
    save: bool = typer.Option(False, help="Store the offer for that listing."),
) -> None:
    """Read pasted offer terms into structured offers (optionally save them for a listing)."""
    run_with_runtime(lambda rt: parse_offer(rt, text, listing, save))


def _money(value: str) -> Decimal:
    from shopping_agent.services.india_price_parser import parse_amount

    amount = parse_amount(value, allow_plain=True)
    if amount is None:
        try:
            amount = Decimal(value)
        except InvalidOperation as exc:
            raise typer.BadParameter(f"not an amount: {value}") from exc
    return amount


@app.command()
def emi(
    price: str = typer.Argument(..., help="Price, e.g. 65000, 65k or ₹65,000."),
    months: int = typer.Argument(..., min=1, max=60),
    rate: float = typer.Argument(..., min=0, max=60, help="Bank's annual interest rate, %"),
    fee: str = typer.Option("0", help="Processing fee."),
    forgone: str = typer.Option("0", help="Discount you lose by choosing EMI."),
) -> None:
    """True cost of a "No-Cost EMI": GST on interest, fees and forgone discounts."""
    cost = no_cost_emi_true_cost(
        _money(price),
        months,
        Decimal(str(rate)),
        processing_fee=_money(fee),
        discount_forgone=_money(forgone),
    )
    console.print(f"Monthly instalment: [bold]{inr(cost.monthly_installment)}[/] × {months}")
    console.print(f"Interest (refunded as an upfront discount): {inr(cost.interest_component)}")
    console.print(f"GST on interest (you pay this): {inr(cost.gst_on_interest)}")
    console.print(f"Processing fee: {inr(cost.processing_fee_total)}")
    console.print(f"Discount forgone: {inr(cost.discount_forgone)}")
    console.print(f"[bold]Extra cost of choosing EMI: {inr(cost.extra_vs_full_payment)}[/]")
    console.print(f"True cost: {inr(cost.true_cost)} (vs {inr(cost.price)} paid in full)")


def show_budget(budget: Decimal, items_text: str, *, save_most: bool) -> None:
    from shopping_agent.services.budget_planner import (
        ItemParseError,
        SortOrder,
        parse_items,
        plan_budget,
    )

    try:
        slots = parse_items(items_text)
    except ItemParseError as exc:
        raise typer.BadParameter(str(exc)) from exc
    order = SortOrder.SAVE_MOST if save_most else SortOrder.USE_MOST_BUDGET
    plan = plan_budget(slots, budget, sort=order, limit=10)
    if not plan.combinations:
        console.print(f"Nothing fits {inr(plan.budget)}.")
        if plan.shortfall:
            console.print(
                f"The cheapest combination costs {inr(plan.cheapest_total)}, "
                f"{inr(plan.shortfall)} over budget."
            )
        return
    console.print(f"[bold]{plan.feasible_count} combination(s) fit {inr(plan.budget)}[/]")
    for combo in plan.combinations:
        picks = "; ".join(
            f"{esc(p.slot)}: {esc(p.name)}" + (f" ×{p.quantity}" if p.quantity > 1 else "")
            for p in combo.picks
        )
        console.print(f"  {inr(combo.total)} [dim](left {inr(combo.remaining)})[/]  {picks}")


@app.command()
def budget(
    amount: str = typer.Argument(..., help="Total budget, e.g. 1L or 100000."),
    item: list[str] = typer.Option(
        ..., "--item", "-i", help="'slot: product = price', repeatable (slot x2 / slot? allowed)."
    ),
    save_most: bool = typer.Option(False, help="Show the cheapest combinations first."),
) -> None:
    """Plan several purchases within one budget, e.g. a phone and a tablet."""
    show_budget(_money(amount), "\n".join(item), save_most=save_most)


# --- demo ---


async def demo_seed_cmd(rt: Runtime, days: int, force: bool) -> None:
    from shopping_agent.demo import seed_demo

    if not rt.providers.demo_mode and not force:
        raise ShoppingAgentError(
            "real data providers are configured; refusing to mix demo data in (use --force)"
        )
    report = await seed_demo(rt.providers, rt.sessions, now=rt.clock(), days=days)
    console.print(
        f"Seeded {report.listings} demo listings, {report.observations} synthetic price "
        f"observations and {report.sale_events} fictional sale events starting "
        f"{report.sale_start:%d %b %Y}. All of it is labelled as demo data."
    )


@app.command("demo-seed")
def demo_seed(
    days: int = typer.Option(60, min=7, max=365, help="Days of synthetic history."),
    force: bool = typer.Option(False, help="Seed even when real providers are configured."),
) -> None:
    """Load the fictional demo catalog with synthetic price history and a demo sale."""
    run_with_runtime(lambda rt: demo_seed_cmd(rt, days, force))


async def demo_clear_cmd(rt: Runtime) -> None:
    from shopping_agent.demo import clear_demo

    observations, events = await clear_demo(rt.sessions)
    console.print(
        f"Removed {observations} synthetic price observations and {events} fictional sale events."
    )


@app.command("demo-clear")
def demo_clear() -> None:
    """Remove the synthetic demo price history and fictional sale (use before real data)."""
    run_with_runtime(demo_clear_cmd)


# --- profile ---


async def show_profile(rt: Runtime) -> None:
    async with rt.sessions() as session:
        repo = ProfileRepository(session)
        profile = await repo.load()
        prefs = await repo.preferences()
    console.print("[bold]Cards[/] [dim](bank and type only)[/]")
    for m in profile.payment_methods:
        console.print(f"  • {esc(m.describe())}" + (f" ({esc(m.network)})" if m.network else ""))
    if not profile.payment_methods:
        console.print("  none")
    console.print(
        "[bold]Memberships[/]: " + (esc(", ".join(sorted(profile.memberships))) or "none")
    )
    console.print("[bold]Exchange devices[/]:")
    for d in profile.exchange_devices:
        console.print(f"  • {esc(d.category)} {esc(d.model or '')} {esc(d.condition or '')}")
    if not profile.exchange_devices:
        console.print("  none")
    console.print("[bold]Preferences[/]:")
    for k, v in prefs.items():
        console.print(f"  • {esc(k)} = {esc(json.dumps(v))}")
    if not prefs:
        console.print("  none")


async def edit_profile(rt: Runtime, action: Callable[[ProfileRepository], Awaitable[Any]]) -> None:
    async with rt.sessions() as session, session.begin():
        await action(ProfileRepository(session))
    await show_profile(rt)


@profile_app.command("show")
def profile_show() -> None:
    """Show everything saved about you."""
    run_with_runtime(show_profile)


def _card_type(value: str) -> CardType:
    try:
        card = CardType(value.lower())
    except ValueError as exc:
        raise typer.BadParameter("use credit or debit") from exc
    if card is CardType.ANY:
        raise typer.BadParameter("use credit or debit")
    return card


@profile_app.command("add-card")
def add_card(
    issuer: str = typer.Argument(..., help="Bank, e.g. HDFC. Never a card number."),
    card_type: str = typer.Argument(..., help="credit or debit"),
    network: str | None = typer.Option(None, help="visa, mastercard, rupay, amex…"),
) -> None:
    """Save a card by bank and type, used to check bank-offer eligibility."""
    if any(ch.isdigit() for ch in issuer) and sum(ch.isdigit() for ch in issuer) >= 4:
        raise typer.BadParameter("enter the bank name only, never card numbers")
    method = PaymentMethod(issuer=issuer, card_type=_card_type(card_type), network=network)
    run_with_runtime(lambda rt: edit_profile(rt, lambda r: r.add_payment_method(method)))


@profile_app.command("remove-card")
def remove_card(issuer: str, card_type: str) -> None:
    """Remove a saved card."""
    card = _card_type(card_type)
    run_with_runtime(lambda rt: edit_profile(rt, lambda r: r.remove_payment_method(issuer, card)))


@profile_app.command("add-membership")
def add_membership(
    name: str = typer.Argument(..., help="e.g. 'amazon prime', 'flipkart plus'"),
) -> None:
    """Save a membership (affects member-only prices and early access)."""
    run_with_runtime(lambda rt: edit_profile(rt, lambda r: r.add_membership(name)))


@profile_app.command("remove-membership")
def remove_membership(name: str) -> None:
    """Remove a membership."""
    run_with_runtime(lambda rt: edit_profile(rt, lambda r: r.remove_membership(name)))


@profile_app.command("add-device")
def add_device(
    category: str = typer.Argument(..., help="e.g. smartphone"),
    model: str | None = typer.Option(None),
    condition: str | None = typer.Option(None, help="good, average, below average"),
) -> None:
    """Save a device you could exchange."""
    device = ExchangeDevice(category=category, model=model, condition=condition)
    run_with_runtime(lambda rt: edit_profile(rt, lambda r: r.add_exchange_device(device)))


@profile_app.command("set")
def set_pref(
    key: str = typer.Argument(..., help=", ".join(KNOWN_PREFERENCES)),
    value: str = typer.Argument(..., help="JSON or text, e.g. true, 80000, '[\"Acme\"]'"),
) -> None:
    """Save a preference."""
    try:
        parsed: Any = json.loads(value)
    except json.JSONDecodeError:
        parsed = value
    run_with_runtime(lambda rt: edit_profile(rt, lambda r: r.set_preference(key, parsed)))


@profile_app.command("unset")
def unset_pref(key: str) -> None:
    """Delete a preference."""
    run_with_runtime(lambda rt: edit_profile(rt, lambda r: r.delete_preference(key)))


@profile_app.command("forget")
def forget(yes: bool = typer.Option(False, "--yes", help="Confirm deletion.")) -> None:
    """Delete everything saved about you."""
    if not yes:
        console.print(
            "This deletes all saved cards, memberships, devices and preferences. Re-run with --yes."
        )
        raise typer.Exit(1)
    run_with_runtime(lambda rt: edit_profile(rt, lambda r: r.delete_all()))


# --- tracking, alerts, sales, worker ---

watch_app = typer.Typer(help="Track prices and get alerts.", no_args_is_help=True)
sale_app = typer.Typer(help="Sale prep plan, sale-start brief, sale events.", no_args_is_help=True)
worker_app = typer.Typer(help="Background price checks and notifications.", no_args_is_help=True)
app.add_typer(watch_app, name="watch")
app.add_typer(sale_app, name="sale")
app.add_typer(worker_app, name="worker")


def _decimal(value: str | None) -> str | None:
    return str(_money(value)) if value else None


async def watch_add(
    rt: Runtime,
    ref: str,
    *,
    target: str | None,
    effective_target: str | None,
    drop_pct: float | None,
    priority: str,
) -> None:
    from shopping_agent.tracking.views import WatchView

    lid = await resolve(rt, ref)
    args: dict[str, Any] = {"listing_id": lid, "priority": priority}
    if target:
        args["target_price"] = target
    if effective_target:
        args["target_effective"] = effective_target
    if drop_pct:
        args["target_drop_pct"] = str(drop_pct)
    view = await rt.tool_data("track_product", args, WatchView, allow_side_effects=True)
    console.print(f"Tracking [bold]{esc(view.label)}[/] [dim](watch {view.id})[/]")
    await watch_check(rt, view.id)


async def watch_list(rt: Runtime) -> None:
    from shopping_agent.db.repositories.tracking import WatchRepository

    async with rt.sessions() as session:
        rows = await WatchRepository(session).items()
    if not rows:
        console.print(
            "Not tracking anything yet. Try: shop watch add <link> --effective-target 65k"
        )
    for w in rows:
        targets = ", ".join(
            t
            for t in (
                f"listed ≤ {inr(w.target_price)}" if w.target_price else "",
                f"for you ≤ {inr(w.target_effective)}" if w.target_effective else "",
                f"drop ≥ {w.target_drop_pct}%" if w.target_drop_pct else "",
            )
            if t
        )
        state = "" if w.active else " [yellow](paused)[/]"
        checked = (
            f"checked {w.last_checked_at:%d %b %H:%M} UTC" if w.last_checked_at else "not checked"
        )
        console.print(f"[bold]{esc(w.label)}[/]{state}  [dim]{w.id}[/]")
        console.print(
            f"    listed {inr(w.last_price)} · for you {inr(w.last_effective)} · "
            f"targets: {targets or 'none'} · priority {w.priority} · {checked}"
        )
        if w.last_error:
            console.print(f"    [red]last check failed: {esc(w.last_error)}[/]")


async def watch_check(rt: Runtime, watch_id: str | None) -> None:
    from shopping_agent.tracking.monitor import check_watch
    from shopping_agent.tracking.worker import run_once

    if watch_id is None:
        report = await run_once(rt)
        console.print(
            f"Checked {report.checked} item(s), {report.failed} failed; "
            f"{len(report.alerts) + len(report.sale_alerts)} new alert(s)."
        )
        for title in [*report.sale_alerts, *report.alerts]:
            console.print(f"  [green]•[/] {esc(title)}")
        return
    outcome = await check_watch(rt, watch_id, now=rt.clock())
    if not outcome.ok:
        raise ShoppingAgentError(f"check failed: {outcome.error}")
    console.print(
        f"Listed {inr(outcome.listing_price)} · best for you {inr(outcome.best_effective)}"
    )
    for title in outcome.alerts:
        console.print(f"  [green]Alert:[/] {esc(title)}")


@watch_app.command("add")
def watch_add_cmd(
    ref: str = typer.Argument(..., help="Listing id or product link."),
    target: str | None = typer.Option(None, help="Alert when the listed price is at or below."),
    effective_target: str | None = typer.Option(
        None, help="Alert when the best price for your cards is at or below."
    ),
    drop_pct: float | None = typer.Option(None, min=1, max=89, help="Alert on a % drop."),
    priority: str = typer.Option("normal", help="high, normal or low"),
) -> None:
    """Track a product (checks it right away)."""
    if priority not in {"high", "normal", "low"}:
        raise typer.BadParameter("priority must be high, normal or low")
    run_with_runtime(
        lambda rt: watch_add(
            rt,
            ref,
            target=_decimal(target),
            effective_target=_decimal(effective_target),
            drop_pct=drop_pct,
            priority=priority,
        )
    )


@watch_app.command("list")
def watch_list_cmd() -> None:
    """Everything you track, with the last check."""
    run_with_runtime(watch_list)


@watch_app.command("check")
def watch_check_cmd(
    watch_id: str | None = typer.Argument(None, help="One item; omit to run all due checks."),
) -> None:
    """Check prices now."""
    run_with_runtime(lambda rt: watch_check(rt, watch_id))


@watch_app.command("remove")
def watch_remove_cmd(watch_id: str) -> None:
    """Stop tracking an item."""
    from shopping_agent.db.repositories.tracking import WatchRepository

    async def remove(rt: Runtime) -> None:
        async with rt.sessions() as session, session.begin():
            if not await WatchRepository(session).delete(watch_id):
                raise ShoppingAgentError(f"watch item {watch_id} not found")
        console.print("Removed.")

    run_with_runtime(remove)


async def show_inbox(rt: Runtime, *, show_all: bool, mark_read: bool) -> None:
    from shopping_agent.db.repositories.tracking import NotificationRepository

    async with rt.sessions() as session, session.begin():
        repo = NotificationRepository(session)
        items = await repo.inbox(unread_only=not show_all, limit=30)
        if mark_read:
            await repo.mark_read(rt.clock())
    if not items:
        console.print("No new alerts." if not show_all else "No alerts yet.")
    for note, alert in items:
        new = "" if note.read_at else "[green]new[/] "
        console.print(
            f"{new}[bold]{esc(alert.title)}[/] [dim]{alert.created_at:%d %b %H:%M} UTC[/]"
        )
        console.print(f"  {esc(alert.message)}")


@app.command()
def inbox(
    show_all: bool = typer.Option(False, "--all", help="Include alerts already read."),
    keep_unread: bool = typer.Option(False, help="Don't mark them as read."),
) -> None:
    """Your alerts: price targets met, offer changes, sales starting."""
    run_with_runtime(lambda rt: show_inbox(rt, show_all=show_all, mark_read=not keep_unread))


async def confirm_cmd(rt: Runtime, run_id: str, action_id: str, target: str | None) -> None:
    from shopping_agent.tracking.actions import ConfirmOverrides, confirm_action

    overrides = ConfirmOverrides(target_price=_money(target) if target else None)
    action, _ = await confirm_action(rt, run_id, action_id, overrides)
    console.print(f"Done: {esc(action.description)}")


@app.command()
def confirm(
    run_id: str,
    action_id: str,
    target: str | None = typer.Option(None, help="Change the target price."),
) -> None:
    """Carry out an action the agent suggested (shown under 'Suggested next steps')."""
    run_with_runtime(lambda rt: confirm_cmd(rt, run_id, action_id, target))


async def show_prep(rt: Runtime, platform: str | None) -> None:
    from shopping_agent.domain.product import Marketplace
    from shopping_agent.tracking.sales import prep_plan

    plan = await prep_plan(rt, now=rt.clock(), platform=Marketplace(platform) if platform else None)
    console.print(f"[bold]{esc(plan.summary)}[/]")
    for step in plan.steps:
        mark = {True: "[green]✓[/]", False: "[yellow]○[/]", None: "[dim]•[/]"}[step.done]
        console.print(f" {mark} [bold]{esc(step.title)}[/]: {esc(step.detail)}")


async def show_brief(rt: Runtime) -> None:
    from shopping_agent.tracking.sales import current_brief

    brief = await current_brief(rt, now=rt.clock())
    if brief is None:
        console.print("No upcoming or live sale is recorded.")
        return
    console.print(f"[bold]{esc(brief.summary)}[/]")
    for item in brief.items:
        console.print(f"  • {esc(item.line)}")


@sale_app.command("prep")
def sale_prep_cmd(platform: str | None = typer.Option(None, help="amazon, flipkart, …")) -> None:
    """Checklist for the next sale: dates, tracking, baselines, targets, cards, early access."""
    run_with_runtime(lambda rt: show_prep(rt, platform))


@sale_app.command("brief")
def sale_brief_cmd() -> None:
    """Each tracked item against its pre-sale price, for the live or next sale."""
    run_with_runtime(show_brief)


@sale_app.command("add")
def sale_add_cmd(
    name: str,
    platform: str = typer.Option(..., help="amazon, flipkart, …"),
    start: str = typer.Option(..., help="Start, e.g. 2026-10-10 or 2026-10-10T00:00 (IST)"),
    end: str = typer.Option(..., help="End, same format"),
    early_access_hours: int = typer.Option(0, min=0, max=72),
    early_access_for: str | None = typer.Option(None, help="e.g. 'flipkart plus'"),
    bank: list[str] = typer.Option([], help="Partner bank (repeatable)"),
    source: list[str] = typer.Option([], help="Where you saw the dates (repeatable)"),
) -> None:
    """Record a sale you know about (marked user-provided, not verified)."""
    from datetime import datetime as dt

    from shopping_agent.db.repositories import SaleEventRepository
    from shopping_agent.domain.common import Verification
    from shopping_agent.domain.product import Marketplace
    from shopping_agent.domain.sale import PhaseKind, SaleEvent, SalePhase
    from shopping_agent.services.price_baseline import IST

    def parse(value: str) -> dt:
        parsed = dt.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=IST)

    begin, finish = parse(start), parse(end)
    if finish <= begin:
        raise typer.BadParameter("end must be after start")
    phases = [SalePhase(name="Main sale", kind=PhaseKind.MAIN, start=begin, end=finish)]
    if early_access_hours:
        phases.insert(
            0,
            SalePhase(
                name="Early access",
                kind=PhaseKind.EARLY_ACCESS,
                start=begin - timedelta(hours=early_access_hours),
                end=begin,
                eligibility=early_access_for,
            ),
        )

    async def add(rt: Runtime) -> None:
        event = SaleEvent(
            name=name,
            platform=Marketplace(platform),
            start=begin,
            end=finish,
            phases=phases,
            partner_banks=bank,
            official_sources=source,
            verification=Verification.USER_PROVIDED,
            last_verified_at=rt.clock(),
        )
        async with rt.sessions() as session, session.begin():
            await SaleEventRepository(session).upsert(event)
        console.print(f"Recorded {esc(name)} on {platform}.")
        await show_prep(rt, platform)

    run_with_runtime(add)


@worker_app.command("run")
def worker_run(once: bool = typer.Option(False, help="Run a single tick and exit.")) -> None:
    """Run the background worker: sale announcements, due price checks, notifications."""
    from shopping_agent.tracking.worker import TickReport, run_worker

    async def show(report: TickReport) -> None:
        console.print(
            f"{report.at:%H:%M:%S} checked {report.checked}, failed {report.failed}, "
            f"alerts {len(report.alerts) + len(report.sale_alerts)}, sent {report.delivered}"
        )
        for title in [*report.sale_alerts, *report.alerts]:
            console.print(f"  [green]•[/] {esc(title)}")

    async def go(rt: Runtime) -> None:
        console.print(
            "Running one tick…"
            if once
            else f"Worker started (tick {rt.settings.worker_tick_seconds}s). Ctrl+C to stop."
        )
        await run_worker(rt, once=once, on_tick=show)

    try:
        run_with_runtime(go)
    except KeyboardInterrupt:
        console.print("Stopped.")


# --- evaluation ---

eval_app = typer.Typer(help="Evaluate the agent on the scenario dataset.", no_args_is_help=True)
app.add_typer(eval_app, name="eval")
DEFAULT_DATASET = Path("evals/dataset.jsonl")


@eval_app.command("list")
def eval_list(dataset: Path = typer.Option(DEFAULT_DATASET)) -> None:
    """List the evaluation scenarios."""
    from shopping_agent.evals.dataset import load_dataset

    for s in load_dataset(dataset):
        console.print(f"[bold]{s.id}[/] [dim]{', '.join(s.tags)}[/]  {esc(s.request[:90])}")


@eval_app.command("run")
def eval_run(
    dataset: Path = typer.Option(DEFAULT_DATASET, help="Scenario file (JSON lines)."),
    only: str | None = typer.Option(None, help="Comma-separated scenario ids."),
    tag: list[str] = typer.Option([], help="Run scenarios with this tag (repeatable)."),
    model: str | None = typer.Option(None, help="provider:model for every role, e.g. groq:..."),
    out: Path = typer.Option(Path("evals/reports"), help="Report directory."),
    name: str | None = typer.Option(None, help="Report file name, e.g. baseline."),
) -> None:
    """Run the agent on the scenarios (real model, demo data) and write a report.

    Uses EVAL_DATABASE_URL (default: TEST_DATABASE_URL), which is wiped before each scenario.
    Each scenario makes roughly 8-16 model calls, so mind free-tier quotas."""
    from shopping_agent.evals.dataset import load_dataset, select
    from shopping_agent.evals.metrics import METRIC_LABELS, ScenarioResult
    from shopping_agent.evals.report import write_report
    from shopping_agent.evals.runner import run_eval

    settings = get_settings()
    if model:
        settings = settings.model_copy(
            update={
                "llm_model": model,
                "llm_model_extraction": "",
                "llm_model_research": "",
                "llm_model_synthesis": "",
                "llm_model_analysis": "",
            }
        )
    scenarios = select(
        load_dataset(dataset), only=only.split(",") if only else None, tags=tag or None
    )
    if not scenarios:
        console.print("No scenarios selected.")
        raise typer.Exit(1)
    console.print(f"Running {len(scenarios)} scenario(s) with {esc(settings.llm_model)}…")

    async def progress(r: ScenarioResult) -> None:
        mark = "[green]✓[/]" if r.passed else "[red]✗[/]"
        why = f"  [dim]{esc('; '.join(r.failures)[:160])}[/]" if r.failures else ""
        console.print(f"{mark} {esc(r.id)} [dim]{r.latency_s:.0f}s, {r.llm_calls} calls[/]{why}")

    async def go() -> None:
        report = await run_eval(
            scenarios, settings=settings, dataset_name=str(dataset), on_result=progress
        )
        json_path, md_path = write_report(report, out, name)
        s = report.summary
        console.print(f"\n[bold]{s.passed}/{s.scenarios} scenarios passed[/]")
        for metric, value in s.metrics.items():
            label = METRIC_LABELS.get(metric, metric)
            console.print(f"  {label}: {value.mean * 100:.0f}% [dim](n={value.n})[/]")
        console.print(f"Report: {md_path} (data: {json_path})")

    try:
        asyncio.run(go())
    except ShoppingAgentError as exc:
        console.print(f"[red]Error:[/] {esc(exc)}")
        raise typer.Exit(1) from exc


# --- misc ---


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@app.command()
def parse_link(url: str) -> None:
    """Show what a product link identifies (no network access)."""
    try:
        parsed = parse_product_url(url)
    except UrlParseError as exc:
        console.print(f"[red]{esc(exc)}[/]")
        raise typer.Exit(1) from exc
    console.print_json(parsed.model_dump_json())


@app.command()
def serve(
    host: str | None = typer.Option(None, help="Bind host (default from settings)."),
    port: int | None = typer.Option(None, help="Bind port (default from settings)."),
    reload: bool = typer.Option(False, help="Auto-reload on code changes."),
) -> None:
    """Run the API and web UI."""
    settings = get_settings()
    uvicorn.run(
        "shopping_agent.api.app:create_app",
        factory=True,
        host=host or settings.api_host,
        port=port or settings.api_port,
        reload=reload,
    )


if __name__ == "__main__":
    app()
