import pytest

from shopping_agent.domain.common import Confidence
from shopping_agent.domain.product import Condition, Connectivity, VariantAttributes
from shopping_agent.services.normalization import (
    detect_condition,
    extract_attributes,
    group_listings,
    identity_key,
    normalize_brand,
    normalize_model_number,
    normalize_seller,
    parse_capacity_gb,
)
from tests.factories import listing


def test_brand_aliases() -> None:
    assert normalize_brand("Hewlett Packard") == "hp"
    assert normalize_brand("  ASUS ") == "asus"
    assert normalize_brand(None) is None


def test_model_number_normalisation() -> None:
    assert normalize_model_number("15-fa1099TX") == normalize_model_number("15 FA1099TX")
    assert normalize_model_number("MQ1G3HN/A") == "MQ1G3HNA"


def test_seller_normalisation() -> None:
    assert normalize_seller("Appario Retail Pvt. Ltd.") == normalize_seller("appario")


@pytest.mark.parametrize(
    ("text", "gb"), [("16GB", 16), ("1 TB", 1024), ("512 gb", 512), ("x", None)]
)
def test_capacity(text: str, gb: int | None) -> None:
    assert parse_capacity_gb(text) == gb


@pytest.mark.parametrize(
    ("title", "ram", "storage"),
    [
        ("HP Victus Gaming Laptop, 16GB DDR5 RAM, 512GB SSD, RTX 4050 (2024)", 16, 512),
        ("Samsung Galaxy S24 5G (8GB RAM, 256GB Storage)", 8, 256),
        ("Redmi Note 13 Pro (8+256)", 8, 256),
        ("Lenovo LOQ 24GB RAM 1TB SSD", 24, 1024),
        ("Apple iPad Air 11 inch Wi-Fi 128GB", None, 128),
        ("ASUS Vivobook 15, 8 GB/512 GB", 8, 512),
    ],
)
def test_extract_ram_and_storage(title: str, ram: int | None, storage: int | None) -> None:
    attrs = extract_attributes(title)
    assert attrs.ram_gb == ram
    assert attrs.storage_gb == storage


def test_extract_year_and_region() -> None:
    attrs = extract_attributes("Dell XPS 13 (2024) International Version")
    assert attrs.model_year == 2024
    assert attrs.region == "international"


def test_connectivity_only_for_relevant_categories() -> None:
    assert extract_attributes("iPad Air Wi-Fi + Cellular 256GB", "tablet").connectivity is (
        Connectivity.CELLULAR
    )
    assert (
        extract_attributes("iPad 10th Gen Wi-Fi 64GB", "tablet").connectivity is Connectivity.WIFI
    )
    assert extract_attributes("Galaxy S24 5G", "smartphone").connectivity is None


@pytest.mark.parametrize(
    ("title", "condition"),
    [
        ("(Renewed) HP Laptop", Condition.RENEWED),
        ("Refurbished iPhone 13", Condition.REFURBISHED),
        ("Open Box Sony WH-1000XM5", Condition.OPEN_BOX),
        ("HP Victus 15", Condition.UNKNOWN),
    ],
)
def test_condition_never_assumed_new(title: str, condition: Condition) -> None:
    assert detect_condition(title) is condition


def test_identity_key() -> None:
    attrs = VariantAttributes(ram_gb=16, storage_gb=512)
    assert identity_key("HP", "15-fa1099TX", attrs) == "hp|15FA1099TX|ram16|sto512"
    assert identity_key("HP", None, attrs) is None


def test_grouping_merges_same_variant_and_separates_others() -> None:
    amazon = listing(id="a", title_raw="HP Victus 15 16GB 512GB SSD")
    flipkart = listing(
        id="f", title_raw="HP VICTUS 15-FA1099TX (16 GB/512 GB)", model_number="15 FA1099TX"
    )
    croma = listing(id="c", title_raw="HP Victus 15", attributes=VariantAttributes(storage_gb=512))
    smaller = listing(id="s", attributes=VariantAttributes(ram_gb=8, storage_gb=512))
    unkeyed = listing(id="u", model_number=None, title_raw="HP Victus 15 16GB 512GB SSD Gaming")

    groups = {
        tuple(g.listing_ids): g for g in group_listings([amazon, flipkart, croma, smaller, unkeyed])
    }

    main = groups[("a", "f", "c")]
    assert main.confidence is Confidence.MEDIUM  # croma doesn't state RAM
    assert any("ram_gb" in n for n in main.notes)
    assert ("s",) in groups
    loose = groups[("u",)]
    assert loose.confidence is Confidence.LOW
    assert main.key in loose.possible_matches


def test_grouping_high_confidence_when_all_attributes_stated() -> None:
    groups = group_listings([listing(id="a"), listing(id="b")])
    assert len(groups) == 1
    assert groups[0].confidence is Confidence.HIGH
