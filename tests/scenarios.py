"""Scripted LLM scenarios shared by agent, API, web and CLI tests."""

import json
import re
from typing import Any

from shopping_agent.llm.types import Message, ResponseSchema, ToolCall, ToolDef
from tests.fakes import analysis_json, facts_from, last_tool_payload, research_turns

PLAN = json.dumps(
    {
        "steps": [
            {"tool": "search_products", "goal": "find laptops"},
            {"tool": "get_product_details", "goal": "verify specs"},
            {"tool": "not_a_tool", "goal": "dropped"},
        ]
    }
)


def final_answer(listing_id: str, price: str, extra_listing: str | None = None) -> str:
    recs = [
        {
            "listing_id": listing_id,
            "product": "Top laptop",
            "verdict": "top_pick",
            "price_summary": f"{price} with your HDFC credit card",
            "why": ["fits budget"],
            "trade_offs": [],
            "evidence_ids": [],
        }
    ]
    if extra_listing:
        recs.append({**recs[0], "listing_id": extra_listing, "verdict": "consider"})
    return json.dumps(
        {
            "summary": "Best value is the top pick. Some site claims it costs ₹9,999.",
            "recommendations": recs,
            "deal_insights": [],
            "timing": None,
            "risks_and_conflicts": [],
            "uncertainties": [],
            "questions_for_user": [],
            "suggested_actions": [
                {
                    "action": "price_alert",
                    "listing_id": listing_id,
                    "target_price": 60000,
                    "detail": "alert",
                },
                {
                    "action": "track_price",
                    "listing_id": "ghost",
                    "target_price": None,
                    "detail": "bad id",
                },
            ],
        }
    )


def review_analysis(messages: list[Message]) -> str:
    urls = re.findall(r"\((https://[^,)\s]+)", messages[-1].content)
    return json.dumps(
        {
            "themes": [
                {
                    "kind": "complaint",
                    "theme": "loud fans",
                    "mentions": 1,
                    "source_urls": urls[:1],
                    "use_case": "gaming",
                },
                {
                    "kind": "positive",
                    "theme": "invented",
                    "mentions": 1,
                    "source_urls": ["https://nowhere"],
                    "use_case": None,
                },
            ]
        }
    )


def discovery_handler(
    messages: list[Message], tools: list[ToolDef] | None, schema: ResponseSchema | None
) -> Any:
    if schema is not None:
        if schema.name == "RequestAnalysis":
            return analysis_json()
        if schema.name == "ResearchPlan":
            return PLAN
        if schema.name == "ReviewAnalysis":
            return review_analysis(messages)
        if schema.name == "FinalAnswer":
            facts = facts_from(messages)
            if not facts.get("candidates_in_order"):
                return json.dumps(
                    {
                        "summary": "Product data is unavailable right now, so I could not "
                        "find options.",
                        "recommendations": [],
                        "deal_insights": [],
                        "timing": None,
                        "risks_and_conflicts": [],
                        "uncertainties": ["product search failed"],
                        "questions_for_user": [],
                        "suggested_actions": [],
                    }
                )
            top = facts["candidates_in_order"][0]
            return final_answer(
                top["listing_id"], top["best_unconditional_price"], extra_listing="made-up-id"
            )
        raise AssertionError(schema.name)
    turn = research_turns(messages)
    if turn == 0:
        return [
            ToolCall(
                id="c1",
                name="search_products",
                arguments={"query": "gaming laptop", "category": "laptop", "max_price": 80000},
            )
        ]
    if turn == 1:
        payload = last_tool_payload(messages)
        found = (payload.get("data") or {}).get("candidates", [])
        nimbus = next(
            (
                c
                for c in found
                if c.get("model_number") == "NB15-G4" and c["attributes"].get("ram_gb") == 16
            ),
            None,
        )
        if nimbus is None:
            return "DONE: the search returned nothing usable"
        listing = nimbus["listings"][0]["listing_id"]
        return [
            ToolCall(id="c2", name="get_product_details", arguments={"listing_id": listing}),
            ToolCall(
                id="c3", name="search_products", arguments={"query": "x"}
            ),  # invalid: too short
        ]
    return "DONE: found candidates"
