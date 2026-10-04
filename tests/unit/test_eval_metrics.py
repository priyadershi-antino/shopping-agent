from decimal import Decimal
from pathlib import Path

from shopping_agent.agent.schemas import FinalAnswer, Intent, Recommendation
from shopping_agent.agent.state import AgentState, FocusItem, RunStatus
from shopping_agent.domain.product import Marketplace
from shopping_agent.evals.dataset import Expect, GoldenPrice, Scenario, load_dataset
from shopping_agent.evals.metrics import _unmarked, f1, score, summarize
from shopping_agent.providers.fixtures import load_catalog
from shopping_agent.services.grounding import GroundingReport, MentionKind, NumberMention
from shopping_agent.services.requirement_match import Level, MatchStatus, RequirementCheck
from tests.factories import NOW, listing_summary

DATASET = Path(__file__).parents[2] / "evals" / "dataset.jsonl"


def answer(listing_id: str, summary: str = "Top pick is great.") -> FinalAnswer:
    return FinalAnswer(
        summary=summary,
        recommendations=[
            Recommendation(
                listing_id=listing_id,
                product="Nova Swift 16",
                verdict="top_pick",
                price_summary="₹64,990 with HDFC credit card",
                why=[],
                trade_offs=[],
                evidence_ids=[],
            )
        ],
        deal_insights=[],
        timing=None,
        risks_and_conflicts=[],
        uncertainties=[],
        questions_for_user=[],
        suggested_actions=[],
    )


def state_with_top(price: str, *, hard_fail: bool = False, summary: str = "ok") -> AgentState:
    listing = listing_summary(
        title="Nova Swift 16 NS16-R5 (16 GB/512 GB SSD/RTX 4060)",
        marketplace=Marketplace.FLIPKART,
        price=Decimal("68990"),
    )
    checks = [
        RequirementCheck(
            requirement="ram",
            level=Level.HARD,
            status=MatchStatus.FAILS if hard_fail else MatchStatus.MEETS,
            expected="16",
            actual="16",
        )
    ]
    state = AgentState(created_at=NOW, request="x", status=RunStatus.COMPLETED)
    state.intent = Intent.DISCOVER
    state.listings[listing.listing_id] = listing
    state.focus = [FocusItem(listing=listing, best_eligible=Decimal(price), checks=checks)]
    state.final = answer(listing.listing_id, summary)
    state.grounding = GroundingReport(checked=4)
    return state


def test_f1_and_unmarked() -> None:
    assert f1(set(), set()) == 1.0
    assert f1({"hdfc"}, {"hdfc", "sbi"}) == 2 * 1 * 0.5 / 1.5
    assert f1({"axis"}, {"hdfc"}) == 0.0
    assert _unmarked("costs ₹9,999 today", "9,999")
    assert not _unmarked("costs ₹9,999 [unverified] today", "9,999")


def test_score_golden_constraints_and_safety() -> None:
    scenario = Scenario(
        id="s",
        request="laptop",
        expect=Expect(
            intents=[Intent.DISCOVER],
            top_models=["NS16-R5"],
            golden=[
                GoldenPrice(marketplace="flipkart", title_contains="ns16-r5", best_eligible=64990)
            ],
        ),
    )
    good = score(scenario, state_with_top("64990"), 12.0)
    assert good.passed, good.failures
    assert good.metrics["effective_price"] == 1.0
    assert good.metrics["constraints"] == 1.0
    assert good.metrics["safety"] == 1.0

    bad = score(
        scenario, state_with_top("66990", hard_fail=True, summary="Lowest ever price!"), 3.0
    )
    assert bad.metrics["effective_price"] == 0.0
    assert bad.metrics["constraints"] == 0.0
    assert bad.metrics["safety"] == 0.0
    assert any(f.startswith("effective_price") for f in bad.failures)


def test_grounding_threshold_and_summary() -> None:
    state = state_with_top("64990")
    state.grounding = GroundingReport(
        checked=10,
        ungrounded=[
            NumberMention(raw="₹1", value=Decimal(1), kind=MentionKind.CURRENCY, start=0, end=2)
        ],
    )
    result = score(Scenario(id="g", request="x"), state, 1.0)
    assert result.metrics["grounding"] == 0.9
    assert result.passed  # 90% meets the grounding threshold
    summary = summarize([result, score(Scenario(id="h", request="y"), state_with_top("1"), 3.0)])
    assert summary.scenarios == 2
    assert summary.metrics["grounding"].n == 2


def test_dataset_is_valid_and_golden_listings_exist() -> None:
    scenarios = load_dataset(DATASET)
    assert len(scenarios) >= 30
    listings = load_catalog()["listings"]
    for s in scenarios:
        for g in s.expect.golden:
            assert any(
                item["marketplace"] == g.marketplace
                and g.title_contains.lower() in item["title"].lower()
                for item in listings
            ), (s.id, g.title_contains)
