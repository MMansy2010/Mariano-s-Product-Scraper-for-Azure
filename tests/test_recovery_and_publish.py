import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import scraper


def test_resume_replaces_only_the_retried_category():
    existing = [
        {"category": "Produce", "product_title": "Old apple"},
        {"category": "Dairy", "product_title": "Milk"},
        {"category": "Produce", "product_title": "Old banana"},
    ]
    fresh = [{"category": "Produce", "product_title": "Fresh apple"}]

    result = scraper.replace_category_records(existing, "Produce", fresh)

    assert result == [
        {"category": "Dairy", "product_title": "Milk"},
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
