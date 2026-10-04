"""Agent tools. `default_tools()` returns the read-only research tools the model can call;
`all_tools()` adds the write tools, which run only after the user confirms."""

from typing import Any

from shopping_agent.tools.base import Tool, ToolContext, ToolResult
from shopping_agent.tools.catalog_tools import (
    FindAlternativesTool,
    GetListingsTool,
    GetProductDetailsTool,
    ResolveProductLinkTool,
    SearchProductsTool,
)
from shopping_agent.tools.deal_tools import FindOffersTool, GetPriceHistoryTool, GetSaleEventsTool
from shopping_agent.tools.registry import ToolCallRecord, ToolRegistry, ToolSpec
from shopping_agent.tools.web_tools import FetchPageTool, ResearchReviewsTool, SearchWebTool
from shopping_agent.tools.write_tools import write_tools


def default_tools() -> list[Tool[Any, Any]]:
    return [
        ResolveProductLinkTool(),
        SearchProductsTool(),
        GetProductDetailsTool(),
        GetListingsTool(),
        FindOffersTool(),
        ResearchReviewsTool(),
        GetPriceHistoryTool(),
        GetSaleEventsTool(),
        FindAlternativesTool(),
        SearchWebTool(),
        FetchPageTool(),
    ]


def all_tools() -> list[Tool[Any, Any]]:
    return [*default_tools(), *write_tools()]


__all__ = [
    "Tool",
    "ToolCallRecord",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "all_tools",
    "default_tools",
]
