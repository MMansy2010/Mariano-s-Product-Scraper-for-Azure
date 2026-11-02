import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from schema import FIELDS
from scraper import is_checkpoint_compatible


def make_location(location_id="loc1"):
    return {
        "id": location_id,
        "zipcode": "60061",
        "store_name": "Mariano's",
        "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061",
    }


def make_checkpoint(location_id="loc1", fingerprint="abc"):
    record = dict.fromkeys(FIELDS, "")
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

    return {
        "location_id": location_id,
        "config_fingerprint": fingerprint,
        "completed_categories": [
            {"category": "Produce", "status": "ok"},
            {"category": "Meat", "status": "failed"},
        ],
        "records_so_far": [record],
    }


def test_compatible_checkpoint_is_accepted():
    assert is_checkpoint_compatible(
        make_checkpoint(),
        make_location(),
        "abc",
    )


def test_changed_fingerprint_is_rejected():
    assert not is_checkpoint_compatible(
        make_checkpoint(),
        make_location(),
        "changed",
    )


def test_wrong_location_is_rejected():
    assert not is_checkpoint_compatible(
        make_checkpoint(location_id="loc2"),
        make_location(),
        "abc",
    )


def test_missing_checkpoint_fields_are_rejected():
    saved = make_checkpoint()
    del saved["records_so_far"]
    assert not is_checkpoint_compatible(saved, make_location(), "abc")


def test_malformed_records_are_rejected():
    saved = make_checkpoint()
    saved["records_so_far"] = [{"product_title": "incomplete"}]
    assert not is_checkpoint_compatible(saved, make_location(), "abc")


def test_legacy_checkpoint_is_rejected():
    saved = make_checkpoint()
    del saved["config_fingerprint"]
    assert not is_checkpoint_compatible(saved, make_location(), "abc")
