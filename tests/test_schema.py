import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from schema import (FIELDS, blank_record, numeric_price, is_approx_weight,
                    product_key, classify_deals, is_private_label, validate_records, verify_store_identity)


def test_schema_order():
    assert list(blank_record().keys()) == FIELDS
    assert FIELDS[-1] == "crawl_date"
    assert FIELDS.index("gupc") == FIELDS.index("upc") + 1


def test_numeric_price_rejects_deal_string():
    assert numeric_price("$1.5") == "1.50"
    assert numeric_price("2 for $5") == ""
    assert numeric_price("$3.99 each") == "3.99"


def test_approx_weight():
    assert is_approx_weight("approximately 1 lb")
    assert is_approx_weight("Approx. 2 lb")
    assert not is_approx_weight("16 oz")


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
    result = validate_records([r], category_stats, cfg)
    assert result["validation_passed"] is True
    r["unit_price"] = "1.00"
    result = validate_records([r], category_stats, cfg)
    assert result["validation_passed"] is False


def test_store_identity_requires_both_name_and_address():
    location = {"store_name": "Marianos Shorewood", "store_location": "950 Brookforest Ave"}
    assert verify_store_identity("Marianos Shorewood | 950 Brookforest Ave", location)
    assert not verify_store_identity("Marianos Shorewood | 100 Main Street", location)
    assert not verify_store_identity("Marianos Shorewood", location)
    assert not verify_store_identity("Marianos Shorewood | 950 Brookforest Ave", {"store_name": "", "store_location": ""})
