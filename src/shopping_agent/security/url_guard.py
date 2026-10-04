"""URL guard for outbound page fetches (SSRF protection).

A URL is fetched only if it is https, has no embedded credentials, uses the default port,
its host is on the allowlist, and every address it resolves to is a public IP. Redirects
are re-checked hop by hop by the fetcher.

Marketplace product pages (Amazon, Flipkart, …) are deliberately NOT on the allowlist:
product data comes from providers, not scraping.
"""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable
from urllib.parse import SplitResult, urlsplit

from shopping_agent.errors import UnsafeUrlError

MANUFACTURER_SITES: frozenset[str] = frozenset(
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
        "store.google.com",
    }
)
REVIEW_SITES: frozenset[str] = frozenset(
    {
        "gsmarena.com",
        "notebookcheck.net",
        "91mobiles.com",
        "smartprix.com",
        "digit.in",
        "gadgets360.com",
        "rtings.com",
        "theverge.com",
        "tomsguide.com",
        "techradar.com",
        "androidauthority.com",
        "anandtech.com",
        "pcmag.com",
    }
)
BANK_SITES: frozenset[str] = frozenset(
    {
        "hdfcbank.com",
        "sbicard.com",
        "icicibank.com",
        "axisbank.com",
        "kotak.com",
        "idfcfirstbank.com",
        "bobcard.co.in",
        "aubank.in",
    }
)
OFFICIAL_PLATFORM_SITES: frozenset[str] = frozenset({"stories.flipkart.com", "aboutamazon.in"})
NEWS_SITES: frozenset[str] = frozenset(
    {
        "economictimes.indiatimes.com",
        "livemint.com",
        "indianexpress.com",
        "timesofindia.indiatimes.com",
        "business-standard.com",
        "moneycontrol.com",
    }
)
DEFAULT_FETCH_ALLOWLIST: frozenset[str] = (
    MANUFACTURER_SITES | REVIEW_SITES | BANK_SITES | OFFICIAL_PLATFORM_SITES | NEWS_SITES
)

Resolver = Callable[[str], Awaitable[list[str]]]


async def system_resolver(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return list({str(info[4][0]) for info in infos})


def host_allowed(host: str, allowlist: Iterable[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in allowlist)


def is_public_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def check_url(url: str, allowlist: Iterable[str]) -> SplitResult:
    """Syntax and allowlist checks (no network)."""
    try:
        parts = urlsplit(url.strip())
    except ValueError as exc:
        raise UnsafeUrlError("malformed URL") from exc
    if parts.scheme.lower() != "https":
        raise UnsafeUrlError("only https URLs can be fetched")
    if parts.username or parts.password:
        raise UnsafeUrlError("URLs with embedded credentials are not allowed")
    host = (parts.hostname or "").lower()
    if not host:
        raise UnsafeUrlError("URL has no host")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise UnsafeUrlError("IP-address URLs are not allowed")
    try:
        port = parts.port
    except ValueError as exc:
        raise UnsafeUrlError("invalid port") from exc
    if port not in (None, 443):
        raise UnsafeUrlError("non-standard ports are not allowed")
    if not host_allowed(host, allowlist):
        raise UnsafeUrlError(f"{host} is not on the fetch allowlist")
    return parts


async def check_resolves_public(host: str, resolver: Resolver = system_resolver) -> None:
    try:
        addresses = await resolver(host)
    except OSError as exc:
        raise UnsafeUrlError(f"could not resolve {host}") from exc
    if not addresses:
        raise UnsafeUrlError(f"could not resolve {host}")
    bad = [a for a in addresses if not is_public_ip(a)]
    if bad:
        raise UnsafeUrlError(f"{host} resolves to a non-public address")
