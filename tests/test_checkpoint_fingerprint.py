import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scraper import checkpoint_fingerprint


def test_fingerprint_is_stable():
    location = {"id": "loc1", "zipcode": "60601"}
    categories = [{"category": "Produce", "url": "https://example.com/produce"}]

    first = checkpoint_fingerprint(location, categories)
    second = checkpoint_fingerprint(location.copy(), categories.copy())

    assert first == second


def test_fingerprint_changes_when_location_changes():
    categories = [{"category": "Produce", "url": "https://example.com/produce"}]

    first = checkpoint_fingerprint(
        {"id": "loc1", "zipcode": "60601"}, categories
    )
    second = checkpoint_fingerprint(
        {"id": "loc1", "zipcode": "60602"}, categories
    )

    assert first != second


def test_fingerprint_changes_when_categories_change():
    location = {"id": "loc1", "zipcode": "60601"}

    first = checkpoint_fingerprint(
        location,
        [{"category": "Produce", "url": "https://example.com/produce"}],
    )
    second = checkpoint_fingerprint(
        location,
        [{"category": "Meat", "url": "https://example.com/meat"}],
    )

    assert first != second