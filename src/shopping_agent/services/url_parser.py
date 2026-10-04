"""Deterministic product-link parsing.

Extracts the marketplace and product identifier from a pasted URL and builds a canonical
URL without affiliate/tracking parameters. Nothing is fetched here — short links are
reported as such so a provider can resolve them.
"""

import re
from enum import StrEnum
from urllib.parse import parse_qs, unquote, urlsplit

from pydantic import BaseModel

from shopping_agent.domain.product import Marketplace


class UrlParseError(ValueError):
    pass


class IdType(StrEnum):
    ASIN = "asin"
    FLIPKART_PID = "flipkart_pid"
    FLIPKART_ITEM = "flipkart_item"
    RETAILER_SKU = "retailer_sku"
    SLUG = "slug"
    SHORT_LINK = "short_link"


class ParsedProductUrl(BaseModel):
    marketplace: Marketplace
    host: str
    external_id: str | None
    id_type: IdType
    title_hint: str | None  # human-readable words from the URL slug
    canonical_url: str


_AMAZON_HOST = re.compile(r"(^|\.)amazon\.(in|com|co\.uk|de|ae)$")
_AMAZON_SHORT = {"amzn.in", "amzn.to", "a.co", "amzn.eu"}
_ASIN_PATH = re.compile(
    r"/(?:dp|gp/product|gp/aw/d|exec/obidos/asin|o/asin)/([A-Z0-9]{10})(?:[/?]|$)", re.IGNORECASE
)
_FLIPKART_HOST = re.compile(r"(^|\.)flipkart\.com$")
_FLIPKART_SHORT = {"fkrt.it", "fkrt.cc", "fkrt.co"}
_FLIPKART_ITEM = re.compile(r"/p/(itm[a-z0-9]+)", re.IGNORECASE)
_FLIPKART_PID = re.compile(r"^[A-Z0-9]{16}$")

_RETAILERS: dict[str, Marketplace] = {
    "croma.com": Marketplace.CROMA,
    "reliancedigital.in": Marketplace.RELIANCE_DIGITAL,
    "vijaysales.com": Marketplace.VIJAY_SALES,
    "tatacliq.com": Marketplace.TATA_CLIQ,
}
_RETAILER_SKU = re.compile(r"/p(?:-mp)?[/-]?(\w*\d\w*)(?:[/?]|$)", re.IGNORECASE)

MANUFACTURER_DOMAINS = frozenset(
    {
        "apple.com",
        "samsung.com",
        "oneplus.in",
        "mi.com",
        "dell.com",
        "hp.com",
        "lenovo.com",
        "asus.com",
        "acer.com",
        "msi.com",
        "lg.com",
        "sony.co.in",
        "realme.com",
        "nothing.tech",
        "motorola.in",
        "vivo.com",
        "oppo.com",
        "google.com",
        "boat-lifestyle.com",
        "noise.com",
        "jbl.com",
        "bose.com",
    }
)

_STOP_WORDS = {"dp", "p", "gp", "product", "ref", "www", "buy", "online", "in", "html"}


def _registrable(host: str) -> str:
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in {"co", "com"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _slug_to_title(slug: str) -> str | None:
    words = [w for w in re.split(r"[-_+\s]+", unquote(slug).lower()) if w]
    words = [w for w in words if w not in _STOP_WORDS and not w.startswith("itm")]
    return " ".join(words) or None


def _validate(url: str) -> tuple[str, str, str, str]:
    url = url.strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url if re.match(r"^[\w.-]+\.[a-z]{2,}/", url, re.I) else url
    parts = urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"}:
        raise UrlParseError("only http(s) product links are supported")
    if parts.username or parts.password:
        raise UrlParseError("links with embedded credentials are not accepted")
    host = (parts.hostname or "").lower()
    if not host or "." not in host:
        raise UrlParseError("link has no valid host")
    if re.fullmatch(r"[\d.]+|\[?[0-9a-f:]+\]?", host):
        raise UrlParseError("IP-address links are not accepted")
    return host.removeprefix("www.").removeprefix("m."), parts.path or "/", parts.query, url


def parse_product_url(url: str) -> ParsedProductUrl:
    host, path, query, full = _validate(url)

    if host in _AMAZON_SHORT:
        return ParsedProductUrl(
            marketplace=Marketplace.AMAZON,
            host=host,
            external_id=None,
            id_type=IdType.SHORT_LINK,
            title_hint=None,
            canonical_url=full,
        )
    if _AMAZON_HOST.search(host):
        match = _ASIN_PATH.search(path)
        if not match:
            raise UrlParseError("no Amazon product ID (ASIN) found in the link")
        asin = match.group(1).upper()
        slug = path.split("/dp/")[0].strip("/") if "/dp/" in path else ""
        return ParsedProductUrl(
            marketplace=Marketplace.AMAZON,
            host=host,
            external_id=asin,
            id_type=IdType.ASIN,
            title_hint=_slug_to_title(slug) if slug else None,
            canonical_url=f"https://www.{host}/dp/{asin}",
        )

    if host in _FLIPKART_SHORT or (host == "dl.flipkart.com" and path.startswith("/s/")):
        return ParsedProductUrl(
            marketplace=Marketplace.FLIPKART,
            host=host,
            external_id=None,
            id_type=IdType.SHORT_LINK,
            title_hint=None,
            canonical_url=full,
        )
    if _FLIPKART_HOST.search(host):
        clean_path = path.removeprefix("/dl")
        pid_values = parse_qs(query).get("pid") or []
        pid: str | None = pid_values[0] if pid_values else None
        item = _FLIPKART_ITEM.search(clean_path)
        if pid and not _FLIPKART_PID.match(pid.upper()):
            pid = None
        if not pid and not item:
            raise UrlParseError("no Flipkart product ID found in the link")
        slug = clean_path.split("/p/")[0].strip("/")
        canonical = f"https://www.flipkart.com{clean_path.split('?')[0]}"
        if pid:
            canonical += f"?pid={pid.upper()}"
        return ParsedProductUrl(
            marketplace=Marketplace.FLIPKART,
            host=host,
            external_id=pid.upper() if pid else item.group(1).lower() if item else None,
            id_type=IdType.FLIPKART_PID if pid else IdType.FLIPKART_ITEM,
            title_hint=_slug_to_title(slug),
            canonical_url=canonical,
        )

    domain = _registrable(host)
    segments = [s for s in path.split("/") if s]
    if domain in _RETAILERS:
        sku = _RETAILER_SKU.search(path)
        slug = segments[0] if segments else ""
        return ParsedProductUrl(
            marketplace=_RETAILERS[domain],
            host=host,
            external_id=sku.group(1) if sku else None,
            id_type=IdType.RETAILER_SKU if sku else IdType.SLUG,
            title_hint=_slug_to_title(slug),
            canonical_url=f"https://www.{host}{path}",
        )

    marketplace = Marketplace.MANUFACTURER if domain in MANUFACTURER_DOMAINS else Marketplace.OTHER
    slug = segments[-1] if segments else ""
    return ParsedProductUrl(
        marketplace=marketplace,
        host=host,
        external_id=slug or None,
        id_type=IdType.SLUG,
        title_hint=_slug_to_title(slug) if slug else None,
        canonical_url=f"https://{host}{path}",
    )
