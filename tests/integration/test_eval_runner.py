"""The evaluation runner end to end with the scripted model on the test database."""

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from shopping_agent.config import Settings, get_settings
from shopping_agent.errors import ConfigurationError
from shopping_agent.evals.dataset import load_dataset, select
from shopping_agent.evals.metrics import ScenarioResult
from shopping_agent.evals.report import to_markdown, write_report
from shopping_agent.evals.runner import run_eval
from tests.factories import NOW
from tests.fakes import scripted_client
from tests.scenarios import discovery_handler

pytestmark = pytest.mark.db

DATASET = Path(__file__).parents[2] / "evals" / "dataset.jsonl"


async def test_runner_scores_and_reports(migrated_engine: AsyncEngine, tmp_path: Path) -> None:
    scenarios = select(
        load_dataset(DATASET), only=["disc-laptop-hdfc", "vague-1", "outage-products"]
    )
    llm, _ = scripted_client(discovery_handler)
    seen: list[str] = []

    async def collect(result: ScenarioResult) -> None:
        seen.append(result.id)

    report = await run_eval(
        scenarios,
        settings=Settings(log_json=False),
        dataset_name="evals/dataset.jsonl",
        database_url=get_settings().test_database_url.get_secret_value(),
        llm=llm,
        clock=lambda: NOW,
        run_migrations=False,
        on_result=collect,
    )
    assert seen == ["disc-laptop-hdfc", "vague-1", "outage-products"]
    by_id = {r.id: r for r in report.results}

    discovery = by_id["disc-laptop-hdfc"]
    assert discovery.status == "completed"
    assert discovery.metrics["effective_price"] == 1.0  # HDFC price computed by the engine
    assert discovery.metrics["payment_f1"] == 1.0
    assert discovery.metrics["tool_recall"] == 1.0

    # The scripted model never asks questions, so the vague scenario must fail clarification.
    assert by_id["vague-1"].metrics["clarification"] == 0.0
    assert not by_id["vague-1"].passed

    outage = by_id["outage-products"]
    assert outage.status == "completed"
    assert outage.metrics["no_recommendations"] == 1.0  # nothing invented without data

    assert report.summary.scenarios == 3
    md = to_markdown(report)
    assert "## Summary" in md and "`vague-1`" in md and "## Failures" in md
    json_path, md_path = write_report(report, tmp_path, "baseline")
    assert json_path.name == "baseline.json" and md_path.exists()


async def test_runner_refuses_the_main_database() -> None:
    settings = Settings(log_json=False)
    with pytest.raises(ConfigurationError):
        await run_eval(
            [],
            settings=settings,
            dataset_name="x",
            database_url=settings.database_url.get_secret_value(),
            run_migrations=False,
        )
