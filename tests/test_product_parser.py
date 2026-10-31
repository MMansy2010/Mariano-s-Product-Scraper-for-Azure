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
