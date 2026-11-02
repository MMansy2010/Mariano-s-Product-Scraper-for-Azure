import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import scraper
from schema import validate_records


class MockResponse:
    def __init__(self, status=200):
        self.status = status


class MockLocator:
    def __init__(self, count_val=0, inner_text_val="", attr_val=None, evaluate_all_val=None):
        self._count = count_val
        self._inner_text = inner_text_val
        self._attr = attr_val
        self._evaluate_all = evaluate_all_val or []

    @property
    def first(self):
        return self

    async def count(self):
        return self._count

    async def inner_text(self, timeout=None):
        return self._inner_text

    async def get_attribute(self, attr):
        return self._attr

    async def evaluate_all(self, expr):
        return self._evaluate_all


class MockPage:
    def __init__(self, status=200, title="Mariano's", body="", header="Mariano's | 1720 N Milwaukee Ave, Vernon Hills, IL 60061",
                 product_links=None, subcategories=None, next_page_url=None):
        self.status = status
        self._title = title
        self._body = body
        self._header = header
        self._product_links = product_links or []
        self._subcategories = subcategories or []
        self._next_page_url = next_page_url
        self.url = "https://www.marianos.com/d/produce"
        self.closed = False

    async def goto(self, url, wait_until=None, timeout=None):
        self.url = url
        return MockResponse(self.status)

    async def wait_for_timeout(self, ms):
        pass

    async def title(self):
        return self._title

    def locator(self, selector):
        if "body" in selector:
            return MockLocator(1, self._body)
        if any(h in selector for h in ("header", "banner", "store")):
            return MockLocator(1, self._header)
        if 'a[href*="/pl/"]' in selector:
            return MockLocator(len(self._subcategories), evaluate_all_val=self._subcategories)
        if any(p in selector for p in ('a[href*="/p/"]', 'a[href*="/product/"]')):
            return MockLocator(len(self._product_links), evaluate_all_val=self._product_links)
        if "Next" in selector or "rel=\"next\"" in selector:
            if self._next_page_url:
                return MockLocator(1, "", attr_val=self._next_page_url)
            return MockLocator(0)
        return MockLocator(0)

    async def evaluate(self, expr):
        return None

    async def close(self):
        self.closed = True

    def is_closed(self):
        return self.closed


class MockBrowser:
    def __init__(self, page_factory):
        self.page_factory = page_factory

    async def new_page(self):
        return self.page_factory()


def run_async(coro):
    return asyncio.run(coro)


@pytest.fixture
def location():
    return {
        "id": "vernon-hills",
        "zipcode": "60061",
        "store_name": "Mariano's",
        "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061",
        "store_id": "53100502",
    }


def test_unverified_store_context_marks_category_status(location):
    # Header does not contain Vernon Hills address
    page = MockPage(header="Mariano's | 333 E Benton Pl, Chicago, IL 60601")
    browser = MockBrowser(lambda: page)

    cfg = {"max_retries": 1, "request_timeout_ms": 5000, "navigation_delay_seconds": 0.01}
    cat = {"category": "Produce", "url": "https://www.marianos.com/d/produce"}

    rows, stats = run_async(scraper.crawl_category(browser, cat, location, cfg, "2026-10-31"))

    assert stats["status"] == "unverified_store_context"
    assert stats["outcome"] == scraper.OUTCOME_UNVERIFIED_STORE
    assert stats["error"] == "selected_store_not_verified"
    assert rows == []


def test_blocked_or_challenged_marks_category_status(location):
    page = MockPage(body="Access Denied: Please verify you are human to continue.")
    browser = MockBrowser(lambda: page)

    cfg = {"max_retries": 1, "request_timeout_ms": 5000, "navigation_delay_seconds": 0.01}
    cat = {"category": "Produce", "url": "https://www.marianos.com/d/produce"}

    rows, stats = run_async(scraper.crawl_category(browser, cat, location, cfg, "2026-10-31"))

    assert stats["status"] == "blocked_or_challenged"
    assert stats["outcome"] == scraper.OUTCOME_BLOCKED_OR_CHALLENGED
    assert stats["error"] == "possible_block_or_challenge"
    assert rows == []


def test_main_category_repeated_page_detected(location, monkeypatch):
    # Next page points to itself
    page = MockPage(
        next_page_url="https://www.marianos.com/d/produce",
        product_links=["https://www.marianos.com/p/apple/1"]
    )
    browser = MockBrowser(lambda: page)

    async def fake_parse_product(*args, **kwargs):
        rec = scraper.blank_record()
        rec.update({
            "zipcode": location["zipcode"],
            "store_name": location["store_name"],
            "store_location": location["store_location"],
            "category": "Produce",
            "product_title": "Apple",
            "url": "https://www.marianos.com/p/apple/1",
            "crawl_date": "2026-10-31",
            "upc": "111",
        })
        return {"status": "success", "record": rec, "reason": "", "title": "Apple"}

    monkeypatch.setattr(scraper, "parse_product_page", fake_parse_product)

    cfg = {"max_retries": 1, "request_timeout_ms": 5000, "navigation_delay_seconds": 0.01}
    cat = {"category": "Produce", "url": "https://www.marianos.com/d/produce"}

    rows, stats = run_async(scraper.crawl_category(browser, cat, location, cfg, "2026-10-31"))

    assert stats["status"] == "repeated_pagination_page"
    assert stats["outcome"] == scraper.OUTCOME_REPEATED_PAGE
    assert stats["pagination_stop_reason"] == "repeated_page_detected"
    assert not stats["pagination_complete"]


def test_product_parse_failure_propagates_to_category_status(location, monkeypatch):
    page = MockPage(product_links=["https://www.marianos.com/p/broken/1"])
    browser = MockBrowser(lambda: page)

    async def failing_parse_product(*args, **kwargs):
        return {"status": "parse_failed", "record": None, "reason": "missing_product_title", "title": ""}

    monkeypatch.setattr(scraper, "parse_product_page", failing_parse_product)

    cfg = {"max_retries": 1, "request_timeout_ms": 5000, "navigation_delay_seconds": 0.01}
    cat = {"category": "Produce", "url": "https://www.marianos.com/d/produce"}

    rows, stats = run_async(scraper.crawl_category(browser, cat, location, cfg, "2026-10-31"))

    assert stats["status"] == "product_parse_failure"
    assert stats["outcome"] == scraper.OUTCOME_PRODUCT_PARSE_FAILURE
    assert len(stats["product_parse_failures"]) == 1
    assert rows == []


def test_subcategory_http_error_propagates_to_category_status(location):
    main_page = MockPage(
        subcategories=[{"url": "https://www.marianos.com/pl/fresh-fruit/1", "name": "Fresh Fruit"}]
    )
    sub_page = MockPage(status=500)

    pages = [main_page, sub_page]
    browser = MockBrowser(lambda: pages.pop(0) if pages else MockPage())

    cfg = {"max_retries": 1, "request_timeout_ms": 5000, "navigation_delay_seconds": 0.01}
    cat = {"category": "Produce", "url": "https://www.marianos.com/d/produce"}

    rows, stats = run_async(scraper.crawl_category(browser, cat, location, cfg, "2026-10-31"))

    assert stats["status"] == "incomplete_failed_requests"
    assert stats["outcome"] == scraper.OUTCOME_FAILED_REQUESTS
    assert len(stats["subcategory_failures"]) == 1
    assert stats["subcategory_failures"][0]["error"] == "http_500"


def test_fully_exhausted_category_marks_ok_and_successful(location, monkeypatch):
    page = MockPage(product_links=["https://www.marianos.com/p/apple/1"])
    browser = MockBrowser(lambda: page)

    async def fake_parse_product(*args, **kwargs):
        rec = scraper.blank_record()
        rec.update({
            "zipcode": location["zipcode"],
            "store_name": location["store_name"],
            "store_location": location["store_location"],
            "category": "Produce",
            "product_title": "Crisp Apple",
            "url": "https://www.marianos.com/p/apple/1",
            "crawl_date": "2026-10-31",
            "upc": "012345678901",
            "regular_price": "2.99",
        })
        return {"status": "success", "record": rec, "reason": "", "title": "Crisp Apple"}

    monkeypatch.setattr(scraper, "parse_product_page", fake_parse_product)

    cfg = {"max_retries": 1, "request_timeout_ms": 5000, "navigation_delay_seconds": 0.01}
    cat = {"category": "Produce", "url": "https://www.marianos.com/d/produce"}

    rows, stats = run_async(scraper.crawl_category(browser, cat, location, cfg, "2026-10-31"))

    assert stats["status"] == "ok"
    assert stats["outcome"] == scraper.OUTCOME_FULLY_EXHAUSTED
    assert stats["error"] == ""
    assert stats["pagination_complete"] is True
    assert len(rows) == 1


def test_validation_fails_closed_when_any_category_is_not_ok():
    record = scraper.blank_record()
    record.update({
        "zipcode": "60061",
        "store_name": "Mariano's",
        "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061",
        "category": "Produce",
        "product_title": "Fresh Apples",
        "url": "https://example.test/apples",
        "upc": "0000000000001",
        "crawl_date": "2026-10-31",
    })
    cfg = {"min_records_for_publish": 1, "min_categories_with_products": 1}

    # One ok, one failed
    category_stats = [
        {"category": "Produce", "status": "ok", "store_evidence": {"selected_store_verified": True}},
        {"category": "Meat", "status": "product_parse_failure", "outcome": "product_parse_failure", "store_evidence": {"selected_store_verified": True}},
    ]

    result = validate_records([record], category_stats, cfg)
    assert result["validation_passed"] is False
    assert "Meat" in result["categories_not_ok"]
