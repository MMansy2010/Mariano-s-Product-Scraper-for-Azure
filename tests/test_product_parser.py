import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import scraper
from schema import FIELDS, blank_record
from playwright.async_api import TimeoutError as PlaywrightTimeoutError


class FakeResponse:
    def __init__(self, status=200):
        self.status = status


class FakePage:
    def __init__(self, status=200, timeout=False):
        self.status = status
        self.timeout = timeout
        self.closed = False

    async def goto(self, *args, **kwargs):
        if self.timeout:
            raise PlaywrightTimeoutError("simulated timeout")
        return FakeResponse(self.status)

    async def wait_for_timeout(self, *_args):
        pass

    async def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, page):
        self.page = page

    async def new_page(self):
        return self.page


def run(coro):
    return asyncio.run(coro)


def setup_parser(monkeypatch, page, product):
    async def fake_extract_jsonld(_page):
        return [product] if product is not None else []

    async def fake_visible_product_facts(_page):
        return {
            "ppm": "",
            "volume_description": "",
            "digital_description": "",
            "volume_price": "",
            "digital_price": "",
            "regular_price": "4.99",
            "sale_price": "",
        }

    monkeypatch.setattr(scraper, "extract_jsonld", fake_extract_jsonld)
    monkeypatch.setattr(scraper, "visible_product_facts", fake_visible_product_facts)
    return FakeBrowser(page)


def test_product_parser_success_returns_exact_schema(monkeypatch):
    page = FakePage()
    browser = setup_parser(
        monkeypatch,
        page,
        {
            "name": "Fresh Strawberries",
            "weight": "16 oz",
            "image": ["https://example.com/strawberries.jpg"],
            "url": "https://example.com/strawberries",
            "gtin12": "012345678905",
        },
    )

    result = run(scraper.parse_product_page(
        browser,
        "https://example.com/strawberries",
        {"category": "Produce", "sub_category": "Fruit"},
        {"zipcode": "60061", "store_name": "Marianos Vernon Hills",
         "store_location": "1720 N Milwaukee Ave"},
        "2026-10-31",
        10000,
    ))

    assert result["status"] == "success"
    assert result["reason"] == ""
    assert list(result["record"].keys()) == FIELDS
    assert result["record"]["regular_price"] == "4.99"
    assert result["record"]["sale_price"] == ""
    assert result["record"]["image_url"] == "https://example.com/strawberries.jpg"
    assert result["record"]["gupc"] == ""
    assert result["record"]["unit_price"] == ""
    assert result["record"]["ppm"] == ""
    assert page.closed


def test_product_parser_excludes_approximate_weight(monkeypatch):
    page = FakePage()
    browser = setup_parser(
        monkeypatch,
        page,
        {
            "name": "Fresh Ground Beef Approximately 1 lb",
            "weight": "",
        },
    )

    result = run(scraper.parse_product_page(
        browser,
        "https://example.com/beef",
        {"category": "Meat", "sub_category": ""},
        {"zipcode": "60061", "store_name": "Marianos Vernon Hills",
         "store_location": "1720 N Milwaukee Ave"},
        "2026-10-31",
        10000,
    ))

    assert result["status"] == "excluded_approx_weight"
    assert result["record"] is None
    assert result["reason"] == "approximate_weight"
    assert page.closed


def test_product_parser_http_error_is_parse_failure(monkeypatch):
    page = FakePage(status=404)
    browser = setup_parser(monkeypatch, page, None)

    result = run(scraper.parse_product_page(
        browser,
        "https://example.com/missing",
        {"category": "Produce", "sub_category": ""},
        {"zipcode": "60061", "store_name": "Marianos Vernon Hills",
         "store_location": "1720 N Milwaukee Ave"},
        "2026-10-31",
        10000,
    ))

    assert result["status"] == "parse_failed"
    assert result["record"] is None
    assert result["reason"] == "http_404"
    assert page.closed


def test_product_parser_timeout_is_parse_failure(monkeypatch):
    page = FakePage(timeout=True)
    browser = setup_parser(monkeypatch, page, None)

    result = run(scraper.parse_product_page(
        browser,
        "https://example.com/slow",
        {"category": "Produce", "sub_category": ""},
        {"zipcode": "60061", "store_name": "Marianos Vernon Hills",
         "store_location": "1720 N Milwaukee Ave"},
        "2026-10-31",
        10000,
    ))

    assert result["status"] == "parse_failed"
    assert result["record"] is None
    assert result["reason"] == "timeout"
    assert page.closed


def test_product_parser_simple_truth_organic_milk_representative(monkeypatch):
    """Representative source response test: Simple Truth Organic Milk at Vernon Hills store."""
    page = FakePage()
    raw_jsonld = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": "Simple Truth Organic Whole Milk",
        "image": ["https://www.marianos.com/product/images/xlarge/front/0001111085303"],
        "description": "Simple Truth Organic Whole Milk 1 Gallon",
        "sku": "0001111085303",
        "gtin12": "0001111085303",
        "brand": {"@type": "Brand", "name": "Simple Truth"},
        "offers": {
            "@type": "Offer",
            "price": "4.29",
            "priceCurrency": "USD",
            "availability": "https://schema.org/InStock",
        },
        "weight": "1 gal",
    }

    async def fake_extract_jsonld(_page):
        return [raw_jsonld]

    async def fake_visible_facts(_page):
        return {
            "ppm": "",
            "volume_description": "",
            "digital_description": "",
            "volume_price": "",
            "digital_price": "",
            "regular_price": "4.29",
            "sale_price": "",
        }

    monkeypatch.setattr(scraper, "extract_jsonld", fake_extract_jsonld)
    monkeypatch.setattr(scraper, "visible_product_facts", fake_visible_facts)

    browser = FakeBrowser(page)
    loc = {
        "zipcode": "60061",
        "store_name": "Mariano's",
        "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061",
        "store_id": "53100502",
    }

    result = run(scraper.parse_product_page(
        browser,
        "https://www.marianos.com/p/simple-truth-organic-whole-milk/0001111085303",
        {"category": "Dairy", "sub_category": "Milk"},
        loc,
        "2026-10-31",
        10000,
    ))

    assert result["status"] == "success"
    rec = result["record"]
    assert list(rec.keys()) == FIELDS
    assert rec["store_name"] == "Mariano's"
    assert rec["store_location"] == "1720 N Milwaukee Ave, Vernon Hills, IL 60061"
    assert rec["zipcode"] == "60061"
    assert rec["store_brand"] == "yes"  # Simple Truth is private label
    assert rec["category"] == "Dairy"
    assert rec["sub_category"] == "Milk"
    assert rec["product_title"] == "Simple Truth Organic Whole Milk"
    assert rec["weight"] == "1 gal"
    assert rec["regular_price"] == "4.29"
    assert rec["sale_price"] == ""
    assert rec["upc"] == "0001111085303"
    assert rec["gupc"] == ""
    assert rec["unit_price"] == ""
    assert rec["ppm"] == ""
    assert rec["crawl_date"] == "2026-10-31"


def test_product_parser_honeycrisp_apples_representative(monkeypatch):
    """Representative source response test: Honeycrisp Apples national brand / produce."""
    page = FakePage()
    raw_jsonld = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": "Honeycrisp Apples Bag",
        "image": "https://www.marianos.com/product/images/xlarge/front/0000000004128",
        "gtin12": "0000000004128",
        "weight": "3 lb",
        "offers": {"price": "5.99"},
    }

    async def fake_extract_jsonld(_page):
        return [raw_jsonld]

    async def fake_visible_facts(_page):
        return {
            "ppm": "",
            "volume_description": "",
            "digital_description": "",
            "volume_price": "",
            "digital_price": "",
            "regular_price": "5.99",
            "sale_price": "",
        }

    monkeypatch.setattr(scraper, "extract_jsonld", fake_extract_jsonld)
    monkeypatch.setattr(scraper, "visible_product_facts", fake_visible_facts)

    browser = FakeBrowser(page)
    loc = {"zipcode": "60061", "store_name": "Mariano's", "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061"}

    result = run(scraper.parse_product_page(
        browser,
        "https://www.marianos.com/p/honeycrisp-apples/0000000004128",
        {"category": "Produce", "sub_category": "Apples"},
        loc,
        "2026-10-31",
        10000,
    ))

    assert result["status"] == "success"
    rec = result["record"]
    assert rec["store_brand"] == "no"
    assert rec["product_title"] == "Honeycrisp Apples Bag"
    assert rec["weight"] == "3 lb"
    assert rec["regular_price"] == "5.99"
    assert rec["upc"] == "0000000004128"


def test_product_parser_excludes_approximate_weight_in_weight_field(monkeypatch):
    page = FakePage()
    raw_jsonld = {
        "@type": "Product",
        "name": "Private Selection USDA Choice Angus Beef Ribeye Steak",
        "weight": "approximately 1.25 lb",
        "gtin12": "0020123400000",
    }

    async def fake_extract_jsonld(_page):
        return [raw_jsonld]

    async def fake_visible_facts(_page):
        return {"ppm": "", "volume_description": "", "digital_description": "", "volume_price": "", "digital_price": "", "regular_price": "14.99", "sale_price": ""}

    monkeypatch.setattr(scraper, "extract_jsonld", fake_extract_jsonld)
    monkeypatch.setattr(scraper, "visible_product_facts", fake_visible_facts)

    browser = FakeBrowser(page)
    loc = {"zipcode": "60061", "store_name": "Mariano's", "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061"}

    result = run(scraper.parse_product_page(
        browser,
        "https://www.marianos.com/p/ribeye/0020123400000",
        {"category": "Meat", "sub_category": "Beef"},
        loc,
        "2026-10-31",
        10000,
    ))

    assert result["status"] == "excluded_approx_weight"
    assert result["record"] is None


def test_product_parser_captures_both_regular_and_sale_and_deals(monkeypatch):
    page = FakePage()
    raw_jsonld = {
        "@type": "Product",
        "name": "Tide Liquid Laundry Detergent",
        "image": "https://www.marianos.com/p/images/tide.jpg",
        "gtin12": "0003700012345",
        "weight": "92 fl oz",
    }

    async def fake_extract_jsonld(_page):
        return [raw_jsonld]

    async def fake_visible_facts(_page):
        return {
            "ppm": "",
            "volume_description": "Buy 2 Save $5 on household items",
            "digital_description": "Save $2.00 with digital coupon",
            "volume_price": "5.00",
            "digital_price": "2.00",
            "regular_price": "12.99",
            "sale_price": "9.99",
        }

    monkeypatch.setattr(scraper, "extract_jsonld", fake_extract_jsonld)
    monkeypatch.setattr(scraper, "visible_product_facts", fake_visible_facts)

    browser = FakeBrowser(page)
    loc = {"zipcode": "60061", "store_name": "Mariano's", "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061"}

    result = run(scraper.parse_product_page(
        browser,
        "https://www.marianos.com/p/tide/0003700012345",
        {"category": "Household", "sub_category": "Laundry"},
        loc,
        "2026-10-31",
        10000,
    ))

    assert result["status"] == "success"
    rec = result["record"]
    assert rec["regular_price"] == "12.99"
    assert rec["sale_price"] == "9.99"
    assert rec["volume_deals_description"] == "Buy 2 Save $5 on household items"
    assert rec["volume_deals_price"] == "5.00"
    assert rec["digital_deals_description"] == "Save $2.00 with digital coupon"
    assert rec["digital_deals_price"] == "2.00"
