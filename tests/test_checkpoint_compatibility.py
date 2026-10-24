
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from schema import FIELDS
from scraper import is_checkpoint_compatible


def make_checkpoint(location_id="loc1", fingerprint="abc"):
    return {
        "location_id": location_id,
        "config_fingerprint": fingerprint,
        "completed_categories": [
            {"category": "Produce", "status": "ok"},
            {"category": "Meat", "status": "failed"},
        ],
        "records_so_far": [
            dict.fromkeys(FIELDS, ""),
        ],
    }


def test_compatible_checkpoint_is_accepted():
    location = {"id": "loc1"}
    saved = make_checkpoint()
    assert is_checkpoint_compatible(saved, location, "abc")


def test_changed_fingerprint_is_rejected():
    assert not is_checkpoint_compatible(
        make_checkpoint(),
        {"id": "loc1"},
        "changed",
    )


def test_wrong_location_is_rejected():
    assert not is_checkpoint_compatible(
        make_checkpoint(location_id="loc2"),
        {"id": "loc1"},
        "abc",
    )


def test_missing_checkpoint_fields_are_rejected():
    saved = make_checkpoint()
    del saved["records_so_far"]
    assert not is_checkpoint_compatible(saved, {"id": "loc1"}, "abc")


def test_malformed_records_are_rejected():
    saved = make_checkpoint()
    saved["records_so_far"] = [{"product_title": "incomplete"}]
    assert not is_checkpoint_compatible(saved, {"id": "loc1"}, "abc")


def test_legacy_checkpoint_is_rejected():
    saved = make_checkpoint()
    del saved["config_fingerprint"]
    assert not is_checkpoint_compatible(saved, {"id": "loc1"}, "abc")
