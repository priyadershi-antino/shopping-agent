"""Product normalization: brands, model numbers, attributes from titles, and listing grouping.

Grouping is conservative. Listings merge with high confidence only when brand and model
number match and no known attribute conflicts. Title-similar listings without a model
number are reported as *possible* matches, never silently merged.
"""

import re
from collections.abc import Iterable

from pydantic import BaseModel, Field

from shopping_agent.domain.common import Confidence
from shopping_agent.domain.product import Condition, Connectivity, Listing, VariantAttributes

_BRAND_ALIASES = {
    "hewlett packard": "hp",
    "hewlett-packard": "hp",
    "xiaomi": "xiaomi",
    "mi": "xiaomi",
    "samsung electronics": "samsung",
    "apple inc": "apple",
    "apple inc.": "apple",
    "lenovo group": "lenovo",
    "asustek": "asus",
    "one plus": "oneplus",
    "boat": "boat",
}

CONNECTIVITY_CATEGORIES = frozenset({"tablet", "smartwatch"})

KNOWN_BRANDS = (
    "apple", "samsung", "oneplus", "xiaomi", "redmi", "poco", "realme", "vivo", "oppo",
    "iqoo", "motorola", "nothing", "google", "nokia", "infinix", "tecno", "lava", "hp",
    "dell", "lenovo", "asus", "acer", "msi", "gigabyte", "microsoft", "lg", "sony",
    "boat", "noise", "jbl", "bose", "sennheiser", "skullcandy", "marshall", "fire-boltt",
    "amazfit", "garmin", "canon", "nikon", "fujifilm", "tcl", "hisense", "benq", "viewsonic",
    "zebronics", "honor", "huawei",
)  # fmt: skip
_BRAND_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(b) for b in KNOWN_BRANDS) + r"|hewlett[\s-]packard)\b", re.I
)


def detect_brand(title: str) -> str | None:
    """Known brand mentioned in a title (first occurrence), normalized."""
    m = _BRAND_PATTERN.search(title)
    return normalize_brand(m.group(1)) if m else None


def normalize_brand(brand: str | None) -> str | None:
    if not brand:
        return None
    key = re.sub(r"\s+", " ", brand.strip().lower())
    return _BRAND_ALIASES.get(key, key)


def normalize_model_number(model: str | None) -> str | None:
    if not model:
        return None
    cleaned = re.sub(r"[\s\-_/.]+", "", model.upper())
    return cleaned or None


def normalize_seller(seller: str | None) -> str | None:
    if not seller:
        return None
    cleaned = re.sub(r"[^a-z0-9]+", " ", seller.lower())
    cleaned = re.sub(r"\b(pvt|private|ltd|limited|llp|inc|retail|india)\b", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip() or None


def parse_capacity_gb(text: str) -> int | None:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(GB|TB)\s*", text, re.IGNORECASE)
    if not m:
        return None
    value = float(m.group(1))
    return int(value * 1024) if m.group(2).upper() == "TB" else int(value)


def _gb(value: str, unit: str) -> int:
    return int(float(value) * 1024) if unit.upper() == "TB" else int(float(value))


_RAM_PATTERNS = [
    re.compile(r"(\d{1,2})\s*GB\s*(?:LP)?(?:DDR\d\w*\s*)?(?:Unified\s*)?(?:RAM|Memory)\b", re.I),
    re.compile(r"\bRAM\s*[:\-]?\s*(\d{1,2})\s*GB", re.I),
]
_STORAGE_PATTERNS = [
    re.compile(
        r"(\d{1,4}(?:\.\d)?)\s*(GB|TB)\s*(?:NVMe\s*|PCIe\s*|M\.2\s*)*(?:SSD|HDD|ROM|Storage|eMMC|UFS)\b",
        re.I,
    ),
    re.compile(r"\b(?:SSD|Storage|ROM)\s*[:\-]?\s*(\d{1,4}(?:\.\d)?)\s*(GB|TB)", re.I),
]
# Phones: "(8GB RAM, 256GB Storage)" handled above; "(8+256)", "8GB+256GB", "8 GB/256 GB".
_PAIR = re.compile(r"\b(\d{1,2})\s*(?:GB)?\s*[+/]\s*(\d{2,4})\s*(GB|TB)?\b", re.I)
_CAPACITY = re.compile(r"\b(\d{1,4}(?:\.\d)?)\s*(GB|TB)\b", re.I)
_YEAR = re.compile(r"\b(20[1-3]\d)\b")
_INTERNATIONAL = re.compile(
    r"international version|imported|global version|us version|uk version|no india warranty",
    re.I,
)
_CELLULAR = re.compile(r"wi-?fi\s*\+\s*cellular|\bcellular\b|\blte\b|\b5g\b|\b4g\b", re.I)
_WIFI_ONLY = re.compile(r"\bwi-?fi\b(?!\s*\+)", re.I)

_CONDITIONS: list[tuple[re.Pattern[str], Condition]] = [
    (re.compile(r"\brenewed\b", re.I), Condition.RENEWED),
    (re.compile(r"\brefurbished\b", re.I), Condition.REFURBISHED),
    (re.compile(r"\bopen[\s-]?box\b", re.I), Condition.OPEN_BOX),
    (re.compile(r"\b(pre-?owned|used|second[\s-]?hand)\b", re.I), Condition.USED),
]


_RAM_KEY = re.compile(r"^(ram|memory|ram size|installed ram|ram capacity|system memory)$", re.I)
_STORAGE_KEY = re.compile(
    r"^(storage|internal storage|rom|ssd|ssd capacity|hard disk size|storage capacity|"
    r"hard drive size|memory storage capacity)$",
    re.I,
)


def _snake(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", key.strip().lower()).strip("_")


def normalize_specs(
    specs: dict[str, str | int | float | bool],
) -> dict[str, str | int | float | bool]:
    """Canonical spec keys; RAM/storage become integer GB under `ram_gb` / `storage_gb`."""
    out: dict[str, str | int | float | bool] = {}
    for key, value in specs.items():
        k = key.strip()
        if _RAM_KEY.match(k) or _STORAGE_KEY.match(k):
            target = "ram_gb" if _RAM_KEY.match(k) else "storage_gb"
            if isinstance(value, int | float) and not isinstance(value, bool):
                out[target] = int(value)
            elif isinstance(value, str) and (m := _CAPACITY.search(value)):
                out[target] = _gb(m.group(1), m.group(2))
            else:
                out[_snake(k)] = value
            continue
        out[_snake(k)] = value
    return out


def detect_condition(title: str) -> Condition:
    for pattern, condition in _CONDITIONS:
        if pattern.search(title):
            return condition
    return Condition.UNKNOWN


def extract_attributes(title: str, category: str | None = None) -> VariantAttributes:
    """Best-effort attribute extraction from a listing title. Unknown stays None."""
    attrs = VariantAttributes()
    for pattern in _RAM_PATTERNS:
        if m := pattern.search(title):
            attrs.ram_gb = int(m.group(1))
            break
    for pattern in _STORAGE_PATTERNS:
        if m := pattern.search(title):
            attrs.storage_gb = _gb(m.group(1), m.group(2))
            break
    if (attrs.ram_gb is None or attrs.storage_gb is None) and (m := _PAIR.search(title)):
        ram, storage = int(m.group(1)), _gb(m.group(2), m.group(3) or "GB")
        if ram <= 32 and storage >= 32:
            attrs.ram_gb = attrs.ram_gb or ram
            attrs.storage_gb = attrs.storage_gb or storage
    if attrs.storage_gb is None:
        # Phones/tablets often state storage bare: "iPad Air 128GB". Take the largest
        # capacity that isn't the RAM figure.
        sizes = [_gb(v, u) for v, u in _CAPACITY.findall(title)]
        sizes = [s for s in sizes if s >= 32 and s != attrs.ram_gb]
        if sizes:
            attrs.storage_gb = max(sizes)
    if _INTERNATIONAL.search(title):
        attrs.region = "international"
    if m := _YEAR.search(title):
        attrs.model_year = int(m.group(1))
    if category in CONNECTIVITY_CATEGORIES:
        if _CELLULAR.search(title):
            attrs.connectivity = Connectivity.CELLULAR
        elif _WIFI_ONLY.search(title):
            attrs.connectivity = Connectivity.WIFI
    return attrs


def identity_key(
    brand: str | None, model_number: str | None, attrs: VariantAttributes
) -> str | None:
    """Stable variant key, or None when brand/model number are unknown."""
    b, m = normalize_brand(brand), normalize_model_number(model_number)
    if not b or not m:
        return None
    parts = [b, m]
    if attrs.ram_gb is not None:
        parts.append(f"ram{attrs.ram_gb}")
    if attrs.storage_gb is not None:
        parts.append(f"sto{attrs.storage_gb}")
    if attrs.connectivity is not None:
        parts.append(attrs.connectivity.value)
    if attrs.region:
        parts.append(attrs.region.lower())
    return "|".join(parts)


_COMPARABLE = ("ram_gb", "storage_gb", "connectivity", "region", "model_year", "generation")


def attribute_conflicts(a: VariantAttributes, b: VariantAttributes) -> list[str]:
    """Attributes known on both sides with different values."""
    out = []
    for name in _COMPARABLE:
        va, vb = getattr(a, name), getattr(b, name)
        if va is not None and vb is not None and va != vb:
            out.append(name)
    return out


def _tokens(title: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", title.lower()) if len(t) > 1}


def title_similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


class ListingGroup(BaseModel):
    """Listings believed to be the same variant."""

    key: str
    listing_ids: list[str]
    confidence: Confidence
    notes: list[str] = Field(default_factory=list)
    possible_matches: list[str] = Field(default_factory=list)  # other group keys


def group_listings(
    listings: Iterable[Listing], *, fuzzy_threshold: float = 0.6
) -> list[ListingGroup]:
    groups: list[tuple[ListingGroup, list[Listing]]] = []
    unkeyed: list[Listing] = []

    for listing in listings:
        brand, model = normalize_brand(listing.brand), normalize_model_number(listing.model_number)
        if not brand or not model:
            unkeyed.append(listing)
            continue
        placed = False
        for group, members in groups:
            head = members[0]
            if (normalize_brand(head.brand), normalize_model_number(head.model_number)) != (
                brand,
                model,
            ):
                continue
            if any(attribute_conflicts(m.attributes, listing.attributes) for m in members):
                continue
            members.append(listing)
            group.listing_ids.append(listing.id)
            placed = True
            break
        if not placed:
            key = identity_key(listing.brand, listing.model_number, listing.attributes) or model
            if any(g.key == key for g, _ in groups):
                key = f"{key}#{len(groups)}"
            groups.append(
                (
                    ListingGroup(key=key, listing_ids=[listing.id], confidence=Confidence.HIGH),
                    [listing],
                )
            )

    for group, members in groups:
        for name in ("ram_gb", "storage_gb", "connectivity"):
            known = [getattr(m.attributes, name) is not None for m in members]
            if any(known) and not all(known):
                group.confidence = Confidence.MEDIUM
                group.notes.append(f"{name} not stated on every listing")

    for listing in unkeyed:
        group = ListingGroup(
            key=f"unkeyed:{listing.id}",
            listing_ids=[listing.id],
            confidence=Confidence.LOW,
            notes=["no model number; identity not established"],
        )
        for other, members in groups:
            same_brand = normalize_brand(members[0].brand) == normalize_brand(listing.brand)
            if (
                same_brand
                and title_similarity(members[0].title_raw, listing.title_raw) >= fuzzy_threshold
                and not attribute_conflicts(members[0].attributes, listing.attributes)
            ):
                group.possible_matches.append(other.key)
        groups.append((group, [listing]))

    return [g for g, _ in groups]
