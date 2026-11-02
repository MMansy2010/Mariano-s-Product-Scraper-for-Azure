import copy
import json
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from schema import (
    FIELDS,
    blank_record,
    numeric_price,
    is_approx_weight,
    product_key,
    classify_deals,
    is_private_label,
    validate_records,
    verify_store_identity,
    replace_category_records,
    location_record_matches,
    load_previous_catalog,
    apply_category_result,
    publish_staging_file,
)


EXPECTED_22_FIELDS = [
    "zipcode",
    "store_name",
    "store_location",
    "store_logo",
    "store_brand",
    "category",
    "sub_category",
    "product_title",
    "weight",
    "regular_price",
    "sale_price",
    "volume_deals_description",
    "volume_deals_price",
    "digital_deals_description",
    "digital_deals_price",
    "image_url",
    "url",
    "upc",
    "gupc",
    "ppm",
    "unit_price",
    "crawl_date",
]


def test_schema_order():
    assert len(FIELDS) == 22
    assert FIELDS == EXPECTED_22_FIELDS
    assert list(blank_record().keys()) == EXPECTED_22_FIELDS
    assert FIELDS[-1] == "crawl_date"
    assert FIELDS.index("gupc") == FIELDS.index("upc") + 1
    assert all(isinstance(v, str) and v == "" for v in blank_record().values())


def test_numeric_price_rejects_deal_string():
    assert numeric_price("$1.5") == "1.50"
    assert numeric_price("2 for $5") == ""
    assert numeric_price("$3.99 each") == "3.99"


def test_approx_weight():
    assert is_approx_weight("approximately 1 lb")
    assert is_approx_weight("Approx. 2 lb")
    assert is_approx_weight("about 1.5 lb")
    assert is_approx_weight("estimated 2 lb")
    assert is_approx_weight("est. 3 lb")
    assert is_approx_weight("Ground Beef (Avg. Wt. 1.5 lbs)")
    assert is_approx_weight("Pork Chops Variable Weight")
    assert is_approx_weight("Steak (Random Weight)")
    assert is_approx_weight("Salmon (Average Weight 1 lb)")
    assert is_approx_weight("Produce item sold by weight varies")

    # Genuine fixed-weight multipacks and family/bulk packages must NOT be excluded
    assert not is_approx_weight("16 oz")
    assert not is_approx_weight("Family Pack Chicken Breast 3 lb")
    assert not is_approx_weight("Twin Pack 2x16 oz")
    assert not is_approx_weight("Bulk 5 lb Rice")
    assert not is_approx_weight("Variety Pack 24 ct")
    assert not is_approx_weight("Multipack 6x12 oz")
    assert not is_approx_weight("Club Pack 4 lb")
    # Brand foundation year (e.g. Est. 1920) must NOT trigger approx weight exclusion
    assert not is_approx_weight("28 oz", "Muir Glen Organic Tomatoes Est. 1991")
    assert not is_approx_weight("16 oz", "Farmer John Smoked Sausage Est. 1920")


def test_product_key_is_store_scoped_and_prefers_upc():
    r1 = blank_record()
    r1["upc"] = "012345678905"
    r1["url"] = "https://example.com/p/test"
    r1["zipcode"] = "60061"
    r1["store_name"] = "Marianos Vernon Hills"
    r1["store_location"] = "1720 N Milwaukee Ave"

    r2 = dict(r1)
    r2["store_name"] = "Marianos Chicago"

    key1 = product_key(r1)
    key2 = product_key(r2)

    assert "upc:012345678905" in key1
    assert "upc:012345678905" in key2
    assert key1 != key2


def test_deal_classification_separates_digital_and_volume():
    assert classify_deals("2 for $5") == ("2 for $5", "")
    assert classify_deals("Clip Coupon: Save $2") == ("", "Clip Coupon: Save $2")
    assert classify_deals("Regular price $4.99") == ("", "")


def test_private_label():
    assert is_private_label("Simple Truth Organic Milk") == "yes"
    assert is_private_label("Fresh Strawberries") == "no"


def test_validation_requires_string_values_and_empty_gupc_unit_price():
    r = blank_record()
    r.update({"category": "Produce", "product_title": "Bananas", "url": "https://example.com/p/1", "crawl_date": "2026-10-24"})
    cfg = {"min_records_for_publish": 1, "min_categories_with_products": 1}
    category_stats = [{"category": "Produce", "status": "ok", "store_evidence": {"selected_store_verified": True}}]

    # Valid baseline record passes validation
    result = validate_records([r], category_stats, cfg)
    assert result["validation_passed"] is True

    # unit_price must be empty string
    r_bad_unit = dict(r)
    r_bad_unit["unit_price"] = "1.00"
    result = validate_records([r_bad_unit], category_stats, cfg)
    assert result["validation_passed"] is False
    assert result["gupc_and_unit_price_empty"] is False

    # gupc must be empty string
    r_bad_gupc = dict(r)
    r_bad_gupc["gupc"] = "000123456789"
    result = validate_records([r_bad_gupc], category_stats, cfg)
    assert result["validation_passed"] is False
    assert result["gupc_and_unit_price_empty"] is False


@pytest.mark.parametrize("non_string_val", [None, 123, 4.99, True, False, ["nested"], {"a": 1}])
def test_validation_rejects_non_string_values_in_any_field(non_string_val):
    cfg = {"min_records_for_publish": 1, "min_categories_with_products": 1}
    category_stats = [{"category": "Produce", "status": "ok", "store_evidence": {"selected_store_verified": True}}]

    # Setting any field of the 22 schema fields to a non-string value must fail validation
    for field in FIELDS:
        r = blank_record()
        r.update({"category": "Produce", "product_title": "Bananas", "url": "https://example.com/p/1", "crawl_date": "2026-10-24"})
        r[field] = non_string_val
        val = validate_records([r], category_stats, cfg)
        assert val["validation_passed"] is False
        assert val["non_string_value_count"] >= 1


def test_store_identity_requires_both_name_and_address():
    location = {"store_name": "Marianos Shorewood", "store_location": "950 Brookforest Ave"}
    assert verify_store_identity("Marianos Shorewood | 950 Brookforest Ave", location)
    assert not verify_store_identity("Marianos Shorewood | 100 Main Street", location)
    assert not verify_store_identity("Marianos Shorewood", location)
    assert not verify_store_identity("Marianos Shorewood | 950 Brookforest Ave", {"store_name": "", "store_location": ""})


def test_schema_replace_category_records_preserves_missing_and_updates_fresh():
    r_old1 = blank_record()
    r_old1.update({"category": "Produce", "product_title": "Old Apple", "upc": "111", "url": "https://a.test/1", "crawl_date": "2026-10-20"})
    r_old2 = blank_record()
    r_old2.update({"category": "Produce", "product_title": "Old Banana", "upc": "222", "url": "https://a.test/2", "crawl_date": "2026-10-20"})
    r_dairy = blank_record()
    r_dairy.update({"category": "Dairy", "product_title": "Milk", "upc": "333", "url": "https://a.test/3", "crawl_date": "2026-10-20"})

    r_fresh1 = blank_record()
    r_fresh1.update({"category": "Produce", "product_title": "Fresh Apple", "upc": "111", "url": "https://a.test/1", "crawl_date": "2026-10-31"})

    merged = replace_category_records([r_old1, r_old2, r_dairy], "Produce", [r_fresh1])
    # Dairy is preserved
    assert any(r["product_title"] == "Milk" for r in merged)
    # Old Apple is updated with fresh crawl_date
    updated_apple = next(r for r in merged if r["upc"] == "111")
    assert updated_apple["product_title"] == "Fresh Apple"
    assert updated_apple["crawl_date"] == "2026-10-31"
    # Old Banana is preserved with original crawl_date
    preserved_banana = next(r for r in merged if r["upc"] == "222")
    assert preserved_banana["product_title"] == "Old Banana"
    assert preserved_banana["crawl_date"] == "2026-10-20"


def test_schema_apply_category_result_only_updates_on_ok():
    r_old = blank_record()
    r_old.update({"category": "Produce", "product_title": "Old Apple", "upc": "111", "url": "https://a.test/1", "crawl_date": "2026-10-20"})
    r_fresh = blank_record()
    r_fresh.update({"category": "Produce", "product_title": "Fresh Apple", "upc": "111", "url": "https://a.test/1", "crawl_date": "2026-10-31"})

    # Failed status keeps old records untouched
    kept = apply_category_result([r_old], "Produce", [r_fresh], "incomplete_due_to_failed_requests")
    assert kept[0]["product_title"] == "Old Apple"
    assert kept[0]["crawl_date"] == "2026-10-20"

    # Ok status applies fresh records
    updated = apply_category_result([r_old], "Produce", [r_fresh], "ok")
    assert updated[0]["product_title"] == "Fresh Apple"
    assert updated[0]["crawl_date"] == "2026-10-31"


def test_schema_publish_staging_file_is_atomic(tmp_path):
    staging = tmp_path / "staging.json"
    final = tmp_path / "final.json"
    staging.write_text('["new"]', encoding="utf-8")
    final.write_text('["old"]', encoding="utf-8")

    publish_staging_file(staging, final)
    assert final.read_text(encoding="utf-8") == '["new"]'
    assert not (tmp_path / "final.json.tmp").exists()


def test_schema_replace_category_records_handles_category_movement():
    # Item 1 was historically in Bakery
    item1 = blank_record()
    item1.update({"category": "Bakery", "product_title": "Gluten Free Bread", "upc": "999", "url": "https://a.test/999", "crawl_date": "2026-10-15"})
    # Item 2 is another Bakery item
    item2 = blank_record()
    item2.update({"category": "Bakery", "product_title": "Bagel", "upc": "888", "url": "https://a.test/888", "crawl_date": "2026-10-15"})

    # In fresh crawl, Item 1 is categorized under "Natural Organic"
    item1_new = blank_record()
    item1_new.update({"category": "Natural Organic", "product_title": "Gluten Free Bread", "upc": "999", "url": "https://a.test/999", "crawl_date": "2026-11-01"})

    merged = replace_category_records([item1, item2], "Natural Organic", [item1_new])

    # Item 1 must appear only ONCE in the merged list, under Natural Organic
    item1_matches = [r for r in merged if r["upc"] == "999"]
    assert len(item1_matches) == 1
    assert item1_matches[0]["category"] == "Natural Organic"
    assert item1_matches[0]["crawl_date"] == "2026-11-01"

    # Item 2 remains preserved under Bakery
    item2_matches = [r for r in merged if r["upc"] == "888"]
    assert len(item2_matches) == 1
    assert item2_matches[0]["category"] == "Bakery"


def test_location_record_matches_safe_normalization():
    loc = {
        "zipcode": "60061",
        "store_name": "Mariano's",
        "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061",
    }

    # Exact match
    r1 = {"zipcode": "60061", "store_name": "Mariano's", "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061"}
    assert location_record_matches(r1, loc)

    # Safe punctuation / casing / abbreviation variations
    r2 = {"zipcode": "60061", "store_name": "Marianos", "store_location": "1720 N. Milwaukee Ave., Vernon Hills, IL 60061"}
    assert location_record_matches(r2, loc)

    # Street part match
    r3 = {"zipcode": "60061", "store_name": "Mariano's", "store_location": "1720 N Milwaukee Ave"}
    assert location_record_matches(r3, loc)

    # Different store must NOT match
    r4 = {"zipcode": "60061", "store_name": "Mariano's", "store_location": "950 Brookforest Ave, Shorewood, IL 60404"}
    assert not location_record_matches(r4, loc)

    # Different zip must NOT match
    r5 = {"zipcode": "60404", "store_name": "Mariano's", "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061"}
    assert not location_record_matches(r5, loc)


def test_load_previous_catalog_valid_catalog(tmp_path):
    p = tmp_path / "valid.json"
    rec = blank_record()
    rec.update({
        "zipcode": "60061",
        "store_name": "Mariano's",
        "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061",
        "category": "Produce",
        "product_title": "Honeycrisp Apples",
        "weight": "3 lb",
        "regular_price": "4.99",
        "upc": "0001111000001",
        "url": "https://example.test/p/1",
        "crawl_date": "2026-10-15",
    })
    p.write_text(json.dumps([rec]), encoding="utf-8")
    loaded = load_previous_catalog(p)
    assert len(loaded) == 1
    assert loaded[0]["upc"] == "0001111000001"
    assert loaded[0]["product_title"] == "Honeycrisp Apples"
    assert loaded[0]["crawl_date"] == "2026-10-15"


def test_load_previous_catalog_missing_file(tmp_path):
    p = tmp_path / "nonexistent.json"
    assert load_previous_catalog(p) == []


def test_load_previous_catalog_corrupted_json(tmp_path):
    p = tmp_path / "corrupt.json"
    p.write_text("{broken json syntax", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Previous catalog is unreadable"):
        load_previous_catalog(p)


def test_load_previous_catalog_empty_catalog_behavior(tmp_path):
    p_empty_list = tmp_path / "empty_list.json"
    p_empty_list.write_text("[]", encoding="utf-8")
    with pytest.raises(RuntimeError, match="non-empty JSON list"):
        load_previous_catalog(p_empty_list)

    p_dict = tmp_path / "dict.json"
    p_dict.write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="non-empty JSON list"):
        load_previous_catalog(p_dict)


def test_load_previous_catalog_wrong_schema_field_count_or_order(tmp_path):
    # Record is not a dict
    p_not_dict = tmp_path / "not_dict.json"
    p_not_dict.write_text(json.dumps(["not a dict"]), encoding="utf-8")
    with pytest.raises(RuntimeError, match="does not match the exact 22-field schema"):
        load_previous_catalog(p_not_dict)

    # Missing a schema field
    p_missing_field = tmp_path / "missing_field.json"
    rec_missing = blank_record()
    del rec_missing["crawl_date"]
    p_missing_field.write_text(json.dumps([rec_missing]), encoding="utf-8")
    with pytest.raises(RuntimeError, match="does not match the exact 22-field schema"):
        load_previous_catalog(p_missing_field)

    # Reversed / incorrect key order
    p_wrong_order = tmp_path / "wrong_order.json"
    reversed_keys = list(reversed(FIELDS))
    rec_reversed = {k: "" for k in reversed_keys}
    rec_reversed.update({
        "zipcode": "60061",
        "store_name": "Mariano's",
        "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061",
        "category": "Produce",
        "product_title": "Apples",
        "url": "https://example.test/p/1",
        "crawl_date": "2026-10-15",
    })
    p_wrong_order.write_text(json.dumps([rec_reversed]), encoding="utf-8")
    with pytest.raises(RuntimeError, match="does not match the exact 22-field schema"):
        load_previous_catalog(p_wrong_order)


@pytest.mark.parametrize("bad_val", [None, 4.99, 12345, True, False, ["nested"], {"a": 1}])
def test_load_previous_catalog_non_string_values(tmp_path, bad_val):
    p = tmp_path / "non_string.json"
    rec = blank_record()
    rec.update({
        "zipcode": "60061",
        "store_name": "Mariano's",
        "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061",
        "category": "Produce",
        "product_title": "Apples",
        "url": "https://example.test/p/1",
        "crawl_date": "2026-10-15",
    })
    rec["regular_price"] = bad_val
    p.write_text(json.dumps([rec]), encoding="utf-8")
    with pytest.raises(RuntimeError, match="contains a non-string value"):
        load_previous_catalog(p)


@pytest.mark.parametrize("req_field", [
    "zipcode", "store_name", "store_location", "category",
    "product_title", "url", "crawl_date",
])
def test_load_previous_catalog_missing_required_values(tmp_path, req_field):
    p = tmp_path / f"missing_{req_field}.json"
    rec = blank_record()
    rec.update({
        "zipcode": "60061",
        "store_name": "Mariano's",
        "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061",
        "category": "Produce",
        "product_title": "Apples",
        "url": "https://example.test/p/1",
        "crawl_date": "2026-10-15",
    })
    rec[req_field] = "   "  # blank / missing required value
    p.write_text(json.dumps([rec]), encoding="utf-8")
    with pytest.raises(RuntimeError, match="missing a required value"):
        load_previous_catalog(p)


def test_load_previous_catalog_violates_gupc_or_unit_price_blank_rule(tmp_path):
    p_unit = tmp_path / "bad_unit.json"
    rec_unit = blank_record()
    rec_unit.update({
        "zipcode": "60061", "store_name": "Mariano's",
        "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061",
        "category": "Produce", "product_title": "Apples",
        "url": "https://example.test/p/1", "crawl_date": "2026-10-15",
        "unit_price": "1.99",
    })
    p_unit.write_text(json.dumps([rec_unit]), encoding="utf-8")
    with pytest.raises(RuntimeError, match="violates the blank gupc/unit_price rule"):
        load_previous_catalog(p_unit)

    p_gupc = tmp_path / "bad_gupc.json"
    rec_gupc = blank_record()
    rec_gupc.update({
        "zipcode": "60061", "store_name": "Mariano's",
        "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061",
        "category": "Produce", "product_title": "Apples",
        "url": "https://example.test/p/1", "crawl_date": "2026-10-15",
        "gupc": "000123456789",
    })
    p_gupc.write_text(json.dumps([rec_gupc]), encoding="utf-8")
    with pytest.raises(RuntimeError, match="violates the blank gupc/unit_price rule"):
        load_previous_catalog(p_gupc)


def test_load_previous_catalog_duplicate_store_scoped_identities(tmp_path):
    p = tmp_path / "duplicate.json"
    rec1 = blank_record()
    rec1.update({
        "zipcode": "60061",
        "store_name": "Mariano's",
        "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061",
        "category": "Produce",
        "product_title": "Apples",
        "weight": "3 lb",
        "regular_price": "4.99",
        "upc": "0001111000001",
        "url": "https://example.test/p/1",
        "crawl_date": "2026-10-15",
    })
    rec2 = copy.deepcopy(rec1)
    rec2["regular_price"] = "3.99"
    p.write_text(json.dumps([rec1, rec2]), encoding="utf-8")
    with pytest.raises(RuntimeError, match="duplicate store-scoped product identities"):
        load_previous_catalog(p)


def test_load_previous_catalog_excludes_approximate_weight_products(tmp_path):
    p = tmp_path / "approx_weight.json"
    rec_fixed = blank_record()
    rec_fixed.update({
        "zipcode": "60061",
        "store_name": "Mariano's",
        "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061",
        "category": "Produce",
        "product_title": "Bagged Honeycrisp Apples",
        "weight": "3 lb",
        "regular_price": "4.99",
        "upc": "0001111000001",
        "url": "https://example.test/p/1",
        "crawl_date": "2026-10-15",
    })
    rec_approx = blank_record()
    rec_approx.update({
        "zipcode": "60061",
        "store_name": "Mariano's",
        "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061",
        "category": "Produce",
        "product_title": "Loose Honeycrisp Apples",
        "weight": "approx 1.5 lb each",
        "regular_price": "2.99",
        "upc": "0001111000002",
        "url": "https://example.test/p/2",
        "crawl_date": "2026-10-15",
    })
    p.write_text(json.dumps([rec_fixed, rec_approx]), encoding="utf-8")
    loaded = load_previous_catalog(p)
    assert len(loaded) == 1
    assert loaded[0]["upc"] == "0001111000001"
    assert loaded[0]["product_title"] == "Bagged Honeycrisp Apples"


def test_exact_22_field_contract_rejects_legacy_fields():
    """Verify FIELDS is the authoritative single source of truth and contains zero legacy fields."""
    expected_contract = [
        "zipcode", "store_name", "store_location", "store_logo", "store_brand",
        "category", "sub_category", "product_title", "weight", "regular_price",
        "sale_price", "volume_deals_description", "volume_deals_price",
        "digital_deals_description", "digital_deals_price", "image_url", "url",
        "upc", "gupc", "ppm", "unit_price", "crawl_date"
    ]
    assert len(FIELDS) == 22
    assert FIELDS == expected_contract

    # Forbidden legacy or alternative schema field names
    legacy_or_alternative_fields = [
        "store_address",
        "product_id",
        "product_name",
        "brand",
        "package_size",
        "stock_status",
        "in_stock",
        "product_url",
        "digital_deals_available",
        "volume_deals_available",
    ]
    for field in legacy_or_alternative_fields:
        assert field not in FIELDS, f"Legacy/alternative field {field!r} found in authoritative FIELDS"


def test_published_baseline_json_and_csv_contract_compliance():
    """Verify existing published baseline JSON and CSV against exact 22-field contract without modifying them."""
    import csv
    repo_root = Path(__file__).resolve().parents[1]
    json_path = repo_root / "data" / "marianos_latest.json"
    csv_path = repo_root / "data" / "marianos_latest.csv"

    assert json_path.exists(), "Baseline marianos_latest.json must exist"
    assert csv_path.exists(), "Baseline marianos_latest.csv must exist"

    # Verify JSON baseline
    with json_path.open("r", encoding="utf-8") as f:
        json_records = json.load(f)
    assert len(json_records) == 560, f"Expected 560 baseline products, got {len(json_records)}"

    for idx, r in enumerate(json_records):
        assert list(r.keys()) == FIELDS, f"Record {idx} keys do not match exact 22-field contract order"
        assert all(isinstance(v, str) for v in r.values()), f"Record {idx} has non-string values"
        assert r["gupc"] == "", f"Record {idx} gupc must be empty string"
        assert r["unit_price"] == "", f"Record {idx} unit_price must be empty string"
        assert r["regular_price"] != "", f"Record {idx} regular_price must not be empty"

    # Verify CSV baseline
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        assert header == FIELDS, "CSV header does not match exact 22-field contract order"
        csv_rows = list(reader)
        assert len(csv_rows) == 560, f"Expected 560 CSV rows, got {len(csv_rows)}"
        for idx, row in enumerate(csv_rows):
            assert len(row) == 22, f"CSV row {idx} does not have exactly 22 columns"
            # gupc is column index 18, unit_price is column index 20
            assert row[18] == "", f"CSV row {idx} gupc must be empty string"
            assert row[20] == "", f"CSV row {idx} unit_price must be empty string"
            assert row[9] != "", f"CSV row {idx} regular_price must not be empty"

