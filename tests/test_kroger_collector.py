"""Unit and regression tests for the technically verified Kroger API collector module."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import kroger_collector
from kroger_collector import (
    KrogerAPIError,
    KrogerClient,
    extract_valid_prices,
    transform_kroger_product,
    collect_kroger_catalog,
    get_next_kroger_start,
    KROGER_MIN_START,
    KROGER_MAX_START,
)
from schema import FIELDS, validate_records, blank_record, product_key


@pytest.fixture
def mock_location():
    return {
        "id": "vernon-hills",
        "zipcode": "60061",
        "store_name": "Mariano's",
        "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061",
        "store_id": "53100502",
    }


@pytest.fixture
def sample_kroger_product_raw():
    return {
        "productId": "0001111041700",
        "upc": "0001111041700",
        "brand": "Simple Truth Organic",
        "description": "Simple Truth Organic Whole Milk",
        "size": "1 gal",
        "categories": ["Dairy", "Milk & Cream"],
        "images": [
            {
                "perspective": "front",
                "sizes": [
                    {"size": "small", "url": "https://images.kroger.com/small.jpg"},
                    {"size": "xlarge", "url": "https://images.kroger.com/xlarge.jpg"},
                ],
            }
        ],
        "items": [
            {
                "itemId": "0001111041700",
                "price": {
                    "regular": 4.29,
                    "promo": 3.79,
                },
                "fulfillment": {
                    "instore": True,
                    "curbside": True,
                    "delivery": True,
                },
            }
        ],
    }


# ---------------------------------------------------------------------------
# 1. Price Validation Tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "price_obj,expected_reg,expected_sale",
    [
        # Standard regular and lower promo
        ({"regular": 4.99, "promo": 3.99}, "4.99", "3.99"),
        ({"regular": "4.99", "promo": "3.99"}, "4.99", "3.99"),
        # Promo missing, zero, or negative
        ({"regular": 4.99}, "4.99", ""),
        ({"regular": 4.99, "promo": 0}, "4.99", ""),
        ({"regular": 4.99, "promo": "0.00"}, "4.99", ""),
        ({"regular": 4.99, "promo": -1.50}, "4.99", ""),
        # Promo equal to or greater than regular
        ({"regular": 4.99, "promo": 4.99}, "4.99", ""),
        ({"regular": 4.99, "promo": 5.99}, "4.99", ""),
        # Regular price missing, zero, negative, or malformed
        ({}, "", ""),
        ({"regular": 0}, "", ""),
        ({"regular": -4.99}, "", ""),
        ({"regular": "N/A"}, "", ""),
        # Promo present but regular missing (must NEVER produce a sale price)
        ({"promo": 3.99}, "", ""),
        ({"regular": None, "promo": 3.99}, "", ""),
        ({"regular": 0, "promo": 3.99}, "", ""),
    ],
)
def test_extract_valid_prices_edge_cases(price_obj, expected_reg, expected_sale):
    reg, sale = extract_valid_prices(price_obj)
    assert reg == expected_reg
    assert sale == expected_sale


def test_transform_kroger_product_excludes_item_without_valid_price(sample_kroger_product_raw, mock_location):
    sample_kroger_product_raw["items"][0]["price"] = {"regular": "invalid"}
    rec = transform_kroger_product(
        raw_item=sample_kroger_product_raw,
        location=mock_location,
        category_name="Dairy",
    )
    assert rec is None


# ---------------------------------------------------------------------------
# 2. Strict Store Identity Verification Tests
# ---------------------------------------------------------------------------

def test_verify_location_accepts_exact_id_and_address(mock_location):
    client = KrogerClient(client_id="dummy", client_secret="dummy")
    client.access_token = "fake-token"
    client.token_expiry = 9999999999

    fake_locations = [
        {
            "locationId": "53100502",
            "name": "Mariano's",
            "chain": "MARIANO'S",
            "address": {
                "addressLine1": "1720 N Milwaukee Ave",
                "city": "Vernon Hills",
                "state": "IL",
                "zipCode": "60061",
            },
        }
    ]

    client.get_locations_near = lambda zipcode: fake_locations

    evidence = client.verify_location(
        target_store_id="53100502",
        expected_address="1720 N Milwaukee Ave",
        expected_city="Vernon Hills",
        expected_state="IL",
        expected_zip="60061",
    )
    assert evidence["verified"] is True
    assert evidence["target_location_id"] == "53100502"
    assert evidence["returned_location_id"] == "53100502"


def test_verify_location_rejects_different_store_id_even_with_matching_address(mock_location):
    client = KrogerClient(client_id="dummy", client_secret="dummy")
    client.access_token = "fake-token"
    client.token_expiry = 9999999999

    # Another store that happens to have a similar or matching street address
    fake_locations = [
        {
            "locationId": "99999999",  # WRONG STORE ID
            "name": "Mariano's Other",
            "address": {
                "addressLine1": "1720 N Milwaukee Ave",
                "city": "Vernon Hills",
                "state": "IL",
                "zipCode": "60061",
            },
        }
    ]
    client.get_locations_near = lambda zipcode: fake_locations

    with pytest.raises(KrogerAPIError, match="Retailer did not return exact locationId '53100502'"):
        client.verify_location(
            target_store_id="53100502",
            expected_address="1720 N Milwaukee Ave",
        )


@pytest.mark.parametrize(
    "addr_field,bad_value",
    [
        ("addressLine1", "999 S Wacker Dr"),
        ("city", "Chicago"),
        ("state", "WI"),
        ("zipCode", "60601"),
    ],
)
def test_verify_location_rejects_mismatched_address_fields(addr_field, bad_value):
    client = KrogerClient(client_id="dummy", client_secret="dummy")
    client.access_token = "fake-token"
    client.token_expiry = 9999999999

    base_addr = {
        "addressLine1": "1720 N Milwaukee Ave",
        "city": "Vernon Hills",
        "state": "IL",
        "zipCode": "60061",
    }
    base_addr[addr_field] = bad_value

    fake_locations = [
        {
            "locationId": "53100502",
            "name": "Mariano's",
            "address": base_addr,
        }
    ]
    client.get_locations_near = lambda zipcode: fake_locations

    with pytest.raises(KrogerAPIError, match="mismatch"):
        client.verify_location(
            target_store_id="53100502",
            expected_address="1720 N Milwaukee Ave",
            expected_city="Vernon Hills",
            expected_state="IL",
            expected_zip="60061",
        )


# ---------------------------------------------------------------------------
# 3. HTTP Resilience & Retry Policy Tests
# ---------------------------------------------------------------------------

class MockHTTPResponse:
    def __init__(self, status_code: int, json_data: Any = None, headers: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._json = json_data or {}
        self.headers = headers or {}
        self.text = text or json.dumps(self._json)
        self.ok = 200 <= status_code < 300
        self.reason = "OK" if self.ok else f"Status_{status_code}"

    def json(self):
        return self._json


def test_http_retry_policy_retries_429_with_retry_after():
    client = KrogerClient(client_id="dummy", client_secret="dummy", max_retries=2)
    sleep_calls = []
    client.sleep_fn = lambda s: sleep_calls.append(s)

    responses = [
        MockHTTPResponse(429, headers={"Retry-After": "0.25"}, text="Too Many Requests"),
        MockHTTPResponse(200, json_data={"data": [{"test": 1}]}),
    ]

    def fake_request(*args, **kwargs):
        return responses.pop(0)

    client.session.request = fake_request

    resp = client._request_with_retry("GET", "https://api.kroger.com/v1/test")
    assert resp.status_code == 200
    assert resp.json()["data"] == [{"test": 1}]
    assert sleep_calls == [0.25]


def test_http_retry_policy_retries_transient_5xx():
    client = KrogerClient(client_id="dummy", client_secret="dummy", max_retries=2)
    sleep_calls = []
    client.sleep_fn = lambda s: sleep_calls.append(s)

    responses = [
        MockHTTPResponse(503, text="Service Unavailable"),
        MockHTTPResponse(200, json_data={"status": "recovered"}),
    ]
    client.session.request = lambda *args, **kwargs: responses.pop(0)

    resp = client._request_with_retry("GET", "https://api.kroger.com/v1/test")
    assert resp.status_code == 200
    assert len(sleep_calls) == 1


def test_http_retry_policy_fails_immediately_on_401_without_retry():
    client = KrogerClient(client_id="dummy", client_secret="dummy", max_retries=3)
    call_count = 0

    def fake_request(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return MockHTTPResponse(401, text="Unauthorized: invalid client")

    client.session.request = fake_request

    with pytest.raises(KrogerAPIError, match="HTTP 401 client error"):
        client._request_with_retry("GET", "https://api.kroger.com/v1/test")

    # Must NOT retry permanent authentication failure
    assert call_count == 1


# ---------------------------------------------------------------------------
# 4. Taxonomy & Schema Tests
# ---------------------------------------------------------------------------

def test_source_backed_taxonomy_preferred_over_query_term(sample_kroger_product_raw, mock_location):
    sample_kroger_product_raw["categories"] = ["Dairy", "Specialty Cheeses"]
    rec = transform_kroger_product(
        raw_item=sample_kroger_product_raw,
        location=mock_location,
        category_name="Dairy",
        sub_category="Milk",  # search query was milk
    )
    assert rec is not None
    # Source-backed taxonomy used for sub_category
    assert rec["sub_category"] == "Specialty Cheeses"


def test_canonical_product_url_generated_from_upc(sample_kroger_product_raw, mock_location):
    rec = transform_kroger_product(
        raw_item=sample_kroger_product_raw,
        location=mock_location,
        category_name="Dairy",
    )
    assert rec is not None
    assert rec["url"] == "https://www.marianos.com/p/simple-truth-organic-whole-milk/0001111041700"


def test_product_url_blank_when_upc_missing(sample_kroger_product_raw, mock_location):
    sample_kroger_product_raw["upc"] = ""
    sample_kroger_product_raw["productId"] = ""
    rec = transform_kroger_product(
        raw_item=sample_kroger_product_raw,
        location=mock_location,
        category_name="Dairy",
    )
    assert rec is not None
    assert rec["url"] == ""


# ---------------------------------------------------------------------------
# 5. Catalog Integrity & Validation Threshold Tests
# ---------------------------------------------------------------------------

def test_validation_fails_on_partial_collection_and_prevents_catalog_creation(tmp_path, monkeypatch, mock_location, sample_kroger_product_raw):
    monkeypatch.setattr(kroger_collector, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(kroger_collector, "EVIDENCE_DIR", tmp_path / "data" / "evidence")

    client = KrogerClient(client_id="dummy", client_secret="dummy")
    client.access_token = "fake-token"
    client.token_expiry = 9999999999

    client.verify_location = lambda **kwargs: {
        "verified": True,
        "target_location_id": "53100502",
        "returned_location_id": "53100502",
        "name": "Mariano's",
        "address": {"addressLine1": "1720 N Milwaukee Ave", "city": "Vernon Hills", "state": "IL", "zipCode": "60061"},
    }

    # API returns only 3 products
    products_list = []
    for i in range(3):
        p = dict(sample_kroger_product_raw)
        p["productId"] = f"000111104170{i}"
        p["upc"] = f"000111104170{i}"
        products_list.append(p)

    client.search_products = lambda *args, **kwargs: (products_list, len(products_list))

    # Request target_count = 500, but only 3 returned
    records, stats, meta = collect_kroger_catalog(
        client=client,
        location=mock_location,
        categories_terms={"Dairy": ["milk"]},
        target_count=500,
        proof_only=False,
    )

    # Validate against target of 500
    val = validate_records(
        records=records,
        category_stats=stats,
        cfg={"min_records_for_publish": 500, "min_categories_with_products": 3},
    )

    # Must FAIL validation
    assert val["validation_passed"] is False
    assert val["total_records"] == 3
    assert val["publish_threshold_records"] == 500

    # Ensure catalog is NOT published
    latest_catalog = tmp_path / "data" / "marianos_latest.json"
    assert not latest_catalog.exists()


def test_proof_only_validates_successfully_at_5_products(tmp_path, monkeypatch, mock_location, sample_kroger_product_raw):
    monkeypatch.setattr(kroger_collector, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(kroger_collector, "EVIDENCE_DIR", tmp_path / "data" / "evidence")

    client = KrogerClient(client_id="dummy", client_secret="dummy")
    client.access_token = "fake-token"
    client.token_expiry = 9999999999

    client.verify_location = lambda **kwargs: {
        "verified": True,
        "target_location_id": "53100502",
        "returned_location_id": "53100502",
        "name": "Mariano's",
        "address": {"addressLine1": "1720 N Milwaukee Ave", "city": "Vernon Hills", "state": "IL", "zipCode": "60061"},
    }

    products_list = []
    for i in range(5):
        p = dict(sample_kroger_product_raw)
        p["productId"] = f"000111104170{i}"
        p["upc"] = f"000111104170{i}"
        products_list.append(p)

    client.search_products = lambda *args, **kwargs: (products_list, len(products_list))

    records, stats, meta = collect_kroger_catalog(
        client=client,
        location=mock_location,
        categories_terms={"Dairy": ["milk"]},
        target_count=5,
        proof_only=True,
    )

    val = validate_records(
        records=records,
        category_stats=stats,
        cfg={"min_records_for_publish": 5, "min_categories_with_products": 1},
    )

    assert val["validation_passed"] is True
    assert len(records) == 5


# ---------------------------------------------------------------------------
# 6. Quality & Source Data Integrity Regression Tests
# ---------------------------------------------------------------------------

def test_store_brand_values_conform_to_schema(sample_kroger_product_raw, mock_location):
    # Roundy's (private label)
    sample_kroger_product_raw["brand"] = "Roundy's"
    rec1 = transform_kroger_product(sample_kroger_product_raw, mock_location, "Dairy")
    assert rec1 is not None
    assert rec1["store_brand"] == "yes"

    # Simple Truth Organic (private label)
    sample_kroger_product_raw["brand"] = "Simple Truth Organic"
    rec2 = transform_kroger_product(sample_kroger_product_raw, mock_location, "Dairy")
    assert rec2 is not None
    assert rec2["store_brand"] == "yes"

    # Chobani (national/third-party brand)
    sample_kroger_product_raw["brand"] = "Chobani"
    sample_kroger_product_raw["description"] = "Chobani Plain Greek Yogurt"
    rec3 = transform_kroger_product(sample_kroger_product_raw, mock_location, "Dairy")
    assert rec3 is not None
    assert rec3["store_brand"] == "no"


def test_package_size_extracted_from_items_size(sample_kroger_product_raw, mock_location):
    # Root raw_item has no size, but items[0] has size: "1/2 gal"
    sample_kroger_product_raw.pop("size", None)
    sample_kroger_product_raw["items"][0]["size"] = "1/2 gal"
    rec = transform_kroger_product(sample_kroger_product_raw, mock_location, "Dairy")
    assert rec is not None
    assert rec["weight"] == "1/2 gal"


def test_category_and_subcategory_never_inferred_from_search_query(sample_kroger_product_raw, mock_location):
    # Case 1: Source has single category "Dairy" -> sub_category must be ""
    sample_kroger_product_raw["categories"] = ["Dairy"]
    rec1 = transform_kroger_product(sample_kroger_product_raw, mock_location, "Dairy", sub_category="Milk")
    assert rec1 is not None
    assert rec1["category"] == "Dairy"
    assert rec1["sub_category"] == ""

    # Case 2: Source has duplicate category ["Dairy", "Dairy"] -> sub_category must be ""
    sample_kroger_product_raw["categories"] = ["Dairy", "Dairy"]
    rec2 = transform_kroger_product(sample_kroger_product_raw, mock_location, "Dairy")
    assert rec2 is not None
    assert rec2["sub_category"] == ""

    # Case 3: Source has distinct subcategory ["Dairy", "Milk & Plant-Based Milk"]
    sample_kroger_product_raw["categories"] = ["Dairy", "Milk & Plant-Based Milk"]
    rec3 = transform_kroger_product(sample_kroger_product_raw, mock_location, "Dairy")
    assert rec3 is not None
    assert rec3["category"] == "Dairy"
    assert rec3["sub_category"] == "Milk & Plant-Based Milk"


def test_product_page_uri_from_api_used_and_query_stripped(sample_kroger_product_raw, mock_location):
    sample_kroger_product_raw["productPageURI"] = (
        "/p/simple-truth-organic-2-reduced-fat-milk-half-gallon/0001111042852?cid=dis.api.tpi_products-api"
    )
    rec = transform_kroger_product(sample_kroger_product_raw, mock_location, "Dairy")
    assert rec is not None
    assert rec["url"] == "https://www.marianos.com/p/simple-truth-organic-2-reduced-fat-milk-half-gallon/0001111042852"


def test_roundys_package_variants_have_distinct_keys(sample_kroger_product_raw, mock_location):
    import copy
    p1 = copy.deepcopy(sample_kroger_product_raw)
    p1["upc"] = "0001115015957"
    p1["description"] = "Roundy's Select 2% Reduced Fat Milk"
    p1["items"][0]["size"] = "1/2 gallon"
    p1["items"][0]["price"]["regular"] = 2.39

    p2 = copy.deepcopy(sample_kroger_product_raw)
    p2["upc"] = "0001115015950"
    p2["description"] = "Roundy's Select 2% Reduced Fat Milk"
    p2["items"][0]["size"] = "1 gallon"
    p2["items"][0]["price"]["regular"] = 3.29

    rec1 = transform_kroger_product(p1, mock_location, "Dairy")
    rec2 = transform_kroger_product(p2, mock_location, "Dairy")

    from schema import product_key
    key1 = product_key(rec1)
    key2 = product_key(rec2)
    assert key1 != key2
    assert "upc:0001115015957" in key1
    assert "upc:0001115015950" in key2
    assert rec1["weight"] == "1/2 gallon"
    assert rec2["weight"] == "1 gallon"


# ---------------------------------------------------------------------------
# 7. Pagination Safety & Kroger Skip-Offset Regression Tests
# ---------------------------------------------------------------------------

def test_pagination_skip_progression_has_zero_overlap_and_no_gaps():
    """Verify offset progression under Kroger documented semantics ('number of items to skip').

    Sequence for limit=50:
    - Page 1: skip 0   (items 1..50)
    - Page 2: skip 50  (items 51..100)
    - Page 3: skip 100 (items 101..150)
    - Page 4: skip 150 (items 151..200)
    - Page 5: skip 200 (items 201..250)
    - Page 6: skip 250 (items 251..300)
    """
    limit = 50
    offsets = [0]
    curr = 0
    while True:
        nxt = get_next_kroger_start(curr, limit=limit)
        if nxt is None:
            break
        offsets.append(nxt)
        curr = nxt

    assert offsets == [0, 50, 100, 150, 200, 250]

    # Verify contiguous non-overlapping coverage:
    # Each page covers [offset, offset + limit)
    covered_ranges = [(s, s + limit) for s in offsets]
    for i in range(len(covered_ranges) - 1):
        prev_start, prev_end = covered_ranges[i]
        next_start, next_end = covered_ranges[i + 1]
        # Next start must match previous end exactly (zero gap, zero overlap)
        assert next_start == prev_end

    # Confirm ceiling at 250
    assert get_next_kroger_start(250, limit=50) is None


def test_search_products_omits_filter_start_at_zero_and_enforces_max_bound():
    client = KrogerClient(client_id="dummy", client_secret="dummy")
    client.access_token = "fake-token"
    client.token_expiry = 9999999999

    requested_params = {}

    def fake_request(method, url, **kwargs):
        nonlocal requested_params
        requested_params = kwargs.get("params", {})
        return MockHTTPResponse(200, json_data={"data": [], "meta": {"pagination": {"total": 0}}})

    client._request_with_retry = fake_request

    # start=0: filter.start must be OMITTED from params
    client.search_products(term="milk", start=0, limit=50)
    assert "filter.start" not in requested_params

    # start=50: filter.start must be 50
    client.search_products(term="milk", start=50, limit=50)
    assert requested_params.get("filter.start") == 50

    # start=250: filter.start must be 250
    client.search_products(term="milk", start=250, limit=50)
    assert requested_params.get("filter.start") == 250

    # start=251: must be rejected before sending request
    with pytest.raises(KrogerAPIError, match=r"between 1 and 250"):
        client.search_products(term="milk", start=251)

    # start=-1: must be rejected before sending request
    with pytest.raises(KrogerAPIError, match=r"between 1 and 250"):
        client.search_products(term="milk", start=-1)


@pytest.mark.parametrize("valid_offset", [50, 100, 150, 200, 250])
def test_search_products_accepts_valid_skip_offsets(valid_offset):
    """Verify that each subsequent valid offset (50, 100, 150, 200, 250) is accepted and passed as filter.start."""
    client = KrogerClient(client_id="dummy", client_secret="dummy")
    client.access_token = "fake-token"
    client.token_expiry = 9999999999
    requested_params = {}

    def fake_request(method, url, **kwargs):
        nonlocal requested_params
        requested_params = kwargs.get("params", {})
        return MockHTTPResponse(200, json_data={"data": [], "meta": {"pagination": {"total": 0}}})

    client._request_with_retry = fake_request
    client.search_products(term="milk", start=valid_offset, limit=50)
    assert requested_params.get("filter.start") == valid_offset


def test_pagination_does_not_abort_merely_due_to_zero_new_upcs_on_page(mock_location, sample_kroger_product_raw):
    """Ensure query does not prematurely terminate if page 1 returns already-seen items but page 2 has new items."""
    import copy
    client = KrogerClient(client_id="dummy", client_secret="dummy")
    client.access_token = "fake-token"
    client.token_expiry = 9999999999

    client.verify_location = lambda **kwargs: {
        "verified": True,
        "target_location_id": "53100502",
        "returned_location_id": "53100502",
        "name": "Mariano's",
        "address": {"addressLine1": "1720 N Milwaukee Ave", "city": "Vernon Hills", "state": "IL", "zipCode": "60061"},
    }

    # Page 1 (start=0): returns 50 products that are duplicates of an existing item
    dup_page = []
    for i in range(50):
        p = copy.deepcopy(sample_kroger_product_raw)
        p["upc"] = "ALREADY_SEEN_UPC"
        p["productId"] = "ALREADY_SEEN_UPC"
        dup_page.append(p)

    # Page 2 (start=50): returns 50 brand-new unique items
    new_page = []
    for i in range(50):
        p = copy.deepcopy(sample_kroger_product_raw)
        p["upc"] = f"NEW_PAGE2_UPC_{i:03d}"
        p["productId"] = p["upc"]
        new_page.append(p)

    calls = []

    def fake_search_products(term, location_id, limit, start):
        calls.append(start)
        if start == 0:
            return dup_page, 100
        elif start == 50:
            return new_page, 100
        return [], 100

    client.search_products = fake_search_products

    records, stats, meta = collect_kroger_catalog(
        client=client,
        location=mock_location,
        categories_terms={"Dairy": ["milk"]},
        target_count=60,
        proof_only=False,
    )

    # Must NOT have stopped on page 1 (start=0)
    assert calls == [0, 50]
    # Successfully retrieved the 50 new items from page 2 (plus 1 unique from page 1)
    assert len(records) == 51


def test_pagination_moves_to_next_term_when_limit_reached(mock_location, sample_kroger_product_raw):
    import copy
    client = KrogerClient(client_id="dummy", client_secret="dummy")
    client.access_token = "fake-token"
    client.token_expiry = 9999999999

    client.verify_location = lambda **kwargs: {
        "verified": True,
        "target_location_id": "53100502",
        "returned_location_id": "53100502",
        "name": "Mariano's",
        "address": {"addressLine1": "1720 N Milwaukee Ave", "city": "Vernon Hills", "state": "IL", "zipCode": "60061"},
    }

    requested_calls = []

    def fake_search_products(term, location_id, limit, start):
        requested_calls.append((term, start))
        items = []
        for i in range(50):
            p = copy.deepcopy(sample_kroger_product_raw)
            p["upc"] = f"{term[:3]}_{start}_{i:03d}"
            p["productId"] = p["upc"]
            p["description"] = f"{term} product {start}-{i}"
            items.append(p)
        return items, 1000

    client.search_products = fake_search_products

    records, stats, meta = collect_kroger_catalog(
        client=client,
        location=mock_location,
        categories_terms={"Dairy": ["milk", "cheese"]},
        target_count=400,
        proof_only=False,
    )

    # For "milk", starts must progress: 0, 50, 100, 150, 200, 250
    milk_starts = [s for (t, s) in requested_calls if t == "milk"]
    assert milk_starts == [0, 50, 100, 150, 200, 250]
    assert not any(s > 250 for (_, s) in requested_calls)

    # Must move to "cheese" starting back at 0
    cheese_starts = [s for (t, s) in requested_calls if t == "cheese"]
    assert cheese_starts[0] == 0
    assert len(records) >= 400


def test_duplicate_upcs_across_search_terms_are_deduplicated(mock_location, sample_kroger_product_raw):
    client = KrogerClient(client_id="dummy", client_secret="dummy")
    client.access_token = "fake-token"
    client.token_expiry = 9999999999

    client.verify_location = lambda **kwargs: {
        "verified": True,
        "target_location_id": "53100502",
        "returned_location_id": "53100502",
        "name": "Mariano's",
        "address": {"addressLine1": "1720 N Milwaukee Ave", "city": "Vernon Hills", "state": "IL", "zipCode": "60061"},
    }

    # Both terms return the same exact product
    client.search_products = lambda term, **kwargs: ([sample_kroger_product_raw], 1)

    records, stats, meta = collect_kroger_catalog(
        client=client,
        location=mock_location,
        categories_terms={"Dairy": ["milk", "dairy"]},
        target_count=10,
        proof_only=False,
    )

    assert len(records) == 1
    assert records[0]["upc"] == sample_kroger_product_raw["upc"]


def test_collection_fails_safely_when_target_cannot_be_reached(mock_location, sample_kroger_product_raw):
    import copy
    client = KrogerClient(client_id="dummy", client_secret="dummy")
    client.access_token = "fake-token"
    client.token_expiry = 9999999999

    client.verify_location = lambda **kwargs: {
        "verified": True,
        "target_location_id": "53100502",
        "returned_location_id": "53100502",
        "name": "Mariano's",
        "address": {"addressLine1": "1720 N Milwaukee Ave", "city": "Vernon Hills", "state": "IL", "zipCode": "60061"},
    }

    products_list = []
    for i in range(10):
        p = copy.deepcopy(sample_kroger_product_raw)
        p["upc"] = f"UPC_{i:04d}"
        p["productId"] = p["upc"]
        products_list.append(p)

    client.search_products = lambda *args, **kwargs: (products_list, len(products_list))

    records, stats, meta = collect_kroger_catalog(
        client=client,
        location=mock_location,
        categories_terms={"Dairy": ["milk"]},
        target_count=500,
        proof_only=False,
    )

    val = validate_records(
        records=records,
        category_stats=stats,
        cfg={"min_records_for_publish": 500, "min_categories_with_products": 3},
    )

    assert val["validation_passed"] is False
    assert val["total_records"] == 10
    assert val["publish_threshold_records"] == 500


# ---------------------------------------------------------------------------
# 8. Darren's Offline Corrections Tests
# ---------------------------------------------------------------------------

def test_term_request_failure_records_diagnostics_and_fails_validation(mock_location, sample_kroger_product_raw):
    client = KrogerClient(client_id="dummy", client_secret="dummy")
    client.access_token = "fake-token"
    client.token_expiry = 9999999999

    client.verify_location = lambda **kwargs: {
        "verified": True,
        "target_location_id": "53100502",
        "returned_location_id": "53100502",
        "name": "Mariano's",
        "address": {"addressLine1": "1720 N Milwaukee Ave", "city": "Vernon Hills", "state": "IL", "zipCode": "60061"},
    }

    def failing_search_products(term, location_id, limit, start):
        if term == "cheese":
            raise KrogerAPIError("HTTP 500 server error", status_code=500)
        return [sample_kroger_product_raw], 1

    client.search_products = failing_search_products

    records, stats, meta = collect_kroger_catalog(
        client=client,
        location=mock_location,
        categories_terms={"Dairy": ["milk", "cheese"]},
        target_count=10,
        proof_only=False,
    )

    dairy_stat = next(s for s in stats if s["category"] == "Dairy")
    assert dairy_stat["status"] == "incomplete_due_to_failed_requests"
    assert len(dairy_stat["term_failures"]) == 1
    assert dairy_stat["term_failures"][0]["term"] == "cheese"
    assert dairy_stat["term_failures"][0]["status_code"] == 500
    assert dairy_stat["pagination_complete"] is False

    # Validation must never report success for incomplete or failed collection
    val = validate_records(
        records=records,
        category_stats=stats,
        cfg={"min_records_for_publish": 1, "min_categories_with_products": 1},
    )
    assert val["validation_passed"] is False
    assert "Dairy" in val["categories_not_ok"]


def test_product_retention_preserves_historical_products_and_original_dates(mock_location, sample_kroger_product_raw):
    client = KrogerClient(client_id="dummy", client_secret="dummy")
    client.access_token = "fake-token"
    client.token_expiry = 9999999999

    client.verify_location = lambda **kwargs: {
        "verified": True,
        "target_location_id": "53100502",
        "returned_location_id": "53100502",
        "name": "Mariano's",
        "address": {"addressLine1": "1720 N Milwaukee Ave", "city": "Vernon Hills", "state": "IL", "zipCode": "60061"},
    }

    # Historical product previously observed
    historical_record = {
        "zipcode": "60061",
        "store_name": "Mariano's",
        "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061",
        "store_logo": "",
        "store_brand": "yes",
        "category": "Dairy",
        "sub_category": "Specialty",
        "product_title": "Historical Artisan Cheese",
        "weight": "8 oz",
        "regular_price": "6.99",
        "sale_price": "",
        "volume_deals_description": "",
        "volume_deals_price": "",
        "digital_deals_description": "",
        "digital_deals_price": "",
        "image_url": "https://images.kroger.com/cheese.jpg",
        "url": "https://www.marianos.com/p/cheese/0009999999999",
        "upc": "0009999999999",
        "gupc": "",
        "ppm": "",
        "unit_price": "",
        "crawl_date": "2026-10-15",
    }

    # Fresh crawl returns only milk
    client.search_products = lambda *args, **kwargs: ([sample_kroger_product_raw], 1)

    records, stats, meta = collect_kroger_catalog(
        client=client,
        location=mock_location,
        categories_terms={"Dairy": ["milk"]},
        target_count=10,
        proof_only=False,
        previous_records=[historical_record],
    )

    # Must preserve the historical artisan cheese absent from the fresh crawl
    assert len(records) == 2
    cheese_rec = next(r for r in records if r["upc"] == "0009999999999")
    assert cheese_rec["product_title"] == "Historical Artisan Cheese"
    assert cheese_rec["crawl_date"] == "2026-10-15"

    # Milk from fresh crawl gets today's crawl date
    milk_rec = next(r for r in records if r["upc"] == sample_kroger_product_raw["upc"])
    assert milk_rec["crawl_date"] != "2026-10-15"


def test_resume_rejects_completed_checkpoint(tmp_path, monkeypatch, mock_location):
    store_id = mock_location["store_id"]
    data_dir = tmp_path / "data"
    cp_dir = data_dir / "checkpoints"
    data_dir.mkdir(parents=True)
    cp_dir.mkdir(parents=True)
    cp_path = cp_dir / f"checkpoint_kroger_{store_id}.json"

    # Save a completed checkpoint where all categories are "ok"
    all_cats = list(kroger_collector.DEFAULT_CATEGORY_TERMS.keys())
    completed_checkpoint = {
        "run_id": "2026-10-31",
        "store_id": store_id,
        "completed_categories": [{"category": c, "status": "ok"} for c in all_cats],
        "records_so_far": [],
    }
    cp_content = json.dumps(completed_checkpoint, indent=2)
    cp_path.write_text(cp_content, encoding="utf-8")

    # Set up config pointing to mock_location
    cfg_file = tmp_path / "settings.json"
    cfg_file.write_text(json.dumps({"locations": [mock_location]}), encoding="utf-8")

    monkeypatch.setattr(kroger_collector, "DATA_DIR", data_dir)
    monkeypatch.setattr(kroger_collector, "CHECKPOINT_DIR", cp_dir)
    monkeypatch.setenv("KROGER_CLIENT_ID", "mock_id")
    monkeypatch.setenv("KROGER_CLIENT_SECRET", "mock_secret")

    # Spy to ensure collection is NEVER started
    collector_called = False
    def mock_collect(*args, **kwargs):
        nonlocal collector_called
        collector_called = True
        raise AssertionError("collect_kroger_catalog should not be called when completed checkpoint is rejected")

    monkeypatch.setattr(kroger_collector, "collect_kroger_catalog", mock_collect)
    monkeypatch.setattr(sys, "argv", ["kroger_collector", "--resume", "--config", str(cfg_file)])

    # Executing main with --resume on a completed checkpoint must fail explicitly with code 2
    exit_code = kroger_collector.main()
    assert exit_code == 2
    assert not collector_called, "Fresh collection was silently started after resume was requested on completed checkpoint!"
    # Checkpoint must be preserved intact for inspection
    assert cp_path.read_text(encoding="utf-8") == cp_content


def test_resume_fails_explicitly_when_no_checkpoint_exists(tmp_path, monkeypatch, mock_location):
    data_dir = tmp_path / "data"
    cp_dir = data_dir / "checkpoints"
    data_dir.mkdir(parents=True)
    cp_dir.mkdir(parents=True)

    cfg_file = tmp_path / "settings.json"
    cfg_file.write_text(json.dumps({"locations": [mock_location]}), encoding="utf-8")

    monkeypatch.setattr(kroger_collector, "DATA_DIR", data_dir)
    monkeypatch.setattr(kroger_collector, "CHECKPOINT_DIR", cp_dir)
    monkeypatch.setenv("KROGER_CLIENT_ID", "mock_id")
    monkeypatch.setenv("KROGER_CLIENT_SECRET", "mock_secret")

    monkeypatch.setattr(sys, "argv", ["kroger_collector", "--resume", "--config", str(cfg_file)])
    exit_code = kroger_collector.main()
    assert exit_code == 2


def test_historical_product_price_and_date_updated_in_fresh_crawl(sample_kroger_product_raw, mock_location):
    client = KrogerClient(client_id="dummy", client_secret="dummy")
    client.access_token = "fake-token"
    client.token_expiry = 9999999999
    client.verify_location = lambda **kwargs: {
        "verified": True,
        "target_location_id": "53100502",
        "returned_location_id": "53100502",
        "name": "Mariano's",
        "address": {"addressLine1": "1720 N Milwaukee Ave", "city": "Vernon Hills", "state": "IL", "zipCode": "60061"},
    }

    # Historical catalog contains Milk with price 3.99 and old date, plus Cheese with price 6.99
    hist_milk = blank_record()
    hist_milk.update({
        "zipcode": "60061",
        "store_name": "Mariano's",
        "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061",
        "category": "Dairy",
        "product_title": "Simple Truth Organic Whole Milk",
        "regular_price": "3.99",
        "upc": sample_kroger_product_raw["upc"],
        "url": "https://www.marianos.com/p/milk/0001111041700",
        "crawl_date": "2026-10-15",
    })
    hist_cheese = blank_record()
    hist_cheese.update({
        "zipcode": "60061",
        "store_name": "Mariano's",
        "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061",
        "category": "Dairy",
        "product_title": "Artisan White Cheddar",
        "regular_price": "6.99",
        "upc": "0009999999999",
        "url": "https://www.marianos.com/p/cheese/0009999999999",
        "crawl_date": "2026-10-15",
    })

    # Fresh API crawl returns Milk with UPDATED regular price 4.49 and sale promo price 3.99
    fresh_milk_raw = copy.deepcopy(sample_kroger_product_raw)
    fresh_milk_raw["items"][0]["price"]["regular"] = 4.49
    fresh_milk_raw["items"][0]["price"]["promo"] = 3.99
    client.search_products = lambda *args, **kwargs: ([fresh_milk_raw], 1)

    records, stats, meta = collect_kroger_catalog(
        client=client,
        location=mock_location,
        categories_terms={"Dairy": ["milk"]},
        target_count=10,
        proof_only=False,
        previous_records=[hist_milk, hist_cheese],
    )

    # 1. Milk MUST be updated with new price and today's crawl date (NOT dropped as duplicate!)
    assert len(records) == 2
    milk = next(r for r in records if r["upc"] == sample_kroger_product_raw["upc"])
    assert milk["regular_price"] == "4.49"
    assert milk["sale_price"] == "3.99"
    assert milk["crawl_date"] != "2026-10-15"

    # 2. Historical cheese absent from the fresh crawl MUST be preserved with its original date
    cheese = next(r for r in records if r["upc"] == "0009999999999")
    assert cheese["regular_price"] == "6.99"
    assert cheese["crawl_date"] == "2026-10-15"


def test_category_movement_reclassification_prevents_duplicate_product_keys(sample_kroger_product_raw, mock_location):
    client = KrogerClient(client_id="dummy", client_secret="dummy")
    client.access_token = "fake-token"
    client.token_expiry = 9999999999
    client.verify_location = lambda **kwargs: {
        "verified": True,
        "target_location_id": "53100502",
        "returned_location_id": "53100502",
        "name": "Mariano's",
        "address": {"addressLine1": "1720 N Milwaukee Ave", "city": "Vernon Hills", "state": "IL", "zipCode": "60061"},
    }

    # Product A was previously in "Dairy"
    hist_item_a = blank_record()
    hist_item_a.update({
        "zipcode": "60061",
        "store_name": "Mariano's",
        "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061",
        "category": "Dairy",
        "product_title": "Organic Apple Juice",
        "regular_price": "3.49",
        "upc": "0005555555555",
        "url": "https://www.marianos.com/p/juice/0005555555555",
        "crawl_date": "2026-10-15",
    })
    # Product B remains in "Dairy"
    hist_item_b = blank_record()
    hist_item_b.update({
        "zipcode": "60061",
        "store_name": "Mariano's",
        "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061",
        "category": "Dairy",
        "product_title": "Butter",
        "regular_price": "4.99",
        "upc": "0006666666666",
        "url": "https://www.marianos.com/p/butter/0006666666666",
        "crawl_date": "2026-10-15",
    })

    # Fresh crawl now categorizes Product A under "Produce"
    reclassified_raw = copy.deepcopy(sample_kroger_product_raw)
    reclassified_raw["upc"] = "0005555555555"
    reclassified_raw["productId"] = "0005555555555"
    reclassified_raw["description"] = "Organic Apple Juice"
    reclassified_raw["items"][0]["price"]["regular"] = 3.99
    client.search_products = lambda *args, **kwargs: ([reclassified_raw], 1)

    records, stats, meta = collect_kroger_catalog(
        client=client,
        location=mock_location,
        categories_terms={"Produce": ["juice"]},
        target_count=10,
        proof_only=False,
        previous_records=[hist_item_a, hist_item_b],
    )

    # Product A must appear EXACTLY ONCE, updated to "Produce"
    juice_records = [r for r in records if r["upc"] == "0005555555555"]
    assert len(juice_records) == 1
    assert juice_records[0]["category"] == "Produce"
    assert juice_records[0]["regular_price"] == "3.99"

    # Product B must remain preserved under "Dairy"
    butter_records = [r for r in records if r["upc"] == "0006666666666"]
    assert len(butter_records) == 1
    assert butter_records[0]["category"] == "Dairy"

    # Validation must PASS with 0 duplicates
    val = validate_records(records, stats, {"min_records_for_publish": 1, "min_categories_with_products": 1})
    assert val["validation_passed"] is True
    assert val["duplicate_count"] == 0


def test_temporary_category_failure_preserves_historical_records(sample_kroger_product_raw, mock_location):
    client = KrogerClient(client_id="dummy", client_secret="dummy")
    client.access_token = "fake-token"
    client.token_expiry = 9999999999
    client.verify_location = lambda **kwargs: {
        "verified": True,
        "target_location_id": "53100502",
        "returned_location_id": "53100502",
        "name": "Mariano's",
        "address": {"addressLine1": "1720 N Milwaukee Ave", "city": "Vernon Hills", "state": "IL", "zipCode": "60061"},
    }

    hist_item = blank_record()
    hist_item.update({
        "zipcode": "60061",
        "store_name": "Mariano's",
        "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061",
        "category": "Produce",
        "product_title": "Honeycrisp Apples",
        "regular_price": "2.99",
        "upc": "0007777777777",
        "url": "https://www.marianos.com/p/apples/0007777777777",
        "crawl_date": "2026-10-15",
    })

    # Produce query raises KrogerAPIError (simulating upstream service error)
    def fail_search(*args, **kwargs):
        raise KrogerAPIError("HTTP 500 upstream server error", status_code=500)

    client.search_products = fail_search

    records, stats, meta = collect_kroger_catalog(
        client=client,
        location=mock_location,
        categories_terms={"Produce": ["apples"]},
        target_count=10,
        proof_only=False,
        previous_records=[hist_item],
    )

    # 1. Historical record MUST be preserved (not discarded because of temporary failure)
    assert any(r["upc"] == "0007777777777" for r in records)

    # 2. Category status must be incomplete_due_to_failed_requests, and validation must fail closed
    produce_stat = next(s for s in stats if s["category"] == "Produce")
    assert produce_stat["status"] == "incomplete_due_to_failed_requests"
    val = validate_records(records, stats, {"min_records_for_publish": 1, "min_categories_with_products": 1})
    assert val["validation_passed"] is False


def test_approx_weight_variable_pricing_exclusion_and_multipack_preservation(sample_kroger_product_raw, mock_location):
    import copy

    # 1. Variable weight must be excluded
    p_var = copy.deepcopy(sample_kroger_product_raw)
    p_var["description"] = "Pork Chops Variable Weight"
    assert transform_kroger_product(p_var, mock_location, "Meat & Seafood") is None

    # 2. Random weight must be excluded
    p_rand = copy.deepcopy(sample_kroger_product_raw)
    p_rand["description"] = "Chuck Roast (Random Weight)"
    assert transform_kroger_product(p_rand, mock_location, "Meat & Seafood") is None

    # 3. Average weight must be excluded
    p_avg = copy.deepcopy(sample_kroger_product_raw)
    p_avg["description"] = "Whole Chicken Avg. Wt. 4 lb"
    assert transform_kroger_product(p_avg, mock_location, "Meat & Seafood") is None

    # 4. Approx weight must be excluded
    p_app = copy.deepcopy(sample_kroger_product_raw)
    p_app["description"] = "Ground Turkey Approx. 1.5 lb"
    assert transform_kroger_product(p_app, mock_location, "Meat & Seafood") is None

    # 5. Genuine fixed-weight multipack must be PRESERVED
    p_multi = copy.deepcopy(sample_kroger_product_raw)
    p_multi["description"] = "Twin Pack 2x16 oz Organic Milk"
    p_multi["items"][0]["size"] = "2x16 oz"
    assert transform_kroger_product(p_multi, mock_location, "Dairy") is not None

    # 6. Genuine fixed-weight family pack must be PRESERVED
    p_fam = copy.deepcopy(sample_kroger_product_raw)
    p_fam["description"] = "Family Pack Chicken Breast 3 lb"
    p_fam["items"][0]["size"] = "3 lb"
    assert transform_kroger_product(p_fam, mock_location, "Meat & Seafood") is not None

    # 7. Brand foundation year must be PRESERVED
    p_est = copy.deepcopy(sample_kroger_product_raw)
    p_est["description"] = "Farmer John Ham Est. 1920"
    p_est["items"][0]["size"] = "16 oz"
    assert transform_kroger_product(p_est, mock_location, "Meat & Seafood") is not None


def test_interruption_saves_checkpoint_and_resumes_cleanly(tmp_path, monkeypatch, mock_location, sample_kroger_product_raw):
    data_dir = tmp_path / "data"
    evidence_dir = data_dir / "evidence"
    cp_dir = data_dir / "checkpoints"
    data_dir.mkdir(parents=True)
    evidence_dir.mkdir(parents=True)
    cp_dir.mkdir(parents=True)

    monkeypatch.setattr(kroger_collector, "DATA_DIR", data_dir)
    monkeypatch.setattr(kroger_collector, "EVIDENCE_DIR", evidence_dir)
    monkeypatch.setattr(kroger_collector, "CHECKPOINT_DIR", cp_dir)

    client = KrogerClient(client_id="dummy", client_secret="dummy")
    client.access_token = "fake-token"
    client.token_expiry = 9999999999
    client.verify_location = lambda **kwargs: {
        "verified": True,
        "target_location_id": "53100502",
        "returned_location_id": "53100502",
        "name": "Mariano's",
        "address": {"addressLine1": "1720 N Milwaukee Ave", "city": "Vernon Hills", "state": "IL", "zipCode": "60061"},
    }

    call_count = 0
    def search_with_interruption(term, location_id, limit, start):
        nonlocal call_count
        call_count += 1
        if term == "apple":
            raise KeyboardInterrupt("Simulated operator interrupt")
        return [sample_kroger_product_raw], 1

    client.search_products = search_with_interruption

    with pytest.raises(KeyboardInterrupt):
        collect_kroger_catalog(
            client=client,
            location=mock_location,
            categories_terms={"Dairy": ["milk"], "Produce": ["apple"]},
            target_count=10,
            proof_only=False,
        )

    # Checkpoint must exist with Dairy: ok and Produce: interrupted
    cp_file = cp_dir / "checkpoint_kroger_53100502.json"
    assert cp_file.exists()
    cp = json.loads(cp_file.read_text(encoding="utf-8"))
    cats = {c["category"]: c["status"] for c in cp["completed_categories"]}
    assert cats.get("Dairy") == "ok"
    assert cats.get("Produce") == "interrupted"

    # Now resume with saved checkpoint
    produce_product = dict(sample_kroger_product_raw)
    produce_product["productId"] = "0001111099999"
    produce_product["upc"] = "0001111099999"
    produce_product["description"] = "Honeycrisp Apples 3 lb bag"
    client.search_products = lambda term, location_id, limit, start: ([produce_product], 1)
    records, stats, meta = collect_kroger_catalog(
        client=client,
        location=mock_location,
        categories_terms={"Dairy": ["milk"], "Produce": ["apple"]},
        target_count=10,
        proof_only=False,
        resume_checkpoint=cp,
    )

    resumed_cats = {c["category"]: c["status"] for c in stats}
    assert resumed_cats.get("Dairy") == "ok"
    assert resumed_cats.get("Produce") == "ok"


def test_atomic_staging_and_failure_preservation(tmp_path, monkeypatch, mock_location):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    monkeypatch.setattr(kroger_collector, "DATA_DIR", data_dir)
    monkeypatch.setattr(kroger_collector, "EVIDENCE_DIR", data_dir / "evidence")
    monkeypatch.setattr(kroger_collector, "CHECKPOINT_DIR", data_dir / "checkpoints")

    latest_file = data_dir / "marianos_latest.json"
    latest_file.write_text('[{"preserved": "true"}]', encoding="utf-8")

    # If validation fails, latest_file must be preserved unchanged
    staging_file = data_dir / "staging_test.json"
    staging_file.write_text('[{"corrupt": "data"}]', encoding="utf-8")

    # Validation failure does not overwrite
    assert latest_file.read_text(encoding="utf-8") == '[{"preserved": "true"}]'


def test_export_catalog_to_csv_schema_and_types(tmp_path, sample_kroger_product_raw, mock_location):
    import csv
    from kroger_collector import export_catalog_to_csv

    rec = transform_kroger_product(sample_kroger_product_raw, mock_location, "Dairy")
    assert rec is not None

    csv_path = tmp_path / "test_catalog.csv"
    export_catalog_to_csv([rec], csv_path)

    assert csv_path.exists()
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        row = next(reader)

    # 1. Exact 22 fields in exact order
    assert header == FIELDS
    assert len(header) == 22
    assert len(row) == 22

    # 2. Values match schema record and empty fields are empty strings
    row_dict = dict(zip(header, row))
    assert row_dict["store_name"] == "Mariano's"
    assert row_dict["zipcode"] == "60061"
    assert row_dict["category"] == "Dairy"
    assert row_dict["product_title"] == "Simple Truth Organic Whole Milk"
    assert row_dict["regular_price"] == "4.29"
    assert row_dict["sale_price"] == "3.79"
    assert row_dict["gupc"] == ""
    assert row_dict["unit_price"] == ""
    assert row_dict["ppm"] == ""
    assert row_dict["store_logo"] == ""
    assert row_dict["volume_deals_description"] == ""
    assert row_dict["volume_deals_price"] == ""
    assert row_dict["digital_deals_description"] == ""
    assert row_dict["digital_deals_price"] == ""


def test_compute_field_completeness_all_fields(sample_kroger_product_raw, mock_location):
    from kroger_collector import compute_field_completeness

    rec1 = transform_kroger_product(sample_kroger_product_raw, mock_location, "Dairy")
    assert rec1 is not None

    rec2 = dict(rec1)
    rec2["sale_price"] = ""  # Not on sale

    stats = compute_field_completeness([rec1, rec2])

    assert len(stats) == 22
    for f in FIELDS:
        assert f in stats
        assert "populated_count" in stats[f]
        assert "empty_count" in stats[f]
        assert "completeness_pct" in stats[f]

    # regular_price is populated in both
    assert stats["regular_price"]["populated_count"] == 2
    assert stats["regular_price"]["completeness_pct"] == 100.0

    # sale_price is populated in only one
    assert stats["sale_price"]["populated_count"] == 1
    assert stats["sale_price"]["completeness_pct"] == 50.0

    # gupc and unit_price are empty in both
    assert stats["gupc"]["populated_count"] == 0
    assert stats["gupc"]["completeness_pct"] == 0.0
    assert stats["unit_price"]["populated_count"] == 0


def test_kroger_client_api_request_statistics():
    client = KrogerClient(client_id="dummy", client_secret="dummy", max_retries=1)
    client.sleep_fn = lambda s: None

    assert client.get_statistics()["total_requests"] == 0
    assert client.get_statistics()["successful_requests"] == 0

    responses = [
        MockHTTPResponse(429, headers={"Retry-After": "0.1"}),
        MockHTTPResponse(200, json_data={"data": []}),
    ]
    client.session.request = lambda *args, **kwargs: responses.pop(0)

    client._request_with_retry("GET", "https://api.kroger.com/test")
    stats = client.get_statistics()
    assert stats["total_requests"] == 2
    assert stats["rate_limit_429_count"] == 1
    assert stats["retries_count"] == 1
    assert stats["successful_requests"] == 1
    assert stats["failed_requests"] == 0


def test_fourteen_departments_coverage_in_default_terms():
    from kroger_collector import DEFAULT_CATEGORY_TERMS

    required_departments = [
        "Produce",
        "Dairy",
        "Meat & Seafood",
        "Bakery",
        "Frozen Foods",
        "Pantry",
        "Snacks",
        "Beverages",
        "Breakfast",
        "Canned Goods",
        "Household",
        "Personal Care",
        "Baby",
        "Pet Supplies",
    ]

    for dept in required_departments:
        assert dept in DEFAULT_CATEGORY_TERMS, f"Missing required department: {dept}"
        assert len(DEFAULT_CATEGORY_TERMS[dept]) >= 4, f"Insufficient search terms for {dept}"


def test_retention_pipeline_preserves_unavailable_eligible_product_with_original_date(sample_kroger_product_raw, mock_location):
    """Verify that when a product disappears from the next API response, the retention pipeline

    preserves the unavailable eligible product in the final catalog with its original crawl date.
    """
    client = KrogerClient(client_id="dummy", client_secret="dummy")
    client.access_token = "fake-token"
    client.token_expiry = 9999999999
    client.verify_location = lambda **kwargs: {
        "verified": True,
        "target_location_id": "53100502",
        "returned_location_id": "53100502",
        "name": "Mariano's",
        "address": {"addressLine1": "1720 N Milwaukee Ave", "city": "Vernon Hills", "state": "IL", "zipCode": "60061"},
    }

    # Initial catalog contains two products: Product A and Product B from an earlier crawl on 2026-10-10
    product_a = blank_record()
    product_a.update({
        "zipcode": "60061",
        "store_name": "Mariano's",
        "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061",
        "category": "Dairy",
        "product_title": "Simple Truth Whole Milk",
        "weight": "1 gal",
        "regular_price": "4.19",
        "upc": "0001111041700",
        "url": "https://www.marianos.com/p/simple-truth-whole-milk/0001111041700",
        "crawl_date": "2026-10-10",
    })

    product_b_unavailable = blank_record()
    product_b_unavailable.update({
        "zipcode": "60061",
        "store_name": "Mariano's",
        "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061",
        "category": "Dairy",
        "product_title": "Organic Greek Yogurt Plain",
        "weight": "32 oz",
        "regular_price": "5.49",
        "upc": "0001111099999",
        "url": "https://www.marianos.com/p/organic-greek-yogurt-plain/0001111099999",
        "crawl_date": "2026-10-10",
    })

    # The fresh API crawl returns ONLY Product A with an updated price (4.49).
    # Product B has disappeared from the API response (e.g., out of stock or delisted from search).
    fresh_item = copy.deepcopy(sample_kroger_product_raw)
    fresh_item["upc"] = "0001111041700"
    fresh_item["productId"] = "0001111041700"
    fresh_item["description"] = "Simple Truth Whole Milk"
    fresh_item["items"][0]["price"]["regular"] = 4.49
    fresh_item["items"][0]["price"]["promo"] = 3.99

    client.search_products = lambda term, location_id, limit, start: ([fresh_item], 1)

    records, stats, meta = collect_kroger_catalog(
        client=client,
        location=mock_location,
        categories_terms={"Dairy": ["milk"]},
        target_count=2,
        proof_only=False,
        previous_records=[product_a, product_b_unavailable],
    )

    # Invariants verification:
    # 1. Total records must be 2 (Product B was not discarded despite being absent from fresh API results)
    assert len(records) == 2

    # 2. Unavailable Product B must be preserved with its exact original crawl_date and price
    retained_b = next(r for r in records if r["upc"] == "0001111099999")
    assert retained_b["crawl_date"] == "2026-10-10"
    assert retained_b["regular_price"] == "5.49"
    assert retained_b["product_title"] == "Organic Greek Yogurt Plain"

    # 3. Product A must be updated with the new price and the fresh crawl date
    updated_a = next(r for r in records if r["upc"] == "0001111041700")
    assert updated_a["regular_price"] == "4.49"
    assert updated_a["sale_price"] == "3.99"
    assert updated_a["crawl_date"] != "2026-10-10"

    # 4. Final validation passes cleanly
    val = validate_records(records, stats, {"min_records_for_publish": 2, "min_categories_with_products": 1})
    assert val["validation_passed"] is True
    assert val["duplicate_count"] == 0


def test_successful_api_response_with_incomplete_pagination_preserves_historical_records(
    sample_kroger_product_raw, mock_location
):
    """Simulate API returning HTTP 200 with truncated/incomplete pagination.

    Category must be marked 'incomplete', validation must fail closed,
    and existing historical records must be preserved without loss.
    """
    import time
    client = KrogerClient(client_id="dummy", client_secret="dummy")
    client.access_token = "valid_token"
    client.token_expiry = time.time() + 3600
    client.verify_location = lambda **kwargs: {
        "verified": True,
        "target_location_id": "53100502",
        "returned_location_id": "53100502",
        "name": "Mariano's",
        "address": {"addressLine1": "1720 N Milwaukee Ave", "city": "Vernon Hills", "state": "IL", "zipCode": "60061"},
    }

    hist_item = blank_record()
    hist_item.update({
        "zipcode": "60061",
        "store_name": "Mariano's",
        "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061",
        "category": "Dairy",
        "product_title": "Historical Organic Whole Milk",
        "regular_price": "3.99",
        "upc": "0001111041700",
        "url": "https://www.marianos.com/p/milk/0001111041700",
        "crawl_date": "2026-10-01",
    })

    fresh_item = copy.deepcopy(sample_kroger_product_raw)
    fresh_item["upc"] = "0001111088888"
    fresh_item["productId"] = "0001111088888"
    fresh_item["description"] = "Fresh Item Partial Crawl"
    fresh_item["items"][0]["price"]["regular"] = 2.49

    # Simulate: search reports total=100 available items, but page 0 returns only 1 product (less than limit 50)
    # even though total is 100, and target per category is 10.
    def mock_truncated_search(term, location_id, limit, start):
        if start == 0:
            return ([fresh_item], 100)
        return ([], 100)

    client.search_products = mock_truncated_search

    records, stats, meta = collect_kroger_catalog(
        client=client,
        location=mock_location,
        categories_terms={"Dairy": ["milk"]},
        target_count=10,
        proof_only=False,
        previous_records=[hist_item],
    )

    # 1. Category must be marked 'incomplete' with 'incomplete_pagination'
    dairy_stat = next(s for s in stats if s["category"] == "Dairy")
    assert dairy_stat["status"] == "incomplete"
    assert dairy_stat["pagination_complete"] is False

    # 2. Historical product MUST be preserved (not deleted or replaced by incomplete crawl)
    assert any(r["upc"] == "0001111041700" for r in records)
    retained = next(r for r in records if r["upc"] == "0001111041700")
    assert retained["regular_price"] == "3.99"
    assert retained["crawl_date"] == "2026-10-01"

    # 3. Validation must fail closed
    val = validate_records(records, stats, {"min_records_for_publish": 1, "min_categories_with_products": 1})
    assert val["validation_passed"] is False
    assert "Dairy" in val["categories_not_ok"]


# ---------------------------------------------------------------------------
# 9. Publication Safety and Indefinite-Retention Regression Tests (Darren Phase 1)
# ---------------------------------------------------------------------------

def test_real_publication_path_600_to_500_indefinite_retention(tmp_path, monkeypatch, mock_location):
    """Darren regression test: A 600-product previous catalog MUST retain all 100 absent

    products when a new crawl collects 500 products. Final published catalog has 600 items.
    """
    data_dir = tmp_path / "data"
    cp_dir = data_dir / "checkpoints"
    evidence_dir = data_dir / "evidence"
    data_dir.mkdir(parents=True)
    cp_dir.mkdir(parents=True)
    evidence_dir.mkdir(parents=True)

    depts = list(kroger_collector.DEFAULT_CATEGORY_TERMS.keys())

    # 1. Previous catalog with 600 unique, valid products dated 2026-10-15
    previous_catalog = []
    for i in range(600):
        cat = depts[i % len(depts)]
        upc = f"{i + 1:013d}"
        rec = blank_record()
        rec.update({
            "zipcode": mock_location["zipcode"],
            "store_name": mock_location["store_name"],
            "store_location": mock_location["store_location"],
            "store_logo": "",
            "store_brand": "no",
            "category": cat,
            "sub_category": "",
            "product_title": f"Catalog Product {i + 1}",
            "weight": "16 oz",
            "regular_price": "5.99",
            "sale_price": "",
            "volume_deals_description": "",
            "volume_deals_price": "",
            "digital_deals_description": "",
            "digital_deals_price": "",
            "image_url": f"https://images.kroger.com/{upc}.jpg",
            "url": f"https://www.marianos.com/p/product-{i + 1}/{upc}",
            "upc": upc,
            "gupc": "",
            "ppm": "",
            "unit_price": "",
            "crawl_date": "2026-10-15",
        })
        previous_catalog.append(rec)

    latest_file = data_dir / "marianos_latest.json"
    latest_file.write_text(json.dumps(previous_catalog, indent=2), encoding="utf-8")

    # 2. Config file
    cfg_file = tmp_path / "settings.json"
    cfg = {
        "locations": [mock_location],
        "min_records_for_publish": 500,
        "min_categories_with_products": 5,
    }
    cfg_file.write_text(json.dumps(cfg), encoding="utf-8")

    # 3. Fresh crawl: 500 products (indices 0..499). Products 500..599 are absent.
    cat_to_new_raw = {c: [] for c in depts}
    for i in range(500):
        cat = depts[i % len(depts)]
        upc = f"{i + 1:013d}"
        raw_p = {
            "productId": upc,
            "upc": upc,
            "description": f"Catalog Product {i + 1}",
            "items": [
                {
                    "itemId": upc,
                    "size": "16 oz",
                    "price": {"regular": 6.49, "promo": 5.49},
                    "fulfillment": {"instore": True, "curbside": True, "delivery": True},
                }
            ],
            "categories": [cat],
            "productPageURI": f"/p/product-{i + 1}/{upc}",
        }
        cat_to_new_raw[cat].append(raw_p)

    mock_client = KrogerClient(client_id="dummy", client_secret="dummy")
    mock_client.access_token = "mock-token"
    mock_client.token_expiry = 9999999999
    mock_client.verify_location = lambda **kwargs: {
        "verified": True,
        "target_location_id": "53100502",
        "returned_location_id": "53100502",
        "name": "Mariano's",
        "address": {"addressLine1": "1720 N Milwaukee Ave", "city": "Vernon Hills", "state": "IL", "zipCode": "60061"},
    }

    def mock_search(term, location_id, limit, start):
        target_cat = next((c for c, terms in kroger_collector.DEFAULT_CATEGORY_TERMS.items() if term in terms), None)
        if not target_cat:
            return [], 0
        first_term = kroger_collector.DEFAULT_CATEGORY_TERMS[target_cat][0]
        if term == first_term and start == 0:
            items = cat_to_new_raw[target_cat]
            return items, len(items)
        return [], 0

    mock_client.search_products = mock_search

    monkeypatch.setattr(kroger_collector, "DATA_DIR", data_dir)
    monkeypatch.setattr(kroger_collector, "CHECKPOINT_DIR", cp_dir)
    monkeypatch.setattr(kroger_collector, "EVIDENCE_DIR", evidence_dir)
    monkeypatch.setattr(kroger_collector, "KrogerClient", lambda *args, **kwargs: mock_client)
    monkeypatch.setattr(sys, "argv", ["kroger_collector", "--config", str(cfg_file), "--target-count", "500"])

    exit_code = kroger_collector.main()
    assert exit_code == 0

    # 4. Verify publication results
    published_records = json.loads(latest_file.read_text(encoding="utf-8"))
    assert len(published_records) == 600, f"Expected 600 records, got {len(published_records)}"

    # Check latest CSV has exact 601 lines (1 header + 600 records)
    latest_csv = data_dir / "marianos_latest.csv"
    assert latest_csv.exists()
    csv_lines = [l for l in latest_csv.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(csv_lines) == 601

    by_upc = {r["upc"]: r for r in published_records}
    assert len(by_upc) == 600

    # A. 500 freshly collected products receive new prices and today's crawl date
    for i in range(500):
        upc = f"{i + 1:013d}"
        rec = by_upc[upc]
        assert rec["regular_price"] == "6.49"
        assert rec["sale_price"] == "5.49"
        assert rec["crawl_date"] != "2026-10-15"

    # B. 100 absent products are preserved with original date and price (not marked as newly collected)
    for i in range(500, 600):
        upc = f"{i + 1:013d}"
        rec = by_upc[upc]
        assert rec["regular_price"] == "5.99"
        assert rec["sale_price"] == ""
        assert rec["crawl_date"] == "2026-10-15"


def test_failures_preserve_published_catalog_byte_for_byte(tmp_path, monkeypatch, mock_location):
    """Verify that any failure in subcategory, next-page, parsing, incomplete pagination,

    or validation preserves the previous published catalog byte-for-byte.
    """
    import hashlib

    data_dir = tmp_path / "data"
    cp_dir = data_dir / "checkpoints"
    evidence_dir = data_dir / "evidence"
    data_dir.mkdir(parents=True)
    cp_dir.mkdir(parents=True)
    evidence_dir.mkdir(parents=True)

    # Base previous catalog
    previous_catalog = []
    for i in range(10):
        upc = f"{i + 1:013d}"
        rec = blank_record()
        rec.update({
            "zipcode": "60061", "store_name": "Mariano's",
            "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061",
            "category": "Dairy", "product_title": f"Product {i + 1}",
            "regular_price": "4.99", "url": f"https://www.marianos.com/p/{upc}",
            "upc": upc, "crawl_date": "2026-10-01",
        })
        previous_catalog.append(rec)

    latest_file = data_dir / "marianos_latest.json"
    latest_content = json.dumps(previous_catalog, indent=2)
    latest_file.write_text(latest_content, encoding="utf-8")
    original_sha256 = hashlib.sha256(latest_file.read_bytes()).hexdigest()

    cfg_file = tmp_path / "settings.json"
    cfg = {
        "locations": [mock_location],
        "min_records_for_publish": 5,
        "min_categories_with_products": 1,
    }
    cfg_file.write_text(json.dumps(cfg), encoding="utf-8")

    monkeypatch.setattr(kroger_collector, "DATA_DIR", data_dir)
    monkeypatch.setattr(kroger_collector, "CHECKPOINT_DIR", cp_dir)
    monkeypatch.setattr(kroger_collector, "EVIDENCE_DIR", evidence_dir)
    monkeypatch.setattr(sys, "argv", ["kroger_collector", "--config", str(cfg_file), "--target-count", "10"])

    # Scenario 1: Term / API request failure (HTTP 500)
    client_500 = KrogerClient(client_id="dummy", client_secret="dummy")
    client_500.access_token = "token"
    client_500.token_expiry = 9999999999
    client_500.verify_location = lambda **kwargs: {
        "verified": True, "target_location_id": "53100502", "returned_location_id": "53100502",
        "name": "Mariano's", "address": {"addressLine1": "1720 N Milwaukee Ave", "city": "Vernon Hills", "state": "IL", "zipCode": "60061"},
    }
    def failing_search(*args, **kwargs):
        raise KrogerAPIError("HTTP 500 server error", status_code=500)
    client_500.search_products = failing_search
    monkeypatch.setattr(kroger_collector, "KrogerClient", lambda *args, **kwargs: client_500)

    exit_code = kroger_collector.main()
    assert exit_code == 2
    assert hashlib.sha256(latest_file.read_bytes()).hexdigest() == original_sha256

    # Scenario 2: Incomplete pagination (total reported 100, but only 1 returned on page 0 and 0 afterwards)
    sample_raw = {
        "productId": "0001111000001", "upc": "0001111000001", "description": "Milk 1",
        "items": [{"itemId": "1", "size": "1 gal", "price": {"regular": 4.99}}],
        "categories": ["Dairy"], "productPageURI": "/p/milk/0001111000001",
    }
    def truncated_search(term, location_id, limit, start):
        if start == 0:
            return [sample_raw], 100
        return [], 100

    client_incomplete = KrogerClient(client_id="dummy", client_secret="dummy")
    client_incomplete.access_token = "token"
    client_incomplete.token_expiry = 9999999999
    client_incomplete.verify_location = client_500.verify_location
    client_incomplete.search_products = truncated_search
    monkeypatch.setattr(kroger_collector, "KrogerClient", lambda *args, **kwargs: client_incomplete)

    exit_code = kroger_collector.main()
    assert exit_code == 2
    assert hashlib.sha256(latest_file.read_bytes()).hexdigest() == original_sha256

    # Scenario 3: Validation failure immediately before publication (target_threshold not met)
    def single_item_search(term, location_id, limit, start):
        return [sample_raw], 1

    client_valid_fail = KrogerClient(client_id="dummy", client_secret="dummy")
    client_valid_fail.access_token = "token"
    client_valid_fail.token_expiry = 9999999999
    client_valid_fail.verify_location = client_500.verify_location
    client_valid_fail.search_products = single_item_search
    monkeypatch.setattr(kroger_collector, "KrogerClient", lambda *args, **kwargs: client_valid_fail)
    monkeypatch.setattr(sys, "argv", ["kroger_collector", "--config", str(cfg_file), "--target-count", "1000"])

    exit_code = kroger_collector.main()
    assert exit_code == 2
    assert hashlib.sha256(latest_file.read_bytes()).hexdigest() == original_sha256

    # Scenario 4: Next-page request failure (page 0 succeeds, page 1 raises KrogerAPIError)
    def next_page_fail_search(term, location_id, limit, start):
        if start == 0:
            return [sample_raw], 100
        raise KrogerAPIError("HTTP 502 bad gateway on next page", status_code=502)

    client_next_fail = KrogerClient(client_id="dummy", client_secret="dummy")
    client_next_fail.access_token = "token"
    client_next_fail.token_expiry = 9999999999
    client_next_fail.verify_location = client_500.verify_location
    client_next_fail.search_products = next_page_fail_search
    monkeypatch.setattr(kroger_collector, "KrogerClient", lambda *args, **kwargs: client_next_fail)
    monkeypatch.setattr(sys, "argv", ["kroger_collector", "--config", str(cfg_file), "--target-count", "10"])

    exit_code = kroger_collector.main()
    assert exit_code == 2
    assert hashlib.sha256(latest_file.read_bytes()).hexdigest() == original_sha256

    # Scenario 5: Product parsing failure (all items missing title/price, resulting in 0 valid records)
    unparseable_raw = {"productId": "000999", "description": "", "items": []}
    def parse_fail_search(term, location_id, limit, start):
        return [unparseable_raw], 1

    client_parse_fail = KrogerClient(client_id="dummy", client_secret="dummy")
    client_parse_fail.access_token = "token"
    client_parse_fail.token_expiry = 9999999999
    client_parse_fail.verify_location = client_500.verify_location
    client_parse_fail.search_products = parse_fail_search
    monkeypatch.setattr(kroger_collector, "KrogerClient", lambda *args, **kwargs: client_parse_fail)

    exit_code = kroger_collector.main()
    assert exit_code == 2
    assert hashlib.sha256(latest_file.read_bytes()).hexdigest() == original_sha256

    # Scenario 6: Malformed previous catalog on disk (corrupted JSON)
    corrupted_content = "{corrupted json content:::"
    latest_file.write_text(corrupted_content, encoding="utf-8")
    corrupted_sha = hashlib.sha256(latest_file.read_bytes()).hexdigest()

    with pytest.raises(RuntimeError, match="Previous catalog is unreadable"):
        kroger_collector.main()
    # Malformed file must remain byte-for-byte untouched
    assert hashlib.sha256(latest_file.read_bytes()).hexdigest() == corrupted_sha


def test_approx_weight_excluded_from_retention_multipacks_preserved(mock_location):
    """Verify approximate-weight items are excluded across retention while fixed-weight

    multipacks (e.g. 2x16 oz) and family packs (3 lb) remain eligible.
    """
    client = KrogerClient(client_id="dummy", client_secret="dummy")
    client.access_token = "fake-token"
    client.token_expiry = 9999999999
    client.verify_location = lambda **kwargs: {
        "verified": True, "target_location_id": "53100502", "returned_location_id": "53100502",
        "name": "Mariano's", "address": {"addressLine1": "1720 N Milwaukee Ave", "city": "Vernon Hills", "state": "IL", "zipCode": "60061"},
    }

    hist_multipack = blank_record()
    hist_multipack.update({
        "zipcode": "60061", "store_name": "Mariano's",
        "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061",
        "category": "Dairy", "product_title": "Twin Pack 2x16 oz Whole Milk",
        "weight": "2x16 oz", "regular_price": "5.99",
        "upc": "0001111000010", "url": "https://www.marianos.com/p/milk/0001111000010",
        "crawl_date": "2026-10-01",
    })

    hist_approx = blank_record()
    hist_approx.update({
        "zipcode": "60061", "store_name": "Mariano's",
        "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061",
        "category": "Meat & Seafood", "product_title": "Ground Beef (Approx. 1.5 lb)",
        "weight": "approx 1.5 lb", "regular_price": "6.99",
        "upc": "0001111000020", "url": "https://www.marianos.com/p/beef/0001111000020",
        "crawl_date": "2026-10-01",
    })

    raw_family = {
        "productId": "0001111000030", "upc": "0001111000030",
        "description": "Family Pack Chicken Breast 3 lb",
        "items": [{"itemId": "30", "size": "3 lb", "price": {"regular": 9.99}}],
        "categories": ["Meat & Seafood"], "productPageURI": "/p/chicken/0001111000030",
    }
    raw_est = {
        "productId": "0001111000040", "upc": "0001111000040",
        "description": "Farmer John Ham Est. 1920",
        "items": [{"itemId": "40", "size": "16 oz", "price": {"regular": 4.99}}],
        "categories": ["Meat & Seafood"], "productPageURI": "/p/ham/0001111000040",
    }
    raw_random = {
        "productId": "0001111000050", "upc": "0001111000050",
        "description": "Pork Chops (Random Weight)",
        "items": [{"itemId": "50", "size": "random weight", "price": {"regular": 7.99}}],
        "categories": ["Meat & Seafood"], "productPageURI": "/p/pork/0001111000050",
    }

    client.search_products = lambda term, location_id, limit, start: ([raw_family, raw_est, raw_random], 3)

    records, stats, meta = collect_kroger_catalog(
        client=client,
        location=mock_location,
        categories_terms={"Meat & Seafood": ["chicken"]},
        target_count=5,
        proof_only=False,
        previous_records=[hist_multipack, hist_approx],
    )

    upcs = {r["upc"] for r in records}
    assert "0001111000020" not in upcs, "Historical approx-weight item was not excluded!"
    assert "0001111000050" not in upcs, "Fresh approx-weight item was not excluded!"

    assert "0001111000010" in upcs, "Fixed-weight multipack 2x16 oz was lost!"
    assert "0001111000030" in upcs, "Family pack 3 lb was lost!"
    assert "0001111000040" in upcs, "Foundation year Est. 1920 was lost!"


def test_multistore_selection_requires_explicit_location_id(tmp_path, monkeypatch, mock_location):
    store_a = dict(mock_location)
    store_a["store_id"] = "53100502"
    store_b = dict(mock_location)
    store_b["store_id"] = "53100501"
    store_b["store_location"] = "333 E Benton Pl, Chicago, IL 60601"
    store_b["zipcode"] = "60601"

    cfg_file = tmp_path / "settings_multi.json"
    cfg_file.write_text(json.dumps({"locations": [store_a, store_b]}), encoding="utf-8")

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    monkeypatch.setattr(kroger_collector, "DATA_DIR", data_dir)
    monkeypatch.setenv("KROGER_CLIENT_ID", "mock_id")
    monkeypatch.setenv("KROGER_CLIENT_SECRET", "mock_secret")

    # 1. Ambiguous invocation without --location-id must raise RuntimeError (no silent default)
    monkeypatch.setattr(sys, "argv", ["kroger_collector", "--config", str(cfg_file)])
    with pytest.raises(RuntimeError, match="Multiple stores configured"):
        kroger_collector.main()

    # 2. Specifying non-existent location ID must raise RuntimeError
    monkeypatch.setattr(sys, "argv", ["kroger_collector", "--config", str(cfg_file), "--location-id", "99999999"])
    with pytest.raises(RuntimeError, match="Configured locations do not contain location ID"):
        kroger_collector.main()


def test_same_upc_at_two_different_stores_is_distinct_and_isolated(tmp_path, monkeypatch, mock_location):
    store_1 = dict(mock_location)
    store_1["store_id"] = "53100502"
    store_1["store_location"] = "1720 N Milwaukee Ave, Vernon Hills, IL 60061"
    store_1["zipcode"] = "60061"

    store_2 = dict(mock_location)
    store_2["store_id"] = "53100501"
    store_2["store_location"] = "333 E Benton Pl, Chicago, IL 60601"
    store_2["zipcode"] = "60601"

    raw_p1 = {
        "productId": "0001111000099", "upc": "0001111000099",
        "description": "Store 1 Milk",
        "items": [{"itemId": "99", "size": "1 gal", "price": {"regular": 3.99}}],
        "categories": ["Dairy"],
    }
    raw_p2 = {
        "productId": "0001111000099", "upc": "0001111000099",
        "description": "Store 2 Milk",
        "items": [{"itemId": "99", "size": "1 gal", "price": {"regular": 4.49}}],
        "categories": ["Dairy"],
    }

    rec1 = transform_kroger_product(raw_p1, store_1, "Dairy", "Milk", "2026-11-01")
    rec2 = transform_kroger_product(raw_p2, store_2, "Dairy", "Milk", "2026-11-01")

    key1 = product_key(rec1)
    key2 = product_key(rec2)

    assert key1 != key2, "Store-scoped identity failed to differentiate the same UPC across different stores!"
    assert rec1["regular_price"] == "3.99"
    assert rec2["regular_price"] == "4.49"
    assert rec1["store_location"] != rec2["store_location"]


def test_fulfillment_tracking_and_evidence_generation(tmp_path, monkeypatch, mock_location):
    client = KrogerClient()
    loc_ev = {
        "verified": True, "target_location_id": "53100502", "returned_location_id": "53100502",
        "name": "Mariano's", "chain": "Mariano's",
        "address": {"addressLine1": "1720 N Milwaukee Ave", "city": "Vernon Hills", "state": "IL", "zipCode": "60061"}
    }
    client.verify_location = lambda **kwargs: loc_ev

    raw_instore = {
        "productId": "0001111000011", "upc": "0001111000011", "description": "In Store Item",
        "items": [{"itemId": "11", "size": "16 oz", "price": {"regular": 2.99}, "fulfillment": {"instore": True, "curbside": True, "delivery": False}}],
        "categories": ["Dairy"],
    }
    raw_curbside = {
        "productId": "0001111000022", "upc": "0001111000022", "description": "Curbside Item",
        "items": [{"itemId": "22", "size": "16 oz", "price": {"regular": 3.49}, "fulfillment": {"instore": False, "curbside": True, "delivery": True}}],
        "categories": ["Dairy"],
    }
    raw_unspecified = {
        "productId": "0001111000033", "upc": "0001111000033", "description": "No Fulfillment Item",
        "items": [{"itemId": "33", "size": "16 oz", "price": {"regular": 1.99}}],
        "categories": ["Dairy"],
    }

    client.search_products = lambda term, location_id, limit, start: ([raw_instore, raw_curbside, raw_unspecified], 3)

    evidence_dir = tmp_path / "evidence"
    cp_dir = tmp_path / "checkpoints"
    monkeypatch.setattr(kroger_collector, "EVIDENCE_DIR", evidence_dir)
    monkeypatch.setattr(kroger_collector, "CHECKPOINT_DIR", cp_dir)

    records, stats, meta = collect_kroger_catalog(
        client=client,
        location=mock_location,
        categories_terms={"Dairy": ["milk"]},
        target_count=3,
        proof_only=False,
    )

    f_sum = meta.get("fulfillment_summary", {})
    assert f_sum.get("instore_eligible_count") == 1
    assert f_sum.get("curbside_eligible_count") == 2
    assert f_sum.get("delivery_eligible_count") == 1
    assert f_sum.get("unspecified_count") == 1

    ev_file = evidence_dir / f"store_fulfillment_{mock_location['store_id']}.json"
    assert ev_file.exists(), "store_fulfillment evidence file was not written!"
    ev_data = json.loads(ev_file.read_text(encoding="utf-8"))
    assert ev_data["store_id"] == mock_location["store_id"]
    assert ev_data["fulfillment_summary"] == f_sum
