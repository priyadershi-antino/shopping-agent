import pytest

from shopping_agent.domain.product import Marketplace
from shopping_agent.services.url_parser import IdType, UrlParseError, parse_product_url


def test_amazon_dp_link_strips_tracking() -> None:
    parsed = parse_product_url(
        "https://www.amazon.in/HP-Victus-Gaming-Laptop/dp/B0CXYZ1234/ref=sr_1_1?crid=abc&tag=aff-21"
    )
    assert parsed.marketplace is Marketplace.AMAZON
    assert parsed.id_type is IdType.ASIN
    assert parsed.external_id == "B0CXYZ1234"
    assert parsed.canonical_url == "https://www.amazon.in/dp/B0CXYZ1234"
    assert parsed.title_hint == "hp victus gaming laptop"


@pytest.mark.parametrize(
    "url",
    [
        "https://amazon.in/gp/product/B0ABCDEF12?psc=1",
        "https://m.amazon.in/gp/aw/d/B0ABCDEF12",
        "amazon.in/dp/b0abcdef12",
    ],
)
def test_amazon_variants(url: str) -> None:
    parsed = parse_product_url(url)
    assert parsed.external_id == "B0ABCDEF12"
    assert parsed.canonical_url == "https://www.amazon.in/dp/B0ABCDEF12"


@pytest.mark.parametrize(
    ("url", "marketplace"),
    [
        ("https://amzn.in/d/abc123", Marketplace.AMAZON),
        ("https://dl.flipkart.com/s/xyz", Marketplace.FLIPKART),
        ("https://fkrt.it/abcd", Marketplace.FLIPKART),
    ],
)
def test_short_links_need_resolution(url: str, marketplace: Marketplace) -> None:
    parsed = parse_product_url(url)
    assert parsed.marketplace is marketplace
    assert parsed.id_type is IdType.SHORT_LINK
    assert parsed.external_id is None


def test_flipkart_pid_link() -> None:
    parsed = parse_product_url(
        "https://www.flipkart.com/hp-victus-15-fa1099tx-laptop/p/itm1234567890abc"
        "?pid=COMGXYZ123456789&lid=LSTCOM&affid=someone"
    )
    assert parsed.marketplace is Marketplace.FLIPKART
    assert parsed.id_type is IdType.FLIPKART_PID
    assert parsed.external_id == "COMGXYZ123456789"
    assert parsed.canonical_url == (
        "https://www.flipkart.com/hp-victus-15-fa1099tx-laptop/p/itm1234567890abc"
        "?pid=COMGXYZ123456789"
    )
    assert parsed.title_hint == "hp victus 15 fa1099tx laptop"


def test_flipkart_item_only_link() -> None:
    parsed = parse_product_url("https://www.flipkart.com/some-phone/p/itmabc123def")
    assert parsed.id_type is IdType.FLIPKART_ITEM
    assert parsed.external_id == "itmabc123def"


@pytest.mark.parametrize(
    ("url", "marketplace", "sku"),
    [
        ("https://www.croma.com/hp-victus-laptop/p/301234", Marketplace.CROMA, "301234"),
        (
            "https://www.tatacliq.com/hp-laptop/p-mp000000012345",
            Marketplace.TATA_CLIQ,
            "000000012345",
        ),
    ],
)
def test_retailer_links(url: str, marketplace: Marketplace, sku: str) -> None:
    parsed = parse_product_url(url)
    assert parsed.marketplace is marketplace
    assert parsed.external_id == sku


def test_manufacturer_link() -> None:
    parsed = parse_product_url("https://www.apple.com/in/shop/buy-iphone/iphone-16?utm=x")
    assert parsed.marketplace is Marketplace.MANUFACTURER
    assert parsed.title_hint == "iphone 16"
    assert "utm" not in parsed.canonical_url


def test_unknown_site_is_other() -> None:
    assert parse_product_url("https://example-shop.in/p/thing").marketplace is Marketplace.OTHER


@pytest.mark.parametrize(
    "url",
    [
        "ftp://amazon.in/dp/B0ABCDEF12",
        "http://192.168.1.1/dp/B0ABCDEF12",
        "https://user:pw@www.amazon.in/dp/B0ABCDEF12",
        "https://www.amazon.in/some-page-without-asin",
        "https://www.flipkart.com/no-product-here",
        "not a url",
    ],
)
def test_rejected_links(url: str) -> None:
    with pytest.raises(UrlParseError):
        parse_product_url(url)
