"""Handling of untrusted external content (web pages, search snippets, reviews, offer text).

External text is data, never instructions. It is wrapped in explicit delimiters before it
reaches a model, delimiter look-alikes inside it are neutralised, and instruction-like
phrases are flagged so they can be reported and ignored.
"""

import re
from html import escape

_DELIMITER = re.compile(r"</?\s*untrusted_content", re.IGNORECASE)

_INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "override_instructions",
        re.compile(
            r"\b(ignore|disregard|forget|override)\b.{0,40}\b(previous|prior|above|all|system|"
            r"earlier)\b.{0,20}\b(instructions?|prompts?|rules?|messages?)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "role_claim",
        re.compile(
            r"(^|\n)\s*(system|assistant|developer)\s*:|\byou are now\b|\bnew instructions?\b",
            re.IGNORECASE,
        ),
    ),
    (
        "tool_or_secret_request",
        re.compile(
            r"\b(call|invoke)\b.{0,30}\b(tool|function)\b|\btool_call\b|"
            r"\b(enter|share|provide|send|give|type)\b.{0,30}"
            r"\b(api[_ ]?key|password|otp|cvv|card number|pin)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "recommendation_manipulation",
        re.compile(
            r"\b(always|only)\s+recommend\b|\brank (this|us)\b.{0,20}\b(first|top|highest)\b|"
            r"\bmust (recommend|buy)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "markup_tags",
        re.compile(r"<\s*/?\s*(system|instructions?|prompt|tool_call)\b", re.IGNORECASE),
    ),
]


def detect_injection(text: str) -> list[str]:
    """Names of instruction-like patterns found in the text (empty if none)."""
    return [name for name, pattern in _INJECTION_PATTERNS if pattern.search(text)]


def neutralise(text: str) -> str:
    return _DELIMITER.sub(lambda m: m.group(0).replace("<", "&lt;"), text)


def wrap_untrusted(text: str, *, source: str, url: str | None = None) -> str:
    attrs = f'source="{escape(source, quote=True)}"'
    if url:
        attrs += f' url="{escape(url, quote=True)}"'
    return f"<untrusted_content {attrs}>\n{neutralise(text)}\n</untrusted_content>"


def truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    cut = text[:max_chars]
    space = cut.rfind(" ")
    if space > max_chars * 0.8:
        cut = cut[:space]
    return cut + " …", True
