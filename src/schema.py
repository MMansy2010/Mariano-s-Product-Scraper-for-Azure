"""Schema and pure validation helpers for the Mariano's scraper.

Kept independent of Playwright so unit tests can run without a browser installation.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

FIELDS = [
    "zipcode", "store_name", "store_location", "store_logo", "store_brand",
    "category", "sub_category", "product_title", "weight", "regular_price",
    "sale_price", "volume_deals_description", "volume_deals_price",
    "digital_deals_description", "digital_deals_price", "image_url", "url",
    "upc", "gupc", "ppm", "unit_price", "crawl_date"
]
APPROX_RE = re.compile(r"\b(approx(?:imately)?\.?|about|estimated|est\.?)\b", re.I)
PRICE_RE = re.compile(r"^\s*\$?\s*(\d{1,5}(?:,\d{3})*(?:\.\d{1,2})?)\s*$")
DIGITAL_RE = re.compile(
    r"\b(digital coupon|clip(?:ped)? coupon|clip(?:ped)?|activate|digital offer|"
    r"with digital coupon|save to card|coupon must be clipped)\b", re.I
)
VOLUME_RE = re.compile(
    r"\b(\d+\s*for\s*\$?\s*\d|buy\s*\d+.{0,25}(get|free)|bogo|buy one get one|"
    r"mix\s*(?:or|&)\s*match|must buy\s*\d+|when you buy\s*\d+|save\s*\$?\d+\s*when you buy)\b", re.I
)
PRIVATE_LABELS = (
    "private selection", "simple truth", "kroger", "heritage farm", "big k",
    "mariano's", "marianos", "home chef", "comforts", "fry's", "fred meyer"
)


def blank_record() -> dict[str, str]:
    return {field: "" for field in FIELDS}


def clean_text(value: Any) -> str:
    if value is None or isinstance(value, (dict, list)):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def numeric_price(value: Any) -> str:
    """Normalize a single displayed price; reject deal strings such as '2 for $5'."""
    s = clean_text(value)
    if not s or re.search(r"\b(for|buy|get|save|coupon|off)\b", s, re.I):
        return ""
    match = PRICE_RE.fullmatch(s)
    if not match:
        # Allow a clearly displayed price followed by a per-item qualifier.
        match = re.fullmatch(r"\s*(\$\s*\d{1,5}(?:,\d{3})*(?:\.\d{1,2})?)\s*(?:each|ea\.?|/each)\s*", s, re.I)
        if not match:
            return ""
        token = match.group(1).replace("$", "").replace(" ", "")
    else:
        token = match.group(1)
    try:
        return f"{float(token.replace(',', '')):.2f}"
    except ValueError:
        return ""


def is_approx_weight(weight: str) -> bool:
    return bool(APPROX_RE.search(weight or ""))


def classify_deals(text: str) -> tuple[str, str]:
    """Return (volume-deal text, digital-deal text); never infer a deal price."""
    s = clean_text(text)
    if not s:
        return "", ""
    if DIGITAL_RE.search(s):
        return "", s
    if VOLUME_RE.search(s):
        return s, ""
    return "", ""


def is_private_label(title: str) -> str:
    lowered = clean_text(title).casefold()
    return "yes" if any(label in lowered for label in PRIVATE_LABELS) else "no"


def product_key(record: dict[str, str]) -> str:
    upc = record.get("upc", "").strip()
    url = record.get("url", "").strip()
    title = record.get("product_title", "").strip().casefold()
    return f"upc:{upc}" if upc else (f"url:{url}" if url else f"title:{title}")



def verify_store_identity(visible_text: str, location: dict[str, Any]) -> bool:
    """Fail-closed identity check: require both configured store name and address in visible text."""
    def norm(value: Any) -> str:
        return re.sub(r"[^a-z0-9]+", "", clean_text(value).casefold())

    visible = norm(visible_text)
    expected_name = norm(location.get("store_name"))
    expected_address = norm(location.get("store_location"))
    if not expected_name or not expected_address or not visible:
        return False
    return expected_name in visible and expected_address in visible


def validate_records(records: list[dict[str, str]], category_stats: list[dict[str, Any]],
                     cfg: dict[str, Any], last_success_path=None) -> dict[str, Any]:
    counts = Counter(r.get("category", "") for r in records)
    keys = [product_key(r) for r in records]
    duplicates = len(keys) - len(set(keys))
    bad_order = any(list(r.keys()) != FIELDS for r in records)
    nulls = sum(1 for r in records for v in r.values() if v is None)
    bad_value_types = sum(1 for r in records for v in r.values() if not isinstance(v, str))
    required_nonempty = ["category", "product_title", "url", "crawl_date"]
    invalid_required = sum(1 for r in records if any(not r.get(k, "").strip() for k in required_nonempty))
    bad_gupc_or_unit = sum(1 for r in records if r.get("gupc") != "" or r.get("unit_price") != "")
    bad_approx = sum(1 for r in records if is_approx_weight(r.get("weight", "")))
    minimum = int(cfg.get("min_records_for_publish", 500))
    min_categories = int(cfg.get("min_categories_with_products", 3))
    categories_with_products = sum(1 for _, count in counts.items() if count > 0)
    failed_categories = [s.get("category", "") for s in category_stats if s.get("status") != "ok"]
    ratio_ok = True
    previous_count = None
    if last_success_path and last_success_path.exists():
        try:
            import json
            old = json.loads(last_success_path.read_text(encoding="utf-8"))
            previous_count = len(old) if isinstance(old, list) else None
            if previous_count:
                ratio_ok = len(records) >= previous_count * float(cfg.get("minimum_yield_ratio_vs_last_success", 0.55))
        except Exception:
            ratio_ok = False
    all_categories_ok = bool(category_stats) and all(s.get("status") == "ok" for s in category_stats)
    location_verification_ok = bool(category_stats) and all(
        s.get("store_evidence", {}).get("selected_store_verified") is True for s in category_stats
    )
    passed = (
        len(records) >= minimum and categories_with_products >= min_categories
        and duplicates == 0 and nulls == 0 and bad_value_types == 0 and not bad_order
        and invalid_required == 0 and bad_gupc_or_unit == 0 and bad_approx == 0
        and ratio_ok and all_categories_ok and location_verification_ok
    )
    return {
        "total_records": len(records), "unique_products": len(set(keys)),
        "duplicate_count": duplicates, "category_counts": dict(sorted(counts.items())),
        "categories_configured": len(category_stats), "categories_with_products": categories_with_products,
        "categories_not_ok": failed_categories, "invalid_required_records": invalid_required,
        "null_value_count": nulls, "non_string_value_count": bad_value_types,
        "field_order_ok": not bad_order, "gupc_and_unit_price_empty": bad_gupc_or_unit == 0,
        "approx_weight_exclusions_ok": bad_approx == 0,
        "location_verification_ok": location_verification_ok,
        "previous_success_count": previous_count if previous_count is not None else "",
        "yield_ratio_check_ok": ratio_ok, "publish_threshold_records": minimum,
        "validation_passed": passed,
    }
