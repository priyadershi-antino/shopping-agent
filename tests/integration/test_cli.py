"""CLI: pure commands through Typer, data commands through their async functions."""

import pytest
from rich.console import Console
from typer.testing import CliRunner

from shopping_agent.cli import main as cli
from shopping_agent.db.repositories import PriceRepository, SaleEventRepository
from shopping_agent.runtime import Runtime

runner = CliRunner()
DEMO_AMAZON = "https://www.amazon.in/Acme-Nimbus/dp/B0DEMO0001?tag=aff-21"


@pytest.fixture
def out(monkeypatch: pytest.MonkeyPatch) -> Console:
    console = Console(record=True, width=140, highlight=False)
    monkeypatch.setattr(cli, "console", console)
    return console


def test_emi_command() -> None:
    result = runner.invoke(cli.app, ["emi", "65k", "6", "15", "--fee", "199"])
    assert result.exit_code == 0, result.output
    assert "Extra cost of choosing EMI" in result.output
    assert "₹10,833" in result.output


def test_parse_link_command() -> None:
    result = runner.invoke(cli.app, ["parse-link", DEMO_AMAZON])
    assert result.exit_code == 0
    assert '"external_id": "B0DEMO0001"' in result.output
    bad = runner.invoke(cli.app, ["parse-link", "ftp://nope"])
    assert bad.exit_code == 1


def test_add_card_rejects_card_numbers() -> None:
    result = runner.invoke(cli.app, ["profile", "add-card", "4111 1111 1111", "credit"])
    assert result.exit_code != 0
    assert "never card numbers" in result.output


@pytest.mark.db
async def test_ask_renders_computed_prices(runtime: Runtime, out: Console) -> None:
    run_id = await cli.ask_once(runtime, "laptop under 80k for coding, I have an HDFC credit card")
    text = out.export_text()
    assert "DEMO DATA" in text
    assert "Computed prices (code, not the model)" in text
    assert "[unverified]" in text  # escaped, not swallowed as Rich markup
    assert f"run {run_id}" in text
    await cli.list_runs(runtime, 5)
    assert run_id in out.export_text()


@pytest.mark.db
async def test_link_history_offers_and_seed(runtime: Runtime, out: Console) -> None:
    await cli.demo_seed_cmd(runtime, 30, force=False)
    async with runtime.sessions() as session:
        events = await SaleEventRepository(session).list()
    assert {e.name for e in events} == {"DEMO Festive Sale (fictional)"}

    await cli.edit_profile(runtime, lambda r: r.add_membership("amazon prime"))
    await cli.show_link(runtime, DEMO_AMAZON, 90)
    text = out.export_text()
    assert "Found the exact listing" in text
    assert "Price scenarios" in text
    assert "30 days observed" in text or "31 days observed" in text

    listing_id = await cli.resolve(runtime, DEMO_AMAZON)
    async with runtime.sessions() as session:
        before = len(await PriceRepository(session).history([listing_id]))
    await cli.demo_seed_cmd(runtime, 30, force=False)  # idempotent
    async with runtime.sessions() as session:
        assert len(await PriceRepository(session).history([listing_id])) == before

    await cli.show_history(runtime, listing_id, 90)
    await cli.show_offers(runtime, listing_id)
    assert "Offers found" in out.export_text()

    await cli.demo_clear_cmd(runtime)
    assert "fictional sale events" in out.export_text()
    async with runtime.sessions() as session:
        assert await SaleEventRepository(session).list() == []
        assert len(await PriceRepository(session).history([listing_id])) == 1  # the current price


@pytest.mark.db
async def test_profile_commands(runtime: Runtime, out: Console) -> None:
    from shopping_agent.domain.offer import CardType, PaymentMethod

    method = PaymentMethod(issuer="Axis", card_type=CardType.CREDIT, network="visa")
    await cli.edit_profile(runtime, lambda r: r.add_payment_method(method))
    await cli.edit_profile(runtime, lambda r: r.set_preference("typical_budget", 80000))
    text = out.export_text()
    assert "Axis credit card (visa)" in text
    assert "typical_budget = 80000" in text
    await cli.edit_profile(runtime, lambda r: r.delete_all())
    assert "none" in out.export_text()


def test_budget_command() -> None:
    result = runner.invoke(
        cli.app,
        [
            "budget",
            "1L",
            "-i",
            "phone: Nova Pixelon 9 = 30,599",
            "-i",
            "phone: Acme Volt 5G = 18,999",
            "-i",
            "tablet: Zenith Tab S = 27,999",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "2 combination(s) fit ₹1,00,000" in result.output
    too_small = runner.invoke(cli.app, ["budget", "20k", "-i", "tablet: Zenith Tab S = 27,999"])
    assert "Nothing fits ₹20,000" in too_small.output
    bad = runner.invoke(cli.app, ["budget", "1L", "-i", "phone Nova"])
    assert bad.exit_code != 0
