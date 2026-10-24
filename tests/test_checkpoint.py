import json


def test_checkpoint_round_trip(tmp_path):
    checkpoint_path = tmp_path / "checkpoint_test-location.json"
    expected = {
        "run_id": "test-run",
        "location_id": "test-location",
        "completed_categories": [
            {"category": "Produce", "status": "ok"},
            {"category": "Meat", "status": "failed"},
        ],
        "records_so_far": [
            {"product_title": "Test apples", "category": "Produce"}
        ],
    }

    temp_path = checkpoint_path.with_suffix(".tmp")
    temp_path.write_text(
        json.dumps(expected, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(checkpoint_path)

    actual = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert actual == expected

    completed_names = {
        item["category"]
        for item in actual["completed_categories"]
        if item.get("status") == "ok"
    }
    assert completed_names == {"Produce"}
    assert actual["records_so_far"][0]["product_title"] == "Test apples"


def test_corrupt_checkpoint_is_detectable(tmp_path):
    checkpoint_path = tmp_path / "checkpoint_test-location.json"
    checkpoint_path.write_text('{"location_id": ', encoding="utf-8")

    try:
        json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        pass
    else:
        raise AssertionError("Corrupt checkpoint should not parse")
