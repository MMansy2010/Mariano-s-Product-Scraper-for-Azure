import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import scraper


def test_resume_retains_missing_products_in_retried_category():
    existing = [
        {"category": "Produce", "product_title": "Old apple"},
        {"category": "Dairy", "product_title": "Milk"},
        {"category": "Produce", "product_title": "Old banana"},
    ]
    fresh = [{"category": "Produce", "product_title": "Fresh apple"}]

    result = scraper.replace_category_records(existing, "Produce", fresh)

    assert result == [
        {"category": "Dairy", "product_title": "Milk"},
        {"category": "Produce", "product_title": "Old apple"},
        {"category": "Produce", "product_title": "Old banana"},
        {"category": "Produce", "product_title": "Fresh apple"},
    ]


def test_atomic_publish_replaces_old_file(tmp_path):
    staging = tmp_path / "staging.json"
    final = tmp_path / "catalog.json"
    staging.write_text("new catalog", encoding="utf-8")
    final.write_text("old catalog", encoding="utf-8")

    scraper.publish_staging_file(staging, final)

    assert final.read_text(encoding="utf-8") == "new catalog"
    assert not final.with_name(final.name + ".tmp").exists()


def test_failed_copy_preserves_old_file_and_cleans_temp(tmp_path, monkeypatch):
    staging = tmp_path / "staging.json"
    final = tmp_path / "catalog.json"
    staging.write_text("new catalog", encoding="utf-8")
    final.write_text("previous good catalog", encoding="utf-8")

    def failing_copy(_source, destination):
        Path(destination).write_text("partial data", encoding="utf-8")
        raise OSError("simulated copy failure")

    monkeypatch.setattr(scraper.shutil, "copy2", failing_copy)

    with pytest.raises(OSError, match="simulated copy failure"):
        scraper.publish_staging_file(staging, final)

    assert final.read_text(encoding="utf-8") == "previous good catalog"
    assert not final.with_name(final.name + ".tmp").exists()


def _product(title, upc, crawl_date, **overrides):
    record = {
        "zipcode": "60061",
        "store_name": "Mariano's",
        "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL",
        "category": "Produce",
        "product_title": title,
        "weight": "1 each",
        "url": f"https://example.test/product/{upc}",
        "upc": upc,
        "crawl_date": crawl_date,
    }
    record.update(overrides)
    return record


def test_missing_products_are_retained_with_original_dates():
    old = [
        _product(f"Product {i}", f"{i:013d}", "2026-10-20")
        for i in range(100)
    ]
    fresh = [
        _product(f"Product {i}", f"{i:013d}", "2026-10-31")
        for i in range(80)
    ]

    result = scraper.replace_category_records(old, "Produce", fresh)

    assert len(result) == 100

    by_upc = {record["upc"]: record for record in result}

    # Freshly observed products receive the new observation date.
    assert by_upc["0000000000000"]["crawl_date"] == "2026-10-31"

    # Products absent from the fresh crawl retain their original records.
    assert by_upc["0000000000099"]["crawl_date"] == "2026-10-20"
    assert by_upc["0000000000099"]["product_title"] == "Product 99"


def test_approximate_weight_products_are_not_retained():
    old = [
        _product("Chicken approximately 2 lb", "0000000000001",
                 "2026-10-20", weight="2 lb"),
        _product("Family Pack Chicken", "0000000000002",
                 "2026-10-20", weight="2 lb"),
    ]
    fresh = [
        _product("Chicken approximately 2 lb", "0000000000001",
                 "2026-10-31", weight="2 lb"),
    ]

    result = scraper.replace_category_records(old, "Produce", fresh)

    assert [record["upc"] for record in result] == ["0000000000002"]


def test_same_upc_at_different_stores_is_retained_separately():
    old = [
        _product("Milk", "0000000000001", "2026-10-20"),
    ]
    other_store = _product(
        "Milk", "0000000000001", "2026-10-20",
        store_location="Another store",
    )
    fresh = [
        _product("Milk", "0000000000001", "2026-10-31"),
    ]

    result = scraper.replace_category_records(
        old + [other_store], "Produce", fresh
    )

    assert len(result) == 2
    assert {r["store_location"] for r in result} == {
        "1720 N Milwaukee Ave, Vernon Hills, IL",
        "Another store",
    }


def _valid_checkpoint_record():
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
    return record


def _valid_checkpoint():
    location = {
        "id": "vernon-hills",
        "zipcode": "60061",
        "store_name": "Mariano's",
        "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061",
    }
    fingerprint = "test-fingerprint"
    saved = {
        "location_id": location["id"],
        "config_fingerprint": fingerprint,
        "completed_categories": [
            {"category": "Produce", "status": "ok"}
        ],
        "records_so_far": [_valid_checkpoint_record()],
    }
    return saved, location, fingerprint


def test_checkpoint_rejects_non_string_record_values():
    saved, location, fingerprint = _valid_checkpoint()
    saved["records_so_far"][0]["regular_price"] = 4.99

    assert not scraper.is_checkpoint_compatible(saved, location, fingerprint)


def test_checkpoint_rejects_records_from_another_store():
    saved, location, fingerprint = _valid_checkpoint()
    saved["records_so_far"][0]["store_location"] = "Another store"

    assert not scraper.is_checkpoint_compatible(saved, location, fingerprint)


def test_checkpoint_rejects_duplicate_product_identities():
    saved, location, fingerprint = _valid_checkpoint()
    saved["records_so_far"].append(dict(saved["records_so_far"][0]))

    assert not scraper.is_checkpoint_compatible(saved, location, fingerprint)


def test_checkpoint_accepts_valid_store_scoped_records():
    saved, location, fingerprint = _valid_checkpoint()

    assert scraper.is_checkpoint_compatible(saved, location, fingerprint)
