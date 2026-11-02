"""Schema and pure validation helpers for the Mariano's scraper.

Kept independent of Playwright so unit tests can run without a browser installation.
"""
from __future__ import annotations

import json
import re
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

FIELDS = [
    "zipcode", "store_name", "store_location", "store_logo", "store_brand",
    "category", "sub_category", "product_title", "weight", "regular_price",
    "sale_price", "volume_deals_description", "volume_deals_price",
    "digital_deals_description", "digital_deals_price", "image_url", "url",
    "upc", "gupc", "ppm", "unit_price", "crawl_date"
]
APPROX_RE = re.compile(
    r"\b("
    r"approx(?:imate(?:ly)?)?\.?|"
    r"estimated\s+(?:weight|wt\.?|size)|"
    r"est(?!\.?\s*(?:17|18|19|20)\d{2}\b)\.?\s*(?:wt\.?|weight)|"
    r"variable(?:\s+(?:weight|price|size))?|"
    r"random\s+weight|"
    r"avg\.?\s*wt\.?|"
    r"average\s+weight|"
    r"(?:weight|size)\s+varies|"
    r"varies\s+by\s+weight|"
    r"sold\s+by\s+weight|"
    r"catch\s+weight|"
    r"priced\s+by\s+weight|"
    r"weighed\s+(?:in|at)\s+(?:store|scale|checkout|register)|"
    r"weighted\s+(?:at|in)\s+(?:scale|checkout|register|store)|"
    r"weighted\s+item"
    r")(?:\b|\s|$)",
    re.I
)
EACH_EST_RE = re.compile(
    r"(?:–|-|/|\()\s*each\b|\beach\b",
    re.I
)
FIXED_CONTAINER_RE = re.compile(
    r"\b(bag|pack|pk|tray|box|carton|container|bunch|clam(?:shell)?|bottle|can|jar|tub|jug|multipack|case)\b",
    re.I
)
WEIGHT_UNIT_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:lb|lbs|pound|pounds|oz|ounce|ounces|kg|g)\b",
    re.I
)
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
    "mariano's", "marianos", "roundy's", "roundys", "home chef", "comforts", "fry's", "fred meyer"
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


def is_approx_weight(weight: str, *product_text: str) -> bool:
    """Detect approximate, variable-weight, or estimated piece-weight items.

    Checks weight, product title, descriptions, size fields, and packaging info.
    Preserves legitimate fixed-weight packs, multipacks, and non-approximate items
    such as 'weighted blanket' or foundation years ('Est. 1920').
    """
    clean_w = clean_text(weight)
    all_texts = [clean_text(v) for v in product_text if clean_text(v)]
    combined = " ".join([clean_w, *all_texts])
    if not combined:
        return False

    # Check for sold-by-weight indicator in extra parameters (e.g. soldBy: Weight)
    for text in all_texts:
        s = text.lower()
        if s in ("weight", "random", "variable") or s.startswith("soldby:weight") or "sold by weight" in s:
            return True

    # In weight field specifically, check approx/about/variable prefixes
    if clean_w:
        if re.search(r"\b(approx(?:imate(?:ly)?)?\.?|about|est(?:imated)?\.?|avg\.?\s*wt\.?|varies)\b", clean_w, re.I):
            if not re.search(r"\best\.?\s*(?:17|18|19|20)\d{2}\b", clean_w, re.I):
                return True

    # Check explicit approx/variable/random weight keywords in combined text
    if APPROX_RE.search(combined):
        return True

    # Check produce or items sold by "each" with an estimated single-item weight
    # e.g., "Large Honeycrisp Apple – Each", "Large Granny Smith Apple - Each" with size "1 lb"
    # These represent estimated per-item weights, not sealed fixed-weight containers.
    if clean_w and WEIGHT_UNIT_RE.search(clean_w):
        if not FIXED_CONTAINER_RE.search(clean_w):
            title_text = " ".join(all_texts)
            if EACH_EST_RE.search(title_text) and not FIXED_CONTAINER_RE.search(title_text):
                return True

    return False


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
    """Store-scoped product identity; the same UPC at different stores is valid."""
    upc = clean_text(record.get("upc", ""))
    url = clean_text(record.get("url", ""))
    title = clean_text(record.get("product_title", "")).casefold()
    store = "|".join(clean_text(record.get(k, "")).casefold() for k in
                     ("zipcode", "store_name", "store_location"))
    identity = f"upc:{upc}" if upc else (f"url:{url}" if url else f"title:{title}")
    return f"store:{store}|{identity}"



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


def validate_records(
    records: list[dict[str, str]],
    category_stats: list[dict[str, Any]],
    cfg: dict[str, Any],
    last_success_path=None,
    evidence_path: Path | None = None,
) -> dict[str, Any]:
    counts = Counter(clean_text(r.get("category", "")) for r in records)
    keys = [product_key(r) for r in records]
    duplicates = len(keys) - len(set(keys))
    bad_order = any(list(r.keys()) != FIELDS for r in records)
    nulls = sum(1 for r in records for v in r.values() if v is None)
    bad_value_types = sum(1 for r in records for v in r.values() if not isinstance(v, str))
    required_nonempty = ["category", "product_title", "url", "crawl_date"]
    invalid_required = sum(
        1 for r in records
        if any(not isinstance(r.get(k), str) or not r.get(k, "").strip() for k in required_nonempty)
    )
    bad_gupc_or_unit = sum(1 for r in records if r.get("gupc") != "" or r.get("unit_price") != "")
    bad_approx = sum(
        1 for r in records
        if is_approx_weight(clean_text(r.get("weight", "")), clean_text(r.get("product_title", "")))
    )
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

    # Coverage and pagination validation
    VALID_TERM_TERMINAL_STATUSES = {"exhausted", "stopped_by_api_limit", "incomplete", "failed"}
    VALID_TERM_STOP_REASONS = {"exhausted", "api_limit_reached", "premature_truncation", "api_error", "offset_limit", "proof_target_met", "repeated_page"}

    terms_skipped_count = sum(s.get("terms_skipped", 0) for s in category_stats)
    terms_failed_count = sum(s.get("terms_failed", 0) for s in category_stats)
    pagination_incomplete_categories = [
        s.get("category", "") for s in category_stats
        if s.get("pagination_complete") is False and not s.get("api_limit_reached") and s.get("coverage_status") != "proof_only"
    ]

    unattempted_or_undocumented_terms_categories: list[str] = []
    invalid_coverage_claims: list[str] = []
    partial_or_failed_categories: list[str] = []

    for s in category_stats:
        cat_title = s.get("category", "")
        cov_status = s.get("coverage_status", "")
        cov_complete = s.get("coverage_complete")
        terms_exp = s.get("terms_expected", 0)
        terms_att = s.get("terms_attempted", 0)
        details = s.get("term_details", [])

        # 1. Enforce actual search-term coverage: every expected term must be attempted and documented
        if terms_exp > 0:
            if terms_att < terms_exp or s.get("terms_skipped", 0) > 0:
                unattempted_or_undocumented_terms_categories.append(cat_title)
            elif details:
                for td in details:
                    t_stat = td.get("status")
                    t_stop = td.get("stop_reason")
                    if t_stat not in VALID_TERM_TERMINAL_STATUSES or t_stop not in VALID_TERM_STOP_REASONS:
                        unattempted_or_undocumented_terms_categories.append(cat_title)
                        break

        # 2. A category must never be marked complete merely because its product quota was reached!
        # If complete is claimed, verify all terms were exhausted and none were failed, skipped, or limited
        if cov_complete is True or cov_status == "complete":
            has_unexhausted_terms = any(td.get("stop_reason") != "exhausted" for td in details) if details else False
            if (
                s.get("api_limit_reached") is True
                or s.get("terms_skipped", 0) > 0
                or s.get("terms_failed", 0) > 0
                or s.get("pagination_complete") is False
                or s.get("status") != "ok"
                or has_unexhausted_terms
            ):
                invalid_coverage_claims.append(cat_title)

        # 3. Distinguish complete, partial, failed, and API-limited coverage
        if cov_status in ("partial", "failed", "interrupted") or s.get("status") in ("incomplete", "failed", "interrupted"):
            partial_or_failed_categories.append(cat_title)
        elif cov_status == "limited_by_api":
            # API-limited coverage must NOT claim coverage_complete=True
            if cov_complete is True:
                invalid_coverage_claims.append(cat_title)
        elif cov_status == "proof_only":
            # Proof-only mode is clearly distinguished from a full production collection
            # It must never falsely claim coverage_complete=True
            if cov_complete is True:
                invalid_coverage_claims.append(cat_title)

    # Traceability evidence check
    evidence_references_ok = True
    if evidence_path is not None and evidence_path.exists():
        try:
            import json
            from datetime import date
            today_str = date.today().isoformat()
            evidence_upcs: set[str] = set()
            evidence_keys: set[str] = set()
            with evidence_path.open("r", encoding="utf-8") as ef:
                for line in ef:
                    line = line.strip()
                    if not line:
                        continue
                    ev_item = json.loads(line)
                    if ev_item.get("upc"):
                        evidence_upcs.add(clean_text(ev_item["upc"]))
                    if ev_item.get("product_key"):
                        evidence_keys.add(ev_item["product_key"])
                    if ev_item.get("evidence_id"):
                        evidence_keys.add(ev_item["evidence_id"])
            for r in records:
                if r.get("crawl_date") == today_str:
                    upc = clean_text(r.get("upc", ""))
                    pkey = product_key(r)
                    if upc and upc not in evidence_upcs and pkey not in evidence_keys:
                        evidence_references_ok = False
                        break
        except Exception:
            evidence_references_ok = False

    passed = (
        len(records) >= minimum and categories_with_products >= min_categories
        and duplicates == 0 and nulls == 0 and bad_value_types == 0 and not bad_order
        and invalid_required == 0 and bad_gupc_or_unit == 0 and bad_approx == 0
        and ratio_ok and all_categories_ok and location_verification_ok
        and terms_skipped_count == 0 and terms_failed_count == 0
        and not pagination_incomplete_categories
        and not unattempted_or_undocumented_terms_categories
        and not invalid_coverage_claims
        and not partial_or_failed_categories
        and evidence_references_ok
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
        "terms_skipped_count": terms_skipped_count,
        "terms_failed_count": terms_failed_count,
        "pagination_incomplete_categories": pagination_incomplete_categories,
        "incorrectly_marked_complete_categories": invalid_coverage_claims,
        "unattempted_or_undocumented_terms_categories": unattempted_or_undocumented_terms_categories,
        "invalid_coverage_claims": invalid_coverage_claims,
        "partial_or_failed_categories": partial_or_failed_categories,
        "evidence_references_ok": evidence_references_ok,
        "previous_success_count": previous_count if previous_count is not None else "",
        "yield_ratio_check_ok": ratio_ok, "publish_threshold_records": minimum,
        "validation_passed": passed,
    }


def replace_category_records(
    existing_records: list[dict[str, str]],
    category_name: str,
    new_records: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Merge a recrawled category without deleting historically observed products.

    Fresh observations replace older versions of the same store-scoped product.
    Products absent from the new crawl remain with their original crawl_date.
    Approximate-weight products are excluded from both sources.
    If a product moved from another category, it is removed from the old category
    so it appears only once in the final catalog.
    """
    fresh_category = [
        record for record in new_records
        if not is_approx_weight(
            record.get("weight", ""),
            record.get("product_title", ""),
        )
    ]
    fresh_keys = {product_key(record) for record in fresh_category}

    # Preserved other categories: keep historical items from other categories UNLESS
    # the product was freshly observed in the current category (handles category movement)
    preserved_other_categories = [
        record for record in existing_records
        if record.get("category") != category_name
        and product_key(record) not in fresh_keys
    ]

    previous_category = [
        record for record in existing_records
        if record.get("category") == category_name
        and not is_approx_weight(
            record.get("weight", ""),
            record.get("product_title", ""),
        )
    ]

    merged: dict[str, dict[str, str]] = {}

    # Keep historical records first. Their original crawl_date is preserved.
    for record in previous_category:
        merged.setdefault(product_key(record), record)

    # Fresh observations take precedence for matching store-scoped identities.
    for record in fresh_category:
        merged[product_key(record)] = record

    return preserved_other_categories + list(merged.values())


def load_previous_catalog(path: Path) -> list[dict[str, str]]:
    """Load a previously published catalog or fail closed if its structure is unsafe."""
    if not path.exists():
        return []

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(
            f"Previous catalog is unreadable; refusing to start: {type(exc).__name__}"
        ) from exc

    if not isinstance(data, list) or not data:
        raise RuntimeError("Previous catalog must be a non-empty JSON list; refusing to start.")

    retained: list[dict[str, str]] = []
    required = (
        "zipcode", "store_name", "store_location", "category",
        "product_title", "url", "crawl_date",
    )

    for index, record in enumerate(data):
        if not isinstance(record, dict) or list(record.keys()) != FIELDS:
            raise RuntimeError(
                f"Previous catalog record {index} does not match the exact 22-field schema."
            )
        if any(not isinstance(value, str) for value in record.values()):
            raise RuntimeError(
                f"Previous catalog record {index} contains a non-string value."
            )
        if any(not record.get(field, "").strip() for field in required):
            raise RuntimeError(
                f"Previous catalog record {index} is missing a required value."
            )
        if record["gupc"] != "" or record["unit_price"] != "":
            raise RuntimeError(
                f"Previous catalog record {index} violates the blank gupc/unit_price rule."
            )

        # Approximate-weight products are excluded, not retained as historical products.
        if not is_approx_weight(record["weight"], record["product_title"]):
            retained.append(record)

    keys = [product_key(record) for record in retained]
    if len(keys) != len(set(keys)):
        raise RuntimeError("Previous catalog contains duplicate store-scoped product identities.")

    return retained


def location_record_matches(record: dict[str, str], location: dict[str, Any]) -> bool:
    """Match a catalog record to a configured store using safe normalized store fields."""
    def norm(text: Any) -> str:
        return re.sub(r"[^a-z0-9]", "", clean_text(text).lower())

    rec_zip = clean_text(record.get("zipcode"))[:5]
    loc_zip = clean_text(location.get("zipcode"))[:5]
    if rec_zip and loc_zip and rec_zip != loc_zip:
        return False

    rec_name = norm(record.get("store_name"))
    loc_name = norm(location.get("store_name"))
    if rec_name and loc_name and rec_name != loc_name:
        return False

    rec_loc = norm(record.get("store_location"))
    loc_loc = norm(location.get("store_location"))
    if not rec_loc or not loc_loc:
        return False

    # Check normalized full address or street component match
    if rec_loc == loc_loc or rec_loc in loc_loc or loc_loc in rec_loc:
        return True

    # Check primary street component (e.g. "1720 N Milwaukee Ave")
    raw_loc = clean_text(location.get("store_location"))
    raw_rec = clean_text(record.get("store_location"))
    street_loc = norm(raw_loc.split(",")[0]) if "," in raw_loc else loc_loc
    street_rec = norm(raw_rec.split(",")[0]) if "," in raw_rec else rec_loc
    return bool(street_loc and street_rec and (street_loc == street_rec or street_loc in street_rec or street_rec in street_loc))


def apply_category_result(
    existing_records: list[dict[str, str]],
    category_name: str,
    new_records: list[dict[str, str]],
    status: str,
) -> list[dict[str, str]]:
    """Only a fully successful category crawl may replace historical observations."""
    if status != "ok":
        return [
            record for record in existing_records
            if not is_approx_weight(record.get("weight", ""), record.get("product_title", ""))
        ]
    return replace_category_records(existing_records, category_name, new_records)


def publish_staging_file(staging_path: Path, final_path: Path) -> None:
    """Atomically publish a completed file while preserving the previous output."""
    publish_temp = final_path.with_name(final_path.name + ".tmp")
    try:
        shutil.copy2(staging_path, publish_temp)
        publish_temp.replace(final_path)
    finally:
        if publish_temp.exists():
            publish_temp.unlink()


def publish_catalog_pair(
    staging_json_path: Path,
    final_json_path: Path,
    staging_csv_path: Path,
    final_csv_path: Path,
) -> None:
    """Coordinate publication of both JSON and CSV catalogs together with automatic rollback.

    Note on Crash Atomicity and Limitations:
    On standard POSIX and Windows filesystems, atomic rename applies strictly to individual
    filesystem entries. A two-file operation across distinct JSON and CSV targets cannot be
    mathematically atomic in the event of an unrecoverable kernel panic, hardware power cut,
    or SIGKILL arriving between the replacement of the first and second file.
    To provide the highest legitimate reliability:
    1. Pre-publication backups (.bak) are created before touching either target file.
    2. If publishing the CSV fails, the JSON file is rolled back to its previous state.
    3. If rollback itself encounters an I/O error, the pre-publication backups (.bak) are
       strictly preserved on disk so that operator or automated recovery remains possible.
    4. Backups are deleted ONLY after both files have been verified to exist, be non-empty,
       and represent a consistent state.
    """
    if not staging_json_path.exists() or not staging_csv_path.exists():
        raise FileNotFoundError("Both staging JSON and CSV files must exist before publication.")

    backup_json = final_json_path.with_name(final_json_path.name + ".bak")
    backup_csv = final_csv_path.with_name(final_csv_path.name + ".bak")
    json_backed_up = False
    csv_backed_up = False

    # 1. Back up existing published files before any modifications
    if final_json_path.exists():
        shutil.copy2(final_json_path, backup_json)
        json_backed_up = True
    if final_csv_path.exists():
        shutil.copy2(final_csv_path, backup_csv)
        csv_backed_up = True

    published_consistently = False

    try:
        # Step A: Publish JSON catalog
        try:
            publish_staging_file(staging_json_path, final_json_path)
        except Exception as json_err:
            rollback_err = None
            if json_backed_up and backup_json.exists():
                try:
                    shutil.copy2(backup_json, final_json_path)
                except Exception as r_err:
                    rollback_err = r_err
            if rollback_err is not None:
                raise RuntimeError(
                    f"CRITICAL: Failed to publish JSON ({json_err}) AND rollback of JSON failed ({rollback_err}). "
                    f"Previous backup preserved on disk at {backup_json}."
                ) from json_err
            raise RuntimeError(
                f"Failed to publish JSON ({json_err}); publication aborted, previous files preserved."
            ) from json_err

        # Step B: Publish CSV catalog
        try:
            publish_staging_file(staging_csv_path, final_csv_path)
        except Exception as csv_err:
            rollback_err = None
            if json_backed_up and backup_json.exists():
                try:
                    shutil.copy2(backup_json, final_json_path)
                except Exception as r_err:
                    rollback_err = r_err
            elif final_json_path.exists():
                try:
                    final_json_path.unlink()
                except Exception as r_err:
                    rollback_err = r_err

            if csv_backed_up and backup_csv.exists():
                try:
                    shutil.copy2(backup_csv, final_csv_path)
                except Exception as r_err_csv:
                    if rollback_err is None:
                        rollback_err = r_err_csv

            if rollback_err is not None:
                raise RuntimeError(
                    f"CRITICAL: Failed to publish CSV ({csv_err}) AND rollback failed ({rollback_err}). "
                    f"Previous backups preserved on disk at {backup_json} and {backup_csv}."
                ) from csv_err

            raise RuntimeError(
                f"Failed to publish CSV ({csv_err}); rolled back JSON to maintain consistent pair."
            ) from csv_err

        # Step C: Confirm that both published files exist and are non-empty
        if not final_json_path.exists() or final_json_path.stat().st_size == 0 or not final_csv_path.exists() or final_csv_path.stat().st_size == 0:
            rollback_err = None
            if json_backed_up and backup_json.exists():
                try:
                    shutil.copy2(backup_json, final_json_path)
                except Exception as e:
                    rollback_err = e
            if csv_backed_up and backup_csv.exists():
                try:
                    shutil.copy2(backup_csv, final_csv_path)
                except Exception as e:
                    if rollback_err is None:
                        rollback_err = e
            if rollback_err is not None:
                raise RuntimeError(
                    f"CRITICAL: Published file validation failed and rollback failed ({rollback_err}). "
                    f"Backups preserved at {backup_json} and {backup_csv}."
                )
            raise RuntimeError("Final catalog pair validation failed: missing or empty file after publication; rolled back to previous files.")

        published_consistently = True

    finally:
        # Never delete backup files until the final pair is confirmed consistent!
        if published_consistently:
            if backup_json.exists():
                try:
                    backup_json.unlink()
                except Exception:
                    pass
            if backup_csv.exists():
                try:
                    backup_csv.unlink()
                except Exception:
                    pass


