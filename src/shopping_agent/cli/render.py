"""Rich rendering for the CLI. Every piece of external or model text goes through `esc`
(Rich would otherwise read "[unverified]" or "[bold]" inside product titles as markup)."""

from collections.abc import Sequence
from decimal import Decimal

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from shopping_agent.agent.views import RunView
from shopping_agent.domain.offer import PriceScenario
from shopping_agent.services.formatting import format_inr
from shopping_agent.tools.catalog_tools import ResolveLinkOutput
from shopping_agent.tools.common import ListingSummary
from shopping_agent.tools.deal_tools import FindOffersOutput, PriceHistoryOutput

STATUS_MARK = {
    "meets": "[green]✓ meets[/]",
    "partial": "[yellow]◐ partial[/]",
    "fails": "[red]✗ fails[/]",
    "unknown": "[dim]? unknown[/]",
}
VERDICTS = {
    "top_pick": "Top pick",
    "strong_alternative": "Strong alternative",
    "budget_pick": "Budget pick",
    "consider": "Consider",
    "not_recommended": "Not recommended",
}
SPARKS = "▁▂▃▄▅▆▇█"


def esc(value: object) -> str:
    return escape(str(value))


def inr(value: Decimal | None) -> str:
    return format_inr(value) if value is not None else "–"


def sparkline(values: Sequence[Decimal | float]) -> str:
    if not values:
        return ""
    nums = [float(v) for v in values]
    low, high = min(nums), max(nums)
    if high == low:
        return SPARKS[3] * len(nums)
    return "".join(SPARKS[round((v - low) / (high - low) * (len(SPARKS) - 1))] for v in nums)


def demo_banner(console: Console) -> None:
    console.print(
        "[black on yellow] DEMO DATA [/] fictional catalog (Acme, Zenith, Nova, Orbit). "
        "Add SERPAPI_API_KEY / TAVILY_API_KEY for real data.",
        highlight=False,
    )


def listing_line(listing: ListingSummary) -> str:
    seller = f" · {esc(listing.seller)}" if listing.seller else ""
    flags = ""
    if listing.condition.value != "new":
        flags += f" [yellow]({esc(listing.condition.value)})[/]"
    if listing.stale:
        flags += " [yellow](price may be out of date)[/]"
    return (
        f"[bold]{esc(listing.title[:100])}[/]\n"
        f"  {esc(listing.marketplace.value)}{seller} · listed {inr(listing.price)}{flags}"
        f"  [dim]id {esc(listing.listing_id)}[/]"
    )


def scenarios_table(scenarios: list[PriceScenario], title: str = "Price scenarios") -> Table:
    table = Table(title=title, title_justify="left", show_lines=False)
    table.add_column("Scenario")
    table.add_column("Pay at checkout", justify="right")
    table.add_column("Cashback later", justify="right")
    table.add_column("Net cost", justify="right")
    table.add_column("Conditions / offers")
    for s in scenarios:
        upfront = (
            f"{inr(s.upfront_best)} – {inr(s.upfront_conservative)}"
            if s.is_range
            else inr(s.upfront_conservative)
        )
        notes = "; ".join(a.title for a in s.applied) or "no offers"
        if s.conditional:
            notes = f"[yellow]needs: {esc('; '.join(s.conditions))}[/]\n{esc(notes)}"
        else:
            notes = esc(notes)
        if s.verification.value != "verified":
            notes += f" [dim]({esc(s.verification.value.replace('_', ' '))})[/]"
        table.add_row(
            f"{s.label} {esc(s.name)}",
            upfront,
            inr(s.deferred_cashback) if s.deferred_cashback else "–",
            f"[bold]{inr(s.net_after_cashback)}[/]",
            notes,
        )
    return table


def render_offers(console: Console, out: FindOffersOutput) -> None:
    for note in out.notes:
        console.print(f"[dim]{esc(note)}[/]")
    if out.scenarios:
        console.print(scenarios_table(out.scenarios))
    if out.card_matrix:
        cards = Table(title="Price with each of your cards", title_justify="left")
        cards.add_column("Payment")
        cards.add_column("Net cost", justify="right")
        for row in out.card_matrix:
            cards.add_row(esc(row.payment), inr(row.best.net_after_cashback))
        console.print(cards)
    if out.offers:
        offers = Table(title="Offers found", title_justify="left")
        offers.add_column("Offer")
        offers.add_column("Type")
        offers.add_column("For you")
        offers.add_column("Source")
        for v in out.offers:
            why = "; ".join(v.eligibility.reasons or v.eligibility.conditions)
            offers.add_row(
                esc(v.offer.title),
                v.offer.type.value,
                f"{v.eligibility.status.value}" + (f" [dim]{esc(why)}[/]" if why else ""),
                f"{esc(v.source)} [dim]({v.offer.verification.value})[/]",
            )
        console.print(offers)
    unknown = [p for p in out.stacking if p.status.value == "unknown"]
    if unknown:
        console.print(
            f"[dim]{len(unknown)} offer pair(s) have unknown stacking terms, "
            "so they were not combined.[/]"
        )


def render_history(console: Console, out: PriceHistoryOutput, *, rows: int = 14) -> None:
    console.print(f"[dim]{esc(out.coverage_note)}[/]")
    for h in out.listings:
        console.print(listing_line(h.listing))
        b = h.baseline
        if b is None:
            console.print("  No observations yet.")
            continue
        console.print(
            f"  low {inr(b.minimum)} · high {inr(b.maximum)} · average {inr(b.average)} · "
            f"{b.coverage_days} days observed ({b.tier.value}) · trend {b.trend.value}"
        )
        if h.low_note:
            console.print(f"  [green]{esc(h.low_note)}[/]")
        if h.pre_sale_hike and h.pre_sale_hike.detected:
            console.print(
                f"  [yellow]! possible pre-sale price rise:[/] {esc(h.pre_sale_hike.reason)}"
            )
        closes = [p.price for p in h.daily]
        console.print(f"  {sparkline(closes)}  [dim]({len(closes)} days)[/]")
        changes = [h.daily[0]] + [
            p for prev, p in zip(h.daily, h.daily[1:], strict=False) if p.price != prev.price
        ]
        table = Table(title="Price changes", title_justify="left", box=None)
        table.add_column("Date")
        table.add_column("Price", justify="right")
        for p in list(reversed(changes))[:rows]:
            table.add_row(f"{p.date:%d %b %Y}", inr(p.price))
        console.print(table)


def render_link(console: Console, out: ResolveLinkOutput) -> None:
    console.print(esc(out.message))
    for s in out.listings:
        console.print(listing_line(s))
    for c in out.candidates:
        first = c.listings[0].listing_id if c.listings else "–"
        console.print(
            f"  • {esc(c.title[:90])}  from {inr(c.lowest_price)}  [dim]listing {first}[/]"
        )


def render_run(console: Console, run: RunView) -> None:
    if run.demo_data:
        demo_banner(console)
    if run.status == "needs_clarification":
        console.print(f"\n[bold]One question first:[/] {esc(run.clarification_question)}")
        return
    if run.status == "failed":
        console.print(f"\n[red]Could not finish:[/] {esc(run.error)}")
    answer = run.answer
    if answer is not None:
        console.print(f"\n{esc(answer.summary)}")
        if run.unverified_figures:
            console.print(
                f"[yellow]! {run.unverified_figures} figure(s) marked {esc('[unverified]')} could "
                "not be traced to a source; do not rely on them.[/]"
            )
        for r in answer.recommendations:
            verdict = VERDICTS.get(r.verdict, r.verdict)
            console.print(
                f"\n[bold]{esc(r.product)}[/]  [cyan]{verdict}[/]  [dim]{esc(r.listing_id)}[/]"
            )
            console.print(f"  {esc(r.price_summary)}")
            for w in r.why:
                console.print(f"  + {esc(w)}")
            for t in r.trade_offs:
                console.print(f"  − {esc(t)}")
        for title, lines in (
            ("Deal insights", answer.deal_insights),
            ("Risks and conflicts", answer.risks_and_conflicts),
            ("To narrow it down", answer.questions_for_user),
        ):
            if lines:
                console.print(f"\n[bold]{title}[/]")
                for line in lines:
                    console.print(f"  • {esc(line)}")
        if answer.timing:
            console.print(f"\n[bold]Buy now or wait?[/] {esc(answer.timing)}")

    if run.items:
        table = Table(title="\nComputed prices (code, not the model)", title_justify="left")
        table.add_column("Product")
        table.add_column("Listed", justify="right")
        table.add_column("Best for you", justify="right")
        table.add_column("With conditions", justify="right")
        table.add_column("Requirements")
        for it in run.items:
            counts: dict[str, int] = {}
            for c in it.checks:
                counts[c.status.value] = counts.get(c.status.value, 0) + 1
            req = ", ".join(f"{n} {k}" for k, n in counts.items()) or "–"
            cond = ""
            if it.best_conditional is not None and it.best_conditional < it.best_eligible:
                needs = next(
                    (
                        s.conditions
                        for s in it.scenarios
                        if s.conditional and s.net_after_cashback == it.best_conditional
                    ),
                    [],
                )
                cond = inr(it.best_conditional) + (
                    f"\n[dim]{esc('; '.join(needs))}[/]" if needs else ""
                )
            table.add_row(
                f"{esc(it.listing.title[:60])}\n[dim]{esc(it.listing.marketplace.value)} · "
                f"{esc(it.listing.listing_id)}[/]",
                inr(it.listing.price),
                f"[bold]{inr(it.best_eligible)}[/]",
                cond or "–",
                req,
            )
        console.print(table)
    if run.proposed_actions:
        console.print(
            "\n[bold]Suggested next steps[/] [dim](nothing is saved until you confirm)[/]"
        )
        for a in run.proposed_actions:
            target = f" target {inr(a.target_price)}" if a.target_price else ""
            console.print(
                f"  • {esc(a.description)}{target}  [dim]{a.status} · "
                f"shop confirm {esc(run.run_id)} {esc(a.id)}[/]"
            )
    if run.uncertainties:
        console.print("\n[bold]Not sure about[/]")
        for u in run.uncertainties:
            console.print(f"  • {esc(u)}")
    if run.conflicts:
        console.print("\n[bold]Conflicting information[/]")
        for conflict in run.conflicts:
            console.print(f"  • {esc(conflict)}")
    if run.sources:
        console.print("\n[bold]Sources[/]")
        for i, s in enumerate(run.sources, start=1):
            where = f" {esc(s.url)}" if s.url else ""
            console.print(
                f"  {esc(f'[{i}]')} {esc(s.claim)} "
                f"[dim]— {esc(s.source_name)}, {s.verification}{where}[/]"
            )
    console.print(
        f"\n[dim]run {esc(run.run_id)} · {run.tool_calls} tool calls · "
        f"{run.llm_calls} model calls · {run.input_tokens}/{run.output_tokens} tokens[/]"
    )
