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


def test_checkpoint_accepts_all_distinguished_statuses():
    saved, location, fingerprint = _valid_checkpoint()
    for status in scraper.VALID_CATEGORY_STATUSES:
        saved["completed_categories"] = [{"category": "Produce", "status": status}]
        assert scraper.is_checkpoint_compatible(saved, location, fingerprint), f"Failed for status: {status}"


def test_failed_category_preserves_previous_records_in_apply():
    existing = [
        {"category": "Produce", "product_title": "Old Apple", "upc": "111", "url": "https://a.test/1", "crawl_date": "2026-10-20"},
        {"category": "Meat", "product_title": "Beef", "upc": "222", "url": "https://a.test/2", "crawl_date": "2026-10-20"},
    ]
    fresh = [
        {"category": "Produce", "product_title": "Fresh Apple", "upc": "111", "url": "https://a.test/1", "crawl_date": "2026-10-31"},
    ]
    # Incomplete / failed statuses must never replace previous records
    for bad_status in ("incomplete", "failed", "blocked_or_challenged", "repeated_pagination_page", "product_parse_failure", "interrupted"):
        result = scraper.apply_category_result(existing, "Produce", fresh, bad_status)
        assert result == existing


def test_azure_blob_publish_skipped_when_unconfigured(tmp_path):
    staging = tmp_path / "staging.json"
    report = tmp_path / "report.json"
    staging.write_text("[]", encoding="utf-8")
    report.write_text("{}", encoding="utf-8")

    result = scraper.publish_to_azure_blob(staging, report, "20261031T120000Z", {})
    assert result["published"] is False
    assert result["reason"] == "not_configured"


def test_azure_blob_publish_uploads_when_configured(tmp_path, monkeypatch):
    staging = tmp_path / "staging.json"
    report = tmp_path / "report.json"
    staging.write_text("[catalog data]", encoding="utf-8")
    report.write_text('{"status": "ok"}', encoding="utf-8")

    uploaded_blobs = {}

    class FakeBlobClient:
        def __init__(self, name):
            self.name = name

        def upload_blob(self, data, overwrite=False):
            uploaded_blobs[self.name] = data

    class FakeContainerClient:
        def exists(self):
            return True

        def get_blob_client(self, blob_name):
            return FakeBlobClient(blob_name)

    class FakeServiceClient:
        @classmethod
        def from_connection_string(cls, _conn):
            return cls()

        def get_container_client(self, _name):
            return FakeContainerClient()

    import sys
    import types
    fake_azure = types.ModuleType("azure")
    fake_storage = types.ModuleType("azure.storage")
    fake_blob = types.ModuleType("azure.storage.blob")
    fake_blob.BlobServiceClient = FakeServiceClient
    monkeypatch.setitem(sys.modules, "azure", fake_azure)
    monkeypatch.setitem(sys.modules, "azure.storage", fake_storage)
    monkeypatch.setitem(sys.modules, "azure.storage.blob", fake_blob)

    cfg = {
        "azure": {
            "connection_string": "DefaultEndpointsProtocol=https;AccountName=test;AccountKey=fake;EndpointSuffix=core.windows.net",
            "container_name": "test-container",
        }
    }

    result = scraper.publish_to_azure_blob(staging, report, "20261031T120000Z", cfg)
    assert result["published"] is True
    assert result["container"] == "test-container"
    assert "marianos_latest.json" in uploaded_blobs
    assert "runs/20261031T120000Z/catalog.json" in uploaded_blobs
    assert "runs/20261031T120000Z/report.json" in uploaded_blobs
    assert uploaded_blobs["marianos_latest.json"] == b"[catalog data]"


def test_interruption_saves_checkpoint_and_resumes_to_completion(tmp_path, monkeypatch):
    import argparse
    import asyncio
    import json

    data_dir = tmp_path / "data"
    checkpoint_dir = data_dir / "checkpoints"
    monkeypatch.setattr(scraper, "DATA_DIR", data_dir)
    monkeypatch.setattr(scraper, "CHECKPOINT_DIR", checkpoint_dir)

    cfg_file = tmp_path / "settings.json"
    cfg = {
        "locations": [
            {
                "id": "vernon-hills",
                "zipcode": "60061",
                "store_name": "Mariano's",
                "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061",
                "store_id": "53100502",
            }
        ],
        "min_records_for_publish": 2,
        "min_categories_with_products": 2,
    }
    cfg_file.write_text(json.dumps(cfg), encoding="utf-8")

    cat_file = tmp_path / "categories.csv"
    cat_file.write_text("Category (Auto),URL\nDairy,https://example.test/dairy\nProduce,https://example.test/produce\n", encoding="utf-8")

    dairy_record = scraper.blank_record()
    dairy_record.update({
        "zipcode": "60061",
        "store_name": "Mariano's",
        "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061",
        "category": "Dairy",
        "product_title": "Mariano's Whole Milk",
        "url": "https://example.test/milk",
        "upc": "1111111111111",
        "crawl_date": "2026-10-31",
    })

    produce_record = scraper.blank_record()
    produce_record.update({
        "zipcode": "60061",
        "store_name": "Mariano's",
        "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061",
        "category": "Produce",
        "product_title": "Fresh Gala Apples",
        "url": "https://example.test/apples",
        "upc": "2222222222222",
        "crawl_date": "2026-10-31",
    })

    class FakeContext:
        async def close(self): pass

    class FakeBrowser:
        async def new_context(self, **kwargs): return FakeContext()
        async def close(self): pass

    class FakePlaywright:
        async def __aenter__(self):
            class P:
                chromium = FakeChromium()
            return P()
        async def __aexit__(self, *args): pass

    class FakeChromium:
        async def launch(self, **kwargs): return FakeBrowser()

    monkeypatch.setattr(scraper, "async_playwright", lambda: FakePlaywright())

    crawl_calls = []

    async def fake_crawl_first_run(context, category, location, cfg, crawl_date):
        name = category["category"]
        crawl_calls.append(name)
        if name == "Dairy":
            return [dairy_record], {
                "category": "Dairy", "status": "ok", "outcome": scraper.OUTCOME_FULLY_EXHAUSTED,
                "records": 1, "product_parse_failures": [],
                "store_evidence": {"selected_store_verified": True},
            }
        elif name == "Produce":
            raise KeyboardInterrupt("Simulated interruption during Produce")

    monkeypatch.setattr(scraper, "crawl_category", fake_crawl_first_run)

    args_run1 = argparse.Namespace(
        config=str(cfg_file),
        categories=str(cat_file),
        location="vernon-hills",
        headed=False,
        manual_store_selection=False,
        resume=False,
    )

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(scraper.run(args_run1))

    checkpoint_file = checkpoint_dir / "checkpoint_vernon-hills.json"
    assert checkpoint_file.exists()
    cp_data = json.loads(checkpoint_file.read_text(encoding="utf-8"))
    assert cp_data["location_id"] == "vernon-hills"
    statuses = {c["category"]: c["status"] for c in cp_data["completed_categories"]}
    assert statuses.get("Dairy") == "ok"
    assert statuses.get("Produce") == "interrupted"
    assert len(cp_data["records_so_far"]) == 1
    assert cp_data["records_so_far"][0]["product_title"] == "Mariano's Whole Milk"

    # Final catalog must NOT exist yet
    assert not (data_dir / "marianos_latest.json").exists()

    # Now resume the run
    crawl_calls.clear()

    async def fake_crawl_second_run(context, category, location, cfg, crawl_date):
        name = category["category"]
        crawl_calls.append(name)
        if name == "Produce":
            return [produce_record], {
                "category": "Produce", "status": "ok", "outcome": scraper.OUTCOME_FULLY_EXHAUSTED,
                "records": 1, "product_parse_failures": [],
                "store_evidence": {"selected_store_verified": True},
            }
        raise AssertionError(f"Unexpected crawl call for: {name}")

    monkeypatch.setattr(scraper, "crawl_category", fake_crawl_second_run)

    args_run2 = argparse.Namespace(
        config=str(cfg_file),
        categories=str(cat_file),
        location="vernon-hills",
        headed=False,
        manual_store_selection=False,
        resume=True,
    )

    exit_code = asyncio.run(scraper.run(args_run2))
    assert exit_code == 0
    assert crawl_calls == ["Produce"]

    final_catalog = data_dir / "marianos_latest.json"
    assert final_catalog.exists()
    published_records = json.loads(final_catalog.read_text(encoding="utf-8"))
    assert len(published_records) == 2
    titles = {r["product_title"] for r in published_records}
    assert titles == {"Mariano's Whole Milk", "Fresh Gala Apples"}
