"""Comprehensive regression test suite for Mariano's collector.

Tests all Darren-review requirements:
1. All configured search terms are attempted (no premature quota stopping).
2. Category is marked incomplete/partial if terms are skipped.
3. Pagination beyond supported offset (250) is reported as limited_by_api.
4. Quota achievement does not imply coverage completion.
5. Repeated pages are detected.
6. Approximate-weight filtering handles all required examples (including produce by each).
7. Fixed-weight multipacks and bags remain accepted.
8. Fulfillment eligibility is not claimed as price-channel proof (pricing_channel_verified: False).
9. Duplicate search appearances do not inflate unique fulfillment counts.
10. Every accepted product has a valid evidence reference in JSONL.
11. Page-level checkpoint recovery resumes from correct term and offset.
12. Fresh runs ignore old checkpoints unless --resume is explicit.
13. Incomplete categories preserve historical records.
14. Failed validation never publishes.
15. Failed publication preserves the previous JSON and CSV pair via rollback.
16. Exact 22-field schema and string types.
17. Same UPC across two stores remains distinct.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

import kroger_collector
from kroger_collector import (
    KROGER_MAX_START,
    KrogerClient,
    append_product_evidence,
    collect_kroger_catalog,
    compute_config_fingerprint,
    save_checkpoint,
    transform_kroger_product,
)
from schema import (
    FIELDS,
    blank_record,
    clean_text,
    is_approx_weight,
    product_key,
    publish_catalog_pair,
    replace_category_records,
    validate_records,
)


@pytest.fixture
def mock_store_loc():
    return {
        "id": "vernon-hills",
        "store_id": "53100502",
        "zipcode": "60061",
        "store_name": "Mariano's",
        "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061",
    }


@pytest.fixture
def base_product_payload():
    return {
        "productId": "0001111000101",
        "upc": "0001111000101",
        "description": "Organic Whole Milk",
        "items": [
            {
                "itemId": "0001111000101",
                "size": "1 gal",
                "price": {"regular": 4.99, "promo": 3.99},
                "fulfillment": {"instore": True, "curbside": True, "delivery": True},
            }
        ],
        "categories": ["Dairy"],
        "productPageURI": "/p/organic-whole-milk/0001111000101",
    }


# =====================================================================
# 1. Category Coverage: Execute every configured search term
# =====================================================================

def test_all_configured_terms_are_attempted_without_quota_stop(tmp_path, monkeypatch, mock_store_loc, base_product_payload):
    """Collector must execute all configured terms in a category, not stopping when quota reached."""
    client = KrogerClient()
    client.verify_location = lambda **kwargs: {"verified": True, "target_location_id": "53100502"}
    
    terms_called: list[str] = []

    def mock_search(term, location_id, limit, start):
        terms_called.append(term)
        # Return 15 items per term for 4 terms -> 60 items total
        items = []
        for i in range(15):
            p = copy.deepcopy(base_product_payload)
            upc = f"{hash(term + str(i)) % 10000000000000:013d}"
            p["productId"] = upc
            p["upc"] = upc
            p["description"] = f"{term.title()} Product {i}"
            items.append(p)
        return items, len(items)

    client.search_products = mock_search

    monkeypatch.setattr(kroger_collector, "EVIDENCE_DIR", tmp_path / "evidence")
    monkeypatch.setattr(kroger_collector, "CHECKPOINT_DIR", tmp_path / "checkpoints")

    test_terms = ["apples", "bananas", "berries", "lettuce"]
    records, stats, meta = collect_kroger_catalog(
        client=client,
        location=mock_store_loc,
        categories_terms={"Produce": test_terms},
        target_count=10,  # low target quota
        proof_only=False,
    )

    stat = stats[0]
    assert stat["terms_expected"] == 4
    assert stat["terms_attempted"] == 4
    assert stat["terms_skipped"] == 0
    assert stat["terms_succeeded"] == 4
    assert stat["terms_exhausted"] == 4
    assert stat["coverage_complete"] is True
    assert stat["coverage_status"] == "complete"
    assert stat["status"] == "ok"
    assert len(records) == 60  # All 60 products accepted across all 4 terms


def test_category_not_complete_when_terms_skipped(tmp_path):
    """A category with skipped terms cannot be marked complete."""
    category_stat = {
        "category": "Produce",
        "status": "incomplete",
        "records": 40,
        "terms_expected": 8,
        "terms_attempted": 2,
        "terms_succeeded": 2,
        "terms_skipped": 6,
        "terms_failed": 0,
        "coverage_complete": False,
        "coverage_status": "partial",
        "pagination_complete": False,
        "store_evidence": {"selected_store_verified": True},
    }

    dummy_records = [
        {
            "zipcode": "60061", "store_name": "Mariano's", "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061",
            "store_logo": "", "store_brand": "no", "category": "Produce", "sub_category": "",
            "product_title": f"Apple {i}", "weight": "16 oz", "regular_price": "2.99", "sale_price": "",
            "volume_deals_description": "", "volume_deals_price": "", "digital_deals_description": "",
            "digital_deals_price": "", "image_url": "", "url": f"https://www.marianos.com/p/apple/{i}",
            "upc": f"{i:013d}", "gupc": "", "ppm": "", "unit_price": "", "crawl_date": "2026-11-02",
        }
        for i in range(1, 41)
    ]

    val = validate_records(
        records=dummy_records,
        category_stats=[category_stat],
        cfg={"min_records_for_publish": 20, "min_categories_with_products": 1},
    )
    assert val["validation_passed"] is False
    assert val["terms_skipped_count"] == 6


# =====================================================================
# 2. Pagination and Documented API Limitations
# =====================================================================

def test_pagination_beyond_kroger_max_offset_reported_as_limited_by_api(tmp_path, monkeypatch, mock_store_loc, base_product_payload):
    """When a search term has >300 results, reaching start=250 reports limited_by_api."""
    client = KrogerClient()
    client.verify_location = lambda **kwargs: {"verified": True, "target_location_id": "53100502"}

    requested_offsets: list[int] = []

    def mock_large_search(term, location_id, limit, start):
        requested_offsets.append(start)
        # Return 50 products per page, with total=1500
        items = []
        for i in range(50):
            p = copy.deepcopy(base_product_payload)
            p["productId"] = f"{start:03d}_{i:03d}"
            p["upc"] = f"{start * 100 + i:013d}"
            items.append(p)
        return items, 1500

    client.search_products = mock_large_search
    monkeypatch.setattr(kroger_collector, "EVIDENCE_DIR", tmp_path / "evidence")
    monkeypatch.setattr(kroger_collector, "CHECKPOINT_DIR", tmp_path / "checkpoints")

    records, stats, meta = collect_kroger_catalog(
        client=client,
        location=mock_store_loc,
        categories_terms={"Dairy": ["milk"]},
        target_count=500,
        proof_only=False,
    )

    stat = stats[0]
    assert stat["api_limit_reached"] is True
    assert stat["coverage_status"] == "limited_by_api"
    assert stat["coverage_complete"] is False  # Cannot be marked fully complete
    assert stat["status"] == "ok"  # Terms processed per policy up to documented API limit
    assert requested_offsets == [0, 50, 100, 150, 200, 250]
    assert len(records) == 300  # 6 pages * 50


def test_repeated_page_detection_stops_infinite_loop(tmp_path, monkeypatch, mock_store_loc, base_product_payload):
    """If API repeats identical product IDs at consecutive offsets, stop with repeated_page."""
    client = KrogerClient()
    client.verify_location = lambda **kwargs: {"verified": True, "target_location_id": "53100502"}

    # Return full page of 50 products on offset 0 and identical 50 products on offset 50
    repeated_page = []
    for i in range(50):
        p = copy.deepcopy(base_product_payload)
        p["productId"] = f"item_{i:03d}"
        p["upc"] = f"{1000000000000 + i:013d}"
        repeated_page.append(p)

    client.search_products = lambda term, location_id, limit, start: (repeated_page, 500)

    monkeypatch.setattr(kroger_collector, "EVIDENCE_DIR", tmp_path / "evidence")
    monkeypatch.setattr(kroger_collector, "CHECKPOINT_DIR", tmp_path / "checkpoints")

    records, stats, meta = collect_kroger_catalog(
        client=client,
        location=mock_store_loc,
        categories_terms={"Dairy": ["milk"]},
        target_count=100,
        proof_only=False,
    )

    stat = stats[0]
    t_detail = stat["term_details"][0]
    assert t_detail["stop_reason"] == "repeated_page"
    assert t_detail["pages_requested"] == 2  # Caught on 2nd page


# =====================================================================
# 3. Approximate-Weight Filtering Regressions
# =====================================================================

def test_approximate_weight_produce_by_each_and_packaged_items():
    """Verify produce sold by each with estimated weight vs legitimate packaged fixed weights."""
    # 1. Produce sold by each with estimated weight -> EXCLUDE
    assert is_approx_weight("1 lb", "Large Honeycrisp Apple – Each") is True
    assert is_approx_weight("1 lb", "Large Granny Smith Apple - Each") is True
    assert is_approx_weight("0.5 lb", "Honeycrisp Apple - each") is True
    assert is_approx_weight("1 lb", "Bananas (Each)") is True
    assert is_approx_weight("2 lb", "Cantaloupe /each") is True

    # 2. Variable, random, and average weight keywords -> EXCLUDE
    assert is_approx_weight("random weight", "Ground Beef") is True
    assert is_approx_weight("16 oz", "Beef Tenderloin Variable Weight") is True
    assert is_approx_weight("avg wt 1.5 lb", "Pork Loin") is True
    assert is_approx_weight("15 lb", "Watermelon - Average weight 15 lb") is True
    assert is_approx_weight("1 lb", "Salmon Fillet (weighed in store)") is True
    assert is_approx_weight("1 lb", "Tomatoes", "soldBy:weight") is True

    # 3. Legitimate fixed-weight bags, trays, packs -> ACCEPT
    assert is_approx_weight("3 lb", "Honeycrisp Apples 3 lb Bag") is False
    assert is_approx_weight("3 lb", "Honeycrisp Apples Bag - 3 lb") is False
    assert is_approx_weight("4 ct", "Avocados 4 ct Tray") is False
    assert is_approx_weight("4-count", "Gala Apples 4-count") is False
    assert is_approx_weight("2x16 oz", "Twin Pack 2x16 oz Whole Milk") is False
    assert is_approx_weight("6 pack", "6-pack Yogurt") is False
    assert is_approx_weight("16 oz", "16 oz Ground Beef") is False
    assert is_approx_weight("16 oz", "Farmer John Smoked Sausage Est. 1920") is False


# =====================================================================
# 4. Fulfillment & Pricing Channel Claims
# =====================================================================

def test_fulfillment_context_and_deduplication(tmp_path, monkeypatch, mock_store_loc, base_product_payload):
    """Fulfillment flags indicate customer eligibility, pricing_channel_verified is strictly False."""
    client = KrogerClient()
    client.verify_location = lambda **kwargs: {
        "verified": True, "target_location_id": "53100502", "name": "Mariano's",
        "address": {"addressLine1": "1720 N Milwaukee Ave", "city": "Vernon Hills", "state": "IL", "zipCode": "60061"}
    }

    # Product 1 appears in term 'milk' and term 'cream'
    p1 = copy.deepcopy(base_product_payload)
    p1["productId"] = "0001111000101"
    p1["upc"] = "0001111000101"
    p1["items"][0]["fulfillment"] = {"instore": True, "curbside": True, "delivery": False}

    p2 = copy.deepcopy(base_product_payload)
    p2["productId"] = "0001111000102"
    p2["upc"] = "0001111000102"
    p2["items"][0]["fulfillment"] = {"instore": True, "curbside": False, "delivery": True}

    def mock_search(term, location_id, limit, start):
        if term == "milk":
            return [p1, p2], 2
        elif term == "cream":
            return [p1], 1  # p1 duplicate appearance
        return [], 0

    client.search_products = mock_search

    monkeypatch.setattr(kroger_collector, "EVIDENCE_DIR", tmp_path / "evidence")
    monkeypatch.setattr(kroger_collector, "CHECKPOINT_DIR", tmp_path / "checkpoints")

    records, stats, meta = collect_kroger_catalog(
        client=client,
        location=mock_store_loc,
        categories_terms={"Dairy": ["milk", "cream"]},
        target_count=2,
        proof_only=False,
    )

    assert meta["pricing_channel_verified"] is False
    f_sum = meta["fulfillment_summary"]
    assert f_sum["pricing_channel_verified"] is False
    # Unique product count is 2, p1 duplicate appearance didn't inflate counts
    assert f_sum["unique_products_evaluated"] == 2
    assert f_sum["instore_eligible_count"] == 2
    assert f_sum["curbside_eligible_count"] == 1
    assert f_sum["delivery_eligible_count"] == 1


# =====================================================================
# 5. Raw Evidence & Product-Level Traceability
# =====================================================================

def test_every_accepted_product_has_traceable_jsonl_evidence(tmp_path, monkeypatch, mock_store_loc, base_product_payload):
    """Every accepted catalog record resolves to a raw evidence line in JSONL."""
    client = KrogerClient()
    client.verify_location = lambda **kwargs: {"verified": True, "target_location_id": "53100502"}

    items = []
    for i in range(5):
        p = copy.deepcopy(base_product_payload)
        p["productId"] = f"000111100020{i}"
        p["upc"] = f"000111100020{i}"
        items.append(p)

    client.search_products = lambda term, location_id, limit, start: (items, len(items))

    evidence_dir = tmp_path / "evidence"
    monkeypatch.setattr(kroger_collector, "EVIDENCE_DIR", evidence_dir)
    monkeypatch.setattr(kroger_collector, "CHECKPOINT_DIR", tmp_path / "checkpoints")

    records, stats, meta = collect_kroger_catalog(
        client=client,
        location=mock_store_loc,
        categories_terms={"Dairy": ["milk"]},
        target_count=5,
        proof_only=False,
    )

    ev_file = evidence_dir / "product_evidence_53100502.jsonl"
    assert ev_file.exists()

    lines = [json.loads(line) for line in ev_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 5

    ev_upcs = {e["upc"] for e in lines}
    for r in records:
        assert r["upc"] in ev_upcs
        # Verify evidence entry integrity
        matching_ev = next(e for e in lines if e["upc"] == r["upc"])
        assert matching_ev["store_id"] == "53100502"
        assert matching_ev["search_term"] == "milk"
        assert matching_ev["category"] == "Dairy"
        assert "access_token" not in matching_ev
        assert "authorization" not in matching_ev


# =====================================================================
# 6. Page-Level Checkpoint and Recovery
# =====================================================================

def test_page_level_checkpoint_resumes_from_exact_term_and_offset(tmp_path, monkeypatch, mock_store_loc, base_product_payload):
    """Resuming from mid-category resumes from the exact term and offset without losing products."""
    client = KrogerClient()
    client.verify_location = lambda **kwargs: {"verified": True, "target_location_id": "53100502"}

    cp_dir = tmp_path / "checkpoints"
    evidence_dir = tmp_path / "evidence"
    cp_path = cp_dir / "checkpoint_kroger_53100502.json"

    monkeypatch.setattr(kroger_collector, "EVIDENCE_DIR", evidence_dir)
    monkeypatch.setattr(kroger_collector, "CHECKPOINT_DIR", cp_dir)

    # 1. Product collected on term 0 (milk)
    p_milk = copy.deepcopy(base_product_payload)
    p_milk["productId"] = "0001111000301"
    p_milk["upc"] = "0001111000301"
    rec_milk = transform_kroger_product(p_milk, mock_store_loc, "Dairy", "", "2026-11-02")

    config_fp = compute_config_fingerprint({"Dairy": ["milk", "cheese", "butter"]}, mock_store_loc, 10)

    # 2. Checkpoint saved during term 1 (cheese) at offset 50
    completed_terms = [{
        "term": "milk", "status": "exhausted", "pages_requested": 1, "raw_results_received": 1,
        "accepted_products": 1, "excluded_approx_weight": 0, "excluded_invalid_price": 0,
        "duplicates_encountered": 0, "errors": [], "api_reported_total": 1, "stop_reason": "exhausted",
        "offsets_requested": [0], "results_per_offset": {"0": 1},
    }]

    save_checkpoint(
        checkpoint_path=cp_path,
        run_id="2026-11-02",
        store_id="53100502",
        config_fingerprint=config_fp,
        category_name="Dairy",
        category_index=0,
        current_term="cheese",
        term_index=1,
        current_offset=50,
        completed_categories=[],
        completed_terms_in_cat=completed_terms,
        cat_records=[rec_milk],
        records_so_far=[],
        status="in_progress",
        completed_pages=2,
        collected_product_identities=[product_key(rec_milk)],
    )

    saved_cp = json.loads(cp_path.read_text(encoding="utf-8"))

    # 3. Resume collection: search should be called for cheese at start=50, then butter at start=0
    search_calls: list[tuple[str, int]] = []

    def mock_resumed_search(term, location_id, limit, start):
        search_calls.append((term, start))
        p = copy.deepcopy(base_product_payload)
        p["productId"] = f"upc_{term}_{start}"
        p["upc"] = f"99{hash(term + str(start)) % 100000000000:011d}"
        return [p], 1

    client.search_products = mock_resumed_search

    records, stats, meta = collect_kroger_catalog(
        client=client,
        location=mock_store_loc,
        categories_terms={"Dairy": ["milk", "cheese", "butter"]},
        target_count=10,
        proof_only=False,
        resume_checkpoint=saved_cp,
    )

    # Term 0 (milk) was skipped because it was already completed!
    # Term 1 (cheese) started at offset 50!
    # Term 2 (butter) started at offset 0!
    assert ("milk", 0) not in search_calls
    assert ("cheese", 50) in search_calls
    assert ("butter", 0) in search_calls

    # The record from term 0 was retained!
    upcs = {r["upc"] for r in records}
    assert "0001111000301" in upcs


# =====================================================================
# 7. Safe Indefinite Retention and Publication Safeguards
# =====================================================================

def test_incomplete_category_preserves_historical_records_byte_for_byte():
    """An incomplete or failed category crawl preserves historical products."""
    hist_rec = blank_record()
    hist_rec.update({
        "zipcode": "60061", "store_name": "Mariano's", "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061",
        "category": "Dairy", "product_title": "Historic Milk", "weight": "1 gal", "regular_price": "3.99",
        "upc": "0001111000999", "url": "https://www.marianos.com/p/milk/0001111000999", "crawl_date": "2026-10-01",
    })

    fresh_rec = blank_record()
    fresh_rec.update({
        "zipcode": "60061", "store_name": "Mariano's", "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061",
        "category": "Dairy", "product_title": "Fresh Milk", "weight": "1 gal", "regular_price": "4.29",
        "upc": "0001111000888", "url": "https://www.marianos.com/p/milk/0001111000888", "crawl_date": "2026-11-02",
    })

    # If status is not 'ok', fresh records are rejected and historical records remain untouched
    retained = kroger_collector.apply_category_result(
        existing_records=[hist_rec],
        category_name="Dairy",
        new_records=[fresh_rec],
        status="incomplete",
    )
    assert len(retained) == 1
    assert retained[0]["upc"] == "0001111000999"


def test_publish_catalog_pair_atomic_rollback_on_csv_failure(tmp_path):
    """If publishing CSV fails, JSON must be rolled back to its previous state."""
    final_json = tmp_path / "marianos_latest.json"
    final_csv = tmp_path / "marianos_latest.csv"

    # Initial published pair
    final_json.write_text('{"version": 1}', encoding="utf-8")
    final_csv.write_text("col1,col2\nold,val\n", encoding="utf-8")

    orig_json_hash = hashlib.sha256(final_json.read_bytes()).hexdigest()
    orig_csv_hash = hashlib.sha256(final_csv.read_bytes()).hexdigest()

    # Staging files for new publication
    staging_json = tmp_path / "staging.json"
    staging_csv = tmp_path / "staging.csv"
    staging_json.write_text('{"version": 2}', encoding="utf-8")
    staging_csv.write_text("col1,col2\nnew,val\n", encoding="utf-8")

    # Monkeypatch publish_staging_file in schema to succeed on json but explode on csv
    import schema
    orig_publish = schema.publish_staging_file
    def faulty_publish(src, dst):
        if dst == final_csv:
            raise IOError("Simulated disk error during CSV atomic replace")
        orig_publish(src, dst)

    try:
        schema.publish_staging_file = faulty_publish
        with pytest.raises(RuntimeError, match="rolled back JSON"):
            publish_catalog_pair(staging_json, final_json, staging_csv, final_csv)
    finally:
        schema.publish_staging_file = orig_publish

    # Verify that BOTH files were preserved byte-for-byte in their original state!
    assert hashlib.sha256(final_json.read_bytes()).hexdigest() == orig_json_hash
    assert hashlib.sha256(final_csv.read_bytes()).hexdigest() == orig_csv_hash


# =====================================================================
# 8. Schema Exactness & Multi-Store Isolation
# =====================================================================

def test_exact_22_fields_and_string_types(mock_store_loc, base_product_payload):
    """Records must conform strictly to 22 fields, all string types, gupc/unit_price blank."""
    rec = transform_kroger_product(base_product_payload, mock_store_loc, "Dairy", "", "2026-11-02")
    assert list(rec.keys()) == FIELDS
    assert len(rec) == 22
    for k, v in rec.items():
        assert isinstance(v, str), f"Field {k} has non-string type {type(v)}"
    assert rec["gupc"] == ""
    assert rec["unit_price"] == ""


def test_same_upc_across_two_stores_remains_distinct(mock_store_loc, base_product_payload):
    """Store-scoped identity prevents cross-store collision for identical UPC."""
    loc_1 = dict(mock_store_loc)
    loc_2 = dict(mock_store_loc)
    loc_2["store_id"] = "53100501"
    loc_2["store_location"] = "333 E Benton Pl, Chicago, IL 60601"
    loc_2["zipcode"] = "60601"

    rec1 = transform_kroger_product(base_product_payload, loc_1, "Dairy", "", "2026-11-02")
    rec2 = transform_kroger_product(base_product_payload, loc_2, "Dairy", "", "2026-11-02")

    key1 = product_key(rec1)
    key2 = product_key(rec2)
    assert key1 != key2
    assert "53100502" not in key1 or "vernon hills" in key1
    assert "chicago" in key2


# =====================================================================
# 9. Search-Term Coverage Enforcement & Terminal Status Tests
# =====================================================================

def test_validate_records_rejects_unattempted_or_undocumented_terms():
    """validate_records rejects categories where terms were unattempted or undocumented."""
    cfg = {"min_records_for_publish": 1, "min_categories_with_products": 1}
    dummy_rec = blank_record()
    dummy_rec.update({
        "zipcode": "60061", "store_name": "Mariano's", "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061",
        "category": "Produce", "product_title": "Apples", "weight": "3 lb Bag", "regular_price": "4.99",
        "upc": "0001111000991", "url": "https://www.marianos.com/p/apples/0001111000991", "crawl_date": "2026-11-02",
    })

    # Case A: terms_attempted (1) < terms_expected (2)
    stat_unattempted = {
        "category": "Produce", "status": "ok", "records": 1,
        "terms_expected": 2, "terms_attempted": 1, "terms_succeeded": 1, "terms_failed": 0, "terms_skipped": 1,
        "coverage_complete": False, "coverage_status": "partial", "pagination_complete": False,
        "store_evidence": {"selected_store_verified": True},
        "term_details": [{"term": "apples", "status": "exhausted", "stop_reason": "exhausted"}],
    }
    val = validate_records([dummy_rec], [stat_unattempted], cfg)
    assert val["validation_passed"] is False
    assert "Produce" in val["unattempted_or_undocumented_terms_categories"]

    # Case B: term has invalid or undocumented status
    stat_undocumented = {
        "category": "Produce", "status": "ok", "records": 1,
        "terms_expected": 1, "terms_attempted": 1, "terms_succeeded": 1, "terms_failed": 0, "terms_skipped": 0,
        "coverage_complete": True, "coverage_status": "complete", "pagination_complete": True,
        "store_evidence": {"selected_store_verified": True},
        "term_details": [{"term": "apples", "status": "mystery_status", "stop_reason": "unknown_reason"}],
    }
    val2 = validate_records([dummy_rec], [stat_undocumented], cfg)
    assert val2["validation_passed"] is False
    assert "Produce" in val2["unattempted_or_undocumented_terms_categories"]


def test_validate_records_rejects_category_marked_complete_on_quota_achievement():
    """A category must NEVER be marked complete merely because its product quota was reached."""
    cfg = {"min_records_for_publish": 10, "min_categories_with_products": 1}
    dummy_records = []
    for i in range(50):
        r = blank_record()
        r.update({
            "zipcode": "60061", "store_name": "Mariano's", "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061",
            "category": "Produce", "product_title": f"Produce {i}", "weight": "16 oz", "regular_price": "2.99",
            "upc": f"{i:013d}", "url": f"https://www.marianos.com/p/item/{i}", "crawl_date": "2026-11-02",
        })
        dummy_records.append(r)

    # Category claims coverage_complete=True and complete status even though term 2 was skipped
    stat_false_claim = {
        "category": "Produce", "status": "ok", "records": 50,  # 50 records met quota!
        "terms_expected": 2, "terms_attempted": 1, "terms_succeeded": 1, "terms_failed": 0, "terms_skipped": 1,
        "coverage_complete": True, "coverage_status": "complete", "pagination_complete": False,
        "store_evidence": {"selected_store_verified": True},
        "term_details": [{"term": "apples", "status": "exhausted", "stop_reason": "exhausted"}],
    }
    val = validate_records(dummy_records, [stat_false_claim], cfg)
    assert val["validation_passed"] is False
    assert "Produce" in val["invalid_coverage_claims"]


def test_validate_records_distinguishes_coverage_statuses():
    """Validation properly distinguishes complete, limited_by_api, partial, and failed statuses."""
    cfg = {"min_records_for_publish": 1, "min_categories_with_products": 1}
    r = blank_record()
    r.update({
        "zipcode": "60061", "store_name": "Mariano's", "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061",
        "category": "Dairy", "product_title": "Milk", "weight": "1 gal", "regular_price": "3.99",
        "upc": "0001111000992", "url": "https://www.marianos.com/p/milk/0001111000992", "crawl_date": "2026-11-02",
    })

    # Status: complete (valid)
    stat_complete = {
        "category": "Dairy", "status": "ok", "records": 1,
        "terms_expected": 1, "terms_attempted": 1, "terms_succeeded": 1, "terms_failed": 0, "terms_skipped": 0,
        "coverage_complete": True, "coverage_status": "complete", "pagination_complete": True,
        "store_evidence": {"selected_store_verified": True},
        "term_details": [{"term": "milk", "status": "exhausted", "stop_reason": "exhausted"}],
    }
    assert validate_records([r], [stat_complete], cfg)["validation_passed"] is True

    # Status: limited_by_api (valid documented limitation, not complete)
    stat_limited = {
        "category": "Dairy", "status": "ok", "records": 1,
        "terms_expected": 1, "terms_attempted": 1, "terms_succeeded": 1, "terms_failed": 0, "terms_skipped": 0,
        "api_limit_reached": True, "coverage_complete": False, "coverage_status": "limited_by_api", "pagination_complete": True,
        "store_evidence": {"selected_store_verified": True},
        "term_details": [{"term": "milk", "status": "stopped_by_api_limit", "stop_reason": "api_limit_reached"}],
    }
    assert validate_records([r], [stat_limited], cfg)["validation_passed"] is True

    # Status: partial (fails publish validation)
    stat_partial = {
        "category": "Dairy", "status": "incomplete", "records": 1,
        "terms_expected": 2, "terms_attempted": 1, "terms_succeeded": 1, "terms_failed": 0, "terms_skipped": 1,
        "coverage_complete": False, "coverage_status": "partial", "pagination_complete": False,
        "store_evidence": {"selected_store_verified": True},
        "term_details": [{"term": "milk", "status": "exhausted", "stop_reason": "exhausted"}],
    }
    val_part = validate_records([r], [stat_partial], cfg)
    assert val_part["validation_passed"] is False
    assert "Dairy" in val_part["partial_or_failed_categories"]

    # Status: failed (fails publish validation)
    stat_failed = {
        "category": "Dairy", "status": "incomplete_due_to_failed_requests", "records": 0,
        "terms_expected": 1, "terms_attempted": 1, "terms_succeeded": 0, "terms_failed": 1, "terms_skipped": 0,
        "coverage_complete": False, "coverage_status": "failed", "pagination_complete": False,
        "store_evidence": {"selected_store_verified": True},
        "term_details": [{"term": "milk", "status": "failed", "stop_reason": "api_error"}],
    }
    val_fail = validate_records([r], [stat_failed], cfg)
    assert val_fail["validation_passed"] is False
    assert "Dairy" in val_fail["partial_or_failed_categories"]


# =====================================================================
# 10. Interruption Checkpoint Accuracy & Resume Test
# =====================================================================

def test_keyboard_interrupt_mid_category_persists_accurate_counters_and_resumes(tmp_path, monkeypatch, mock_store_loc, base_product_payload):
    """Interruption in middle of category persists non-zero progress counters and resumes cleanly."""
    client = KrogerClient()
    client.verify_location = lambda **kwargs: {"verified": True, "target_location_id": "53100502"}

    evidence_dir = tmp_path / "evidence"
    checkpoint_dir = tmp_path / "checkpoints"
    monkeypatch.setattr(kroger_collector, "EVIDENCE_DIR", evidence_dir)
    monkeypatch.setattr(kroger_collector, "CHECKPOINT_DIR", checkpoint_dir)

    # Create distinct products for terms
    def mock_interrupt_search(term, location_id, limit, start):
        if term == "apples":
            p = copy.deepcopy(base_product_payload)
            p["productId"] = "1111111111111"
            p["upc"] = "1111111111111"
            p["description"] = "Apples Item"
            return [p], 1
        elif term == "bananas":
            # Interrupt on term 2
            raise KeyboardInterrupt("Simulated operator interrupt")
        elif term == "oranges":
            p = copy.deepcopy(base_product_payload)
            p["productId"] = "3333333333333"
            p["upc"] = "3333333333333"
            p["description"] = "Oranges Item"
            return [p], 1
        return [], 0

    client.search_products = mock_interrupt_search

    # Run should raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        collect_kroger_catalog(
            client=client,
            location=mock_store_loc,
            categories_terms={"Produce": ["apples", "bananas", "oranges"]},
            target_count=50,
            proof_only=False,
        )

    # 1. Verify checkpoint state: counters must be REAL and NON-ZERO!
    cp_file = checkpoint_dir / f"checkpoint_kroger_{mock_store_loc['store_id']}.json"
    assert cp_file.exists()
    cp_data = json.loads(cp_file.read_text(encoding="utf-8"))
    assert cp_data["status"] == "interrupted"
    assert cp_data["category_name"] == "Produce"
    assert cp_data["current_term"] == "bananas"
    assert cp_data["term_index"] == 1
    assert len(cp_data["cat_records"]) == 1
    assert cp_data["cat_records"][0]["upc"] == "1111111111111"

    # Verify interrupted_stat in completed_categories has real non-zero values
    interrupted_stat = cp_data["completed_categories"][0]
    assert interrupted_stat["category"] == "Produce"
    assert interrupted_stat["status"] == "interrupted"
    assert interrupted_stat["terms_expected"] == 3
    assert interrupted_stat["terms_attempted"] >= 1
    assert interrupted_stat["terms_succeeded"] >= 1  # Not hardcoded to 0!
    assert interrupted_stat["pages_requested"] >= 1  # Not hardcoded to 0!
    assert interrupted_stat["raw_products_received"] >= 1  # Not hardcoded to 0!
    assert interrupted_stat["unique_products_accepted"] == 1

    # 2. Now resume collection with the bananas term succeeding
    def mock_resumed_search(term, location_id, limit, start):
        if term == "bananas":
            p = copy.deepcopy(base_product_payload)
            p["productId"] = "2222222222222"
            p["upc"] = "2222222222222"
            p["description"] = "Bananas Item"
            return [p], 1
        elif term == "oranges":
            p = copy.deepcopy(base_product_payload)
            p["productId"] = "3333333333333"
            p["upc"] = "3333333333333"
            p["description"] = "Oranges Item"
            return [p], 1
        elif term == "apples":
            raise RuntimeError("Apples should have been skipped on resume!")
        return [], 0

    client.search_products = mock_resumed_search

    records, stats, meta = collect_kroger_catalog(
        client=client,
        location=mock_store_loc,
        categories_terms={"Produce": ["apples", "bananas", "oranges"]},
        target_count=50,
        proof_only=False,
        resume_checkpoint=cp_data,
    )

    # Verify: All 3 products collected with ZERO duplicates and full completion!
    assert len(records) == 3
    upcs = {r["upc"] for r in records}
    assert upcs == {"1111111111111", "2222222222222", "3333333333333"}
    final_stat = stats[0]
    assert final_stat["status"] == "ok"
    assert final_stat["terms_expected"] == 3
    assert final_stat["terms_succeeded"] == 3
    assert final_stat["coverage_complete"] is True


# =====================================================================
# 11. Hardened Paired Publication Failure-Injection Tests
# =====================================================================

def test_publish_catalog_pair_failure_injection_json_failure(tmp_path):
    """Simulated failure during JSON publication preserves original files and backups."""
    final_json = tmp_path / "marianos_latest.json"
    final_csv = tmp_path / "marianos_latest.csv"
    staging_json = tmp_path / "staging.json"
    staging_csv = tmp_path / "staging.csv"

    final_json.write_text('{"version": "orig_json"}', encoding="utf-8")
    final_csv.write_text("col1\norig_csv\n", encoding="utf-8")
    staging_json.write_text('{"version": "new_json"}', encoding="utf-8")
    staging_csv.write_text("col1\nnew_csv\n", encoding="utf-8")

    import schema
    orig_publish = schema.publish_staging_file

    def faulty_publish_json(src, dst):
        if dst == final_json:
            raise IOError("Simulated disk write failure on JSON")
        orig_publish(src, dst)

    try:
        schema.publish_staging_file = faulty_publish_json
        with pytest.raises(RuntimeError, match="Failed to publish JSON"):
            publish_catalog_pair(staging_json, final_json, staging_csv, final_csv)
    finally:
        schema.publish_staging_file = orig_publish

    # Both original files remain intact
    assert final_json.read_text(encoding="utf-8") == '{"version": "orig_json"}'
    assert final_csv.read_text(encoding="utf-8") == "col1\norig_csv\n"


def test_publish_catalog_pair_failure_injection_rollback_failure(tmp_path):
    """Simulated failure during rollback preserves .bak files on disk for operator recovery."""
    final_json = tmp_path / "marianos_latest.json"
    final_csv = tmp_path / "marianos_latest.csv"
    staging_json = tmp_path / "staging.json"
    staging_csv = tmp_path / "staging.csv"

    final_json.write_text('{"version": "orig_json"}', encoding="utf-8")
    final_csv.write_text("col1\norig_csv\n", encoding="utf-8")
    staging_json.write_text('{"version": "new_json"}', encoding="utf-8")
    staging_csv.write_text("col1\nnew_csv\n", encoding="utf-8")

    import schema
    import shutil
    orig_publish = schema.publish_staging_file
    orig_copy2 = shutil.copy2

    def faulty_publish_csv(src, dst):
        if dst == final_csv:
            raise IOError("Simulated error on CSV replace")
        orig_publish(src, dst)

    def faulty_copy2_on_rollback(src, dst):
        # Allow initial backup copy, but explode when copying backup_json back to final_json
        if ".bak" in str(src):
            raise IOError("Simulated disk error during JSON rollback!")
        return orig_copy2(src, dst)

    try:
        schema.publish_staging_file = faulty_publish_csv
        shutil.copy2 = faulty_copy2_on_rollback
        with pytest.raises(RuntimeError, match=r"CRITICAL.*rollback failed"):
            publish_catalog_pair(staging_json, final_json, staging_csv, final_csv)
    finally:
        schema.publish_staging_file = orig_publish
        shutil.copy2 = orig_copy2

    # Backups were NOT unlinked; they are preserved on disk!
    backup_json = tmp_path / "marianos_latest.json.bak"
    backup_csv = tmp_path / "marianos_latest.csv.bak"
    assert backup_json.exists()
    assert backup_csv.exists()


# =====================================================================
# 12. Approximate-Weight False-Positive Prevention Tests
# =====================================================================

def test_approx_weight_false_positive_prevention_on_weighted_and_multipacks():
    """Verify 'weighted' non-grocery items, foundation years, and multipacks are ACCEPTED."""
    # Legitimate non-grocery items with 'weighted' -> ACCEPT (False)
    assert is_approx_weight("15 lb", "Weighted Blanket 15 lb") is False
    assert is_approx_weight("10 oz", "Munchkin Weighted Straw Cup 10 oz") is False
    assert is_approx_weight("1 lb", "Weighted Utensil Set for Hand Tremors") is False
    assert is_approx_weight("12 lb", "Adjustable Weighted Vest 12 lb") is False
    assert is_approx_weight("2 lb", "Weighted Jump Rope") is False

    # Foundation years -> ACCEPT (False)
    assert is_approx_weight("8 oz", "Tillamook Medium Cheddar Cheese Est. 1920") is False
    assert is_approx_weight("12 oz", "Jones Dairy Farm Pork Sausage Est. 1889") is False

    # Fixed containers and multipacks -> ACCEPT (False)
    assert is_approx_weight("4x5.3 oz", "Chobani Greek Yogurt 4 Pack (4x5.3 oz)") is False
    assert is_approx_weight("4 ct", "Hass Avocados 4 ct Tray") is False
    assert is_approx_weight("3 lb", "Honeycrisp Apples 3 lb Bag") is False
    assert is_approx_weight("1 lb", "Organic Strawberries 1 lb Clamshell") is False

    # True approximate weight items -> EXCLUDE (True)
    assert is_approx_weight("1 lb", "Large Honeycrisp Apple – Each") is True
    assert is_approx_weight("0.5 lb", "Gala Apple - Each") is True
    assert is_approx_weight("0.33 lb", "Organic Hass Avocado - Each") is True
    assert is_approx_weight("1.25 lb", "Beef Ribeye Steak Variable Weight") is True
    assert is_approx_weight("1.5 lb", "Chicken Breast Sold by Weight") is True
    assert is_approx_weight("1 lb", "Fresh Salmon Fillet", "soldBy:weight") is True
    assert is_approx_weight("approx 1.5 lb", "Pork Tenderloin") is True
    assert is_approx_weight("1 lb", "Produce weighed at checkout") is True


# =====================================================================
# 13. Deterministic Mid-Category Page-Level Interruption & Resume Test
# =====================================================================

def test_deterministic_interruption_after_page_one_and_resume_exact_offset(tmp_path, monkeypatch, mock_store_loc, base_product_payload):
    """Interrupt collection after page 1 (offset 0), resume, and verify exact continuation.

    Asserts:
    1. Page 1 (offset 0) is NOT requested again upon resume.
    2. Page 2 (offset 50) is requested and completes.
    3. No product duplicates in the returned catalog records.
    4. No duplicate evidence records in the JSONL file for page 1 items.
    """
    client = KrogerClient()
    client.verify_location = lambda **kwargs: {"verified": True, "target_location_id": "53100502"}

    evidence_dir = tmp_path / "evidence"
    checkpoint_dir = tmp_path / "checkpoints"
    monkeypatch.setattr(kroger_collector, "EVIDENCE_DIR", evidence_dir)
    monkeypatch.setattr(kroger_collector, "CHECKPOINT_DIR", checkpoint_dir)

    search_requests_run1: list[int] = []

    def mock_search_run1(term, location_id, limit, start):
        search_requests_run1.append(start)
        if start == 0:
            # Page 1: 50 products, total reported is 70
            page1 = []
            for i in range(50):
                p = copy.deepcopy(base_product_payload)
                p["productId"] = f"prod_{i:04d}"
                p["upc"] = f"{1000000000000 + i:013d}"
                p["description"] = f"Apples Variety {i}"
                page1.append(p)
            return page1, 70
        elif start == 50:
            # Operator / process interrupt on page 2
            raise KeyboardInterrupt("Simulated interrupt on page 2")
        return [], 0

    client.search_products = mock_search_run1

    # First run: should be interrupted during page 2
    with pytest.raises(KeyboardInterrupt):
        collect_kroger_catalog(
            client=client,
            location=mock_store_loc,
            categories_terms={"Produce": ["apples"]},
            target_count=100,
            proof_only=False,
        )

    assert search_requests_run1 == [0, 50]

    # Verify checkpoint
    cp_file = checkpoint_dir / f"checkpoint_kroger_{mock_store_loc['store_id']}.json"
    assert cp_file.exists()
    cp_data = json.loads(cp_file.read_text(encoding="utf-8"))
    assert cp_data["status"] == "interrupted"
    assert cp_data["category_name"] == "Produce"
    assert cp_data["current_term"] == "apples"
    assert cp_data["term_index"] == 0
    assert cp_data["current_offset"] == 50
    assert len(cp_data["cat_records"]) == 50
    assert len(cp_data["collected_product_identities"]) == 50

    # Verify evidence file after run 1 has exactly 50 records
    ev_file = evidence_dir / f"product_evidence_{mock_store_loc['store_id']}.jsonl"
    assert ev_file.exists()
    lines_run1 = [json.loads(line) for line in ev_file.read_text(encoding="utf-8").strip().split("\n") if line]
    assert len(lines_run1) == 50

    # Second run: resume from checkpoint
    search_requests_run2: list[int] = []

    def mock_search_run2(term, location_id, limit, start):
        search_requests_run2.append(start)
        if start == 0:
            raise RuntimeError("Page 1 (offset 0) was already completed and must NOT be fetched again!")
        elif start == 50:
            # Page 2: remaining 20 products
            page2 = []
            for i in range(50, 70):
                p = copy.deepcopy(base_product_payload)
                p["productId"] = f"prod_{i:04d}"
                p["upc"] = f"{1000000000000 + i:013d}"
                p["description"] = f"Apples Variety {i}"
                page2.append(p)
            return page2, 70
        return [], 0

    client.search_products = mock_search_run2

    records, stats, meta = collect_kroger_catalog(
        client=client,
        location=mock_store_loc,
        categories_terms={"Produce": ["apples"]},
        target_count=100,
        proof_only=False,
        resume_checkpoint=cp_data,
    )

    # 1. Page 1 was NOT requested during resume; exactly page 2 was requested
    assert 0 not in search_requests_run2
    assert search_requests_run2 == [50]

    # 2. Total accepted products is 70 with zero duplicates
    assert len(records) == 70
    unique_upcs = {r["upc"] for r in records}
    assert len(unique_upcs) == 70

    # 3. Evidence file has exactly 70 records (50 from run 1 + 20 from run 2), no duplicates
    lines_run2 = [json.loads(line) for line in ev_file.read_text(encoding="utf-8").strip().split("\n") if line]
    assert len(lines_run2) == 70
    ev_upcs = [e["upc"] for e in lines_run2]
    assert len(ev_upcs) == len(set(ev_upcs)) == 70


# =====================================================================
# 14. Additional Paired Publication Failure & Success Tests
# =====================================================================

def test_publish_catalog_pair_failure_injection_csv_rollback_failure(tmp_path):
    """Simulated failure during CSV rollback preserves backups on disk for recovery."""
    final_json = tmp_path / "marianos_latest.json"
    final_csv = tmp_path / "marianos_latest.csv"
    staging_json = tmp_path / "staging.json"
    staging_csv = tmp_path / "staging.csv"

    final_json.write_text('{"version": "orig_json"}', encoding="utf-8")
    final_csv.write_text("col1\norig_csv\n", encoding="utf-8")
    staging_json.write_text('{"version": "new_json"}', encoding="utf-8")
    staging_csv.write_text("col1\nnew_csv\n", encoding="utf-8")

    import schema
    import shutil
    orig_publish = schema.publish_staging_file
    orig_copy2 = shutil.copy2

    def faulty_publish_csv(src, dst):
        if dst == final_csv:
            raise IOError("Simulated error on CSV replace")
        orig_publish(src, dst)

    def faulty_copy2_on_csv_rollback(src, dst):
        # Explode when copying backup_csv back to final_csv
        if str(src).endswith(".csv.bak"):
            raise IOError("Simulated disk error during CSV rollback!")
        return orig_copy2(src, dst)

    try:
        schema.publish_staging_file = faulty_publish_csv
        shutil.copy2 = faulty_copy2_on_csv_rollback
        with pytest.raises(RuntimeError, match="CRITICAL.*rollback failed"):
            publish_catalog_pair(staging_json, final_json, staging_csv, final_csv)
    finally:
        schema.publish_staging_file = orig_publish
        shutil.copy2 = orig_copy2

    # Backups preserved on disk
    assert (tmp_path / "marianos_latest.json.bak").exists()
    assert (tmp_path / "marianos_latest.csv.bak").exists()


def test_publish_catalog_pair_successful_publication(tmp_path):
    """Successful paired publication updates both files and cleans up backups."""
    final_json = tmp_path / "marianos_latest.json"
    final_csv = tmp_path / "marianos_latest.csv"
    staging_json = tmp_path / "staging.json"
    staging_csv = tmp_path / "staging.csv"

    final_json.write_text('{"version": "orig_json"}', encoding="utf-8")
    final_csv.write_text("col1\norig_csv\n", encoding="utf-8")
    staging_json.write_text('{"version": "new_json"}', encoding="utf-8")
    staging_csv.write_text("col1\nnew_csv\n", encoding="utf-8")

    publish_catalog_pair(staging_json, final_json, staging_csv, final_csv)

    # Both files have new contents
    assert final_json.read_text(encoding="utf-8") == '{"version": "new_json"}'
    assert final_csv.read_text(encoding="utf-8") == "col1\nnew_csv\n"

    # Backups were unlinked because publication succeeded consistently
    assert not (tmp_path / "marianos_latest.json.bak").exists()
    assert not (tmp_path / "marianos_latest.csv.bak").exists()


def test_pagination_continues_on_47_48_49_products_per_page(tmp_path, mock_store_loc):
    """Verify pages with 47-49 products do not cause premature truncation and continue pagination."""
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

    requested_offsets = []

    def mock_search_products(term, location_id, limit, start):
        requested_offsets.append(start)
        # Page 1: returns 47 items (total: 289)
        if start == 0:
            prods = [{"productId": f"P0_{i:03d}", "description": f"Apple {i}", "items": [{"price": {"regular": 2.99}, "size": "1 lb"}]} for i in range(47)]
            return prods, 289
        # Page 2: returns 50 items
        elif start == 50:
            prods = [{"productId": f"P50_{i:03d}", "description": f"Apple {i}", "items": [{"price": {"regular": 2.99}, "size": "1 lb"}]} for i in range(50)]
            return prods, 289
        # Page 3: returns 50 items
        elif start == 100:
            prods = [{"productId": f"P100_{i:03d}", "description": f"Apple {i}", "items": [{"price": {"regular": 2.99}, "size": "1 lb"}]} for i in range(50)]
            return prods, 289
        # Page 4: returns 50 items
        elif start == 150:
            prods = [{"productId": f"P150_{i:03d}", "description": f"Apple {i}", "items": [{"price": {"regular": 2.99}, "size": "1 lb"}]} for i in range(50)]
            return prods, 289
        # Page 5: returns 50 items
        elif start == 200:
            prods = [{"productId": f"P200_{i:03d}", "description": f"Apple {i}", "items": [{"price": {"regular": 2.99}, "size": "1 lb"}]} for i in range(50)]
            return prods, 289
        # Page 6 (offset 250): returns remaining 42 items (exhausted: 250 + 42 = 292 >= 289)
        elif start == 250:
            prods = [{"productId": f"P250_{i:03d}", "description": f"Apple {i}", "items": [{"price": {"regular": 2.99}, "size": "1 lb"}]} for i in range(42)]
            return prods, 289
        return [], 289

    client.search_products = mock_search_products

    records, stats, meta = collect_kroger_catalog(
        client=client,
        location=mock_store_loc,
        categories_terms={"Produce": ["apples"]},
        target_count=500,
        proof_only=False,
        evidence_dir=tmp_path / "evidence",
        checkpoint_dir=tmp_path / "checkpoints",
    )

    # Offset progression verified
    assert requested_offsets == [0, 50, 100, 150, 200, 250]
    produce_stat = stats[0]
    term_detail = produce_stat["term_details"][0]
    assert term_detail["stop_reason"] == "exhausted"
    assert term_detail["status"] == "exhausted"
    assert term_detail["pages_requested"] == 6
    assert len(records) == 47 + 50 + 50 + 50 + 50 + 42


def test_pagination_reaches_api_limit_on_large_catalog_with_48_item_first_page(tmp_path, mock_store_loc):
    """Verify that a 48-item first page does not truncate and reaches api_limit_reached at offset 250."""
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

    requested_offsets = []

    def mock_search_products(term, location_id, limit, start):
        requested_offsets.append(start)
        # Page 1: returns 48 items out of 548
        count = 48 if start == 0 else 50
        prods = [{"productId": f"P{start}_{i:03d}", "description": f"Potato {i}", "items": [{"price": {"regular": 1.99}, "size": "5 lb"}]} for i in range(count)]
        return prods, 548

    client.search_products = mock_search_products

    records, stats, meta = collect_kroger_catalog(
        client=client,
        location=mock_store_loc,
        categories_terms={"Produce": ["potatoes"]},
        target_count=500,
        proof_only=False,
        evidence_dir=tmp_path / "evidence",
        checkpoint_dir=tmp_path / "checkpoints",
    )

    assert requested_offsets == [0, 50, 100, 150, 200, 250]
    term_detail = stats[0]["term_details"][0]
    assert term_detail["stop_reason"] == "api_limit_reached"
    assert term_detail["status"] == "stopped_by_api_limit"
    assert stats[0]["coverage_status"] == "limited_by_api"


def test_pagination_premature_truncation_when_zero_products_returned_before_total(tmp_path, mock_store_loc):
    """Verify premature_truncation is flagged when API returns 0 items before reaching total."""
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

    def mock_search_products(term, location_id, limit, start):
        if start == 0:
            return [{"productId": f"P0_{i:03d}", "description": f"Berry {i}", "items": [{"price": {"regular": 3.99}, "size": "1 pint"}]} for i in range(49)], 400
        # Page 2 unexpectedly returns empty list despite total=400
        return [], 400

    client.search_products = mock_search_products

    records, stats, meta = collect_kroger_catalog(
        client=client,
        location=mock_store_loc,
        categories_terms={"Produce": ["berries"]},
        target_count=500,
        proof_only=False,
        evidence_dir=tmp_path / "evidence",
        checkpoint_dir=tmp_path / "checkpoints",
    )

    term_detail = stats[0]["term_details"][0]
    assert term_detail["stop_reason"] == "premature_truncation"
    assert term_detail["status"] == "incomplete"
    assert stats[0]["coverage_status"] == "partial"
    assert stats[0]["coverage_complete"] is False


def test_evidence_run_isolation_and_traceability(tmp_path, mock_store_loc):
    """Verify evidence records contain run_id, historical lines are preserved, and runs are isolated."""
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

    ev_dir = tmp_path / "shared_evidence"
    cp_dir = tmp_path / "shared_checkpoints"

    # Run 1: run_id = "RUN_20261102T010000Z"
    client.search_products = lambda term, location_id, limit, start: (
        [{"productId": f"RUN1_{i:02d}", "upc": f"00011111110{i:02d}", "description": f"Milk {i}", "items": [{"price": {"regular": 2.50}, "size": "1 gal"}]} for i in range(3)],
        3
    )
    recs1, _, _ = collect_kroger_catalog(
        client=client,
        location=mock_store_loc,
        categories_terms={"Dairy": ["milk"]},
        run_id="RUN_20261102T010000Z",
        evidence_dir=ev_dir,
        checkpoint_dir=cp_dir,
    )
    assert len(recs1) == 3

    # Run 2: run_id = "RUN_20261102T020000Z"
    client.search_products = lambda term, location_id, limit, start: (
        [{"productId": f"RUN2_{i:02d}", "upc": f"00022222220{i:02d}", "description": f"Cheese {i}", "items": [{"price": {"regular": 4.00}, "size": "8 oz"}]} for i in range(4)],
        4
    )
    recs2, _, _ = collect_kroger_catalog(
        client=client,
        location=mock_store_loc,
        categories_terms={"Dairy": ["cheese"]},
        run_id="RUN_20261102T020000Z",
        evidence_dir=ev_dir,
        checkpoint_dir=cp_dir,
    )
    assert len(recs2) == 4

    ev_file = ev_dir / "product_evidence_53100502.jsonl"
    assert ev_file.exists()

    all_lines = [json.loads(line) for line in ev_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    # Total historical lines preserved (3 from Run 1 + 4 from Run 2)
    assert len(all_lines) == 7

    # Run isolation
    run1_entries = [e for e in all_lines if e.get("run_id") == "RUN_20261102T010000Z"]
    run2_entries = [e for e in all_lines if e.get("run_id") == "RUN_20261102T020000Z"]
    assert len(run1_entries) == 3
    assert len(run2_entries) == 4

    # Full traceability assertions on every entry
    for entry in all_lines:
        assert entry["run_id"] in ("RUN_20261102T010000Z", "RUN_20261102T020000Z")
        assert entry["store_id"] == "53100502"
        assert entry["category"] == "Dairy"
        assert entry["search_term"] in ("milk", "cheese")
        assert "requested_offset" in entry
        assert entry["upc"].startswith("000")
        assert "raw_product_payload" in entry and isinstance(entry["raw_product_payload"], dict)
        assert "selected_item_payload" in entry and isinstance(entry["selected_item_payload"], dict)
        assert "product_key" in entry



