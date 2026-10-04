import re

from shopping_agent.domain.product import Availability, Marketplace

_SELLER_TO_MARKETPLACE: list[tuple[re.Pattern[str], Marketplace]] = [
    (re.compile(r"\bamazon", re.I), Marketplace.AMAZON),
    (re.compile(r"\bflipkart", re.I), Marketplace.FLIPKART),
    (re.compile(r"\bcroma", re.I), Marketplace.CROMA),
    (re.compile(r"reliance\s*digital", re.I), Marketplace.RELIANCE_DIGITAL),
    (re.compile(r"vijay\s*sales", re.I), Marketplace.VIJAY_SALES),
    (re.compile(r"tata\s*cliq", re.I), Marketplace.TATA_CLIQ),
]


def marketplace_from_store(name: str | None) -> Marketplace:
    if not name:
        return Marketplace.OTHER
    for pattern, marketplace in _SELLER_TO_MARKETPLACE:
        if pattern.search(name):
            return marketplace
    return Marketplace.OTHER


def availability_from_text(texts: list[str] | str | None) -> Availability:
    if not texts:
        return Availability.UNKNOWN
    joined = " ".join(texts) if isinstance(texts, list) else texts
    low = joined.lower()
    if "out of stock" in low or "currently unavailable" in low or "sold out" in low:
        return Availability.OUT_OF_STOCK
    if re.search(r"only \d+ left|few left|limited stock", low):
        return Availability.LIMITED
    if "pre-order" in low or "preorder" in low:
        return Availability.PREORDER
    if "in stock" in low:
        return Availability.IN_STOCK
    return Availability.UNKNOWN


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]
