# Independent Code Review: Mariano's #78 Scraper & API Collector

**Repository:** `MMansy2010/Mariano-s-Product-Scraper-for-Azure`  
**Branch:** `fix/darren-review-2026-10`  
**Current HEAD Commit:** `ab961aa4f8fedd931b6c8797daf7d1d61e88d0a7`  
**Target Store:** Mariano's, 1720 N Milwaukee Ave, Vernon Hills, IL 60061 (`53100502`)  
**Review Date:** 2026-11-01  
**Review Status:** **REJECTED (DO NOT SEND TO DARREN IN CURRENT STATE)**

---

## Executive Summary

An independent, line-by-line inspection of the Git diff against HEAD `ab961aa` was conducted. While schema validation, store verification, and atomic file publication logic are well-engineered, **critical defects were identified in historical product retention, test validity, and WAF compliance statements.**

Most notably:
1. **Critical Defect in Collector Retention:** When `previous_records` is passed to `collect_kroger_catalog`, `seen_keys` and `seen_upcs` are initialized with all historical items. As a result, **every existing product is dropped as a duplicate during fresh runs**, preventing price/promo/date updates, causing empty categories, and causing validation to fail.
2. **Category Reclassification Duplicate Defect:** If a product changes category across crawls, `replace_category_records` keeps the old category record while adding the new category record, triggering a duplicate product key failure in `validate_records` and aborting publication.
3. **Inaccurate / Prohibited WAF Recommendations in Audit:** `DATA_SOURCE_AUDIT.md` recommended "residential proxies" and "commercial proxy services" to bypass Akamai WAF, directly violating strict safety rules.
4. **Hollow Test:** `test_resume_rejects_completed_checkpoint` was found to be a hollow test asserting that a set is a subset of itself without executing any application code.

Below is the detailed area-by-area breakdown.

---

## Area-by-Area Review

### 1. Historical Product Retention: FAIL

#### Exact Code Locations
- [src/kroger_collector.py#L555-L556](src/kroger_collector.py#L555-L556)
- [src/kroger_collector.py#L639-L640](src/kroger_collector.py#L639-L640)
- [src/kroger_collector.py#L676-L678](src/kroger_collector.py#L676-L678)
- [src/schema.py#L180-L222](src/schema.py#L180-L222) (`replace_category_records`)
- [src/schema.py#L225-L236](src/schema.py#L225-L236) (`location_record_matches`)

#### Concrete Failure Scenarios

1. **Stale Observation & Category Failure due to Deduplication against History:**
   - In `collect_kroger_catalog` (lines 555–556):
     ```python
     seen_keys: set[str] = {product_key(r) for r in all_records}
     seen_upcs: set[str] = {clean_text(r.get("upc")) for r in all_records if clean_text(r.get("upc"))}
     ```
   - When `previous_records` is passed in (line 831 from `load_previous_catalog(latest_path)`), `all_records` contains all prior products.
   - During the search loop (lines 639–640):
     ```python
     if (upc and upc in seen_upcs) or (key in seen_keys):
         continue
     ```
   - **Impact:** Any product already present in `previous_records` is dropped as a duplicate. Consequently:
     - Fresh prices, promotional discounts, and current `crawl_date`s are **never** updated for existing products.
     - If a category contains only previously collected products (no new items), `cat_records` remains empty (`[]`).
     - Line 676–678 triggers: `cat_status = "incomplete"`.
     - `validate_records` fails closed because `categories_not_ok` is non-empty.
     - **Result:** Fresh runs fail validation and reject publication whenever all products in any category were previously seen.

2. **Duplicate Key Failure upon Category Reclassification:**
   - In `replace_category_records` (lines 190–222):
     ```python
     preserved_other_categories = [
         record for record in existing_records
         if record.get("category") != category_name
     ]
     ```
   - `product_key` is strictly `{store}|upc:{upc}` and does not include the category name.
   - If a product was previously categorized under `"Dairy"`, but in a subsequent crawl it is returned under `"Produce"` or `"Organic"`:
     - `preserved_other_categories` retains the old record under `"Dairy"`.
     - `merged` contains the fresh record under `"Produce"`.
     - Both records share the exact same `product_key`.
     - In `validate_records`: `duplicates = len(keys) - len(set(keys))`. Since `duplicates > 0`, `validation_passed` is set to `False`.
     - **Result:** A simple taxonomy reclassification corrupts catalog validation and aborts publication.

3. **Store Location String Mismatch Discards Entire Historical Catalog:**
   - In `location_record_matches` (lines 225–236):
     ```python
     return all(left and right and left.casefold() == right.casefold() for left, right in expected)
     ```
   - Performs a rigid case-insensitive equality check on `store_location`.
   - If historical records have minor punctuation variations (e.g. `"1720 N. Milwaukee Ave., Vernon Hills, IL 60061"` vs `"1720 N Milwaukee Ave, Vernon Hills, IL 60061"`), `location_record_matches` returns `False`.
   - **Result:** `historical_location_records` becomes empty, silently discarding the entire historical catalog without warning.

---

### 2. API Field Claims: NEEDS EVIDENCE / PARTIAL FAIL

#### Exact Code Locations
- [DATA_SOURCE_AUDIT.md#Section-2](DATA_SOURCE_AUDIT.md)
- [DATA_SOURCE_AUDIT.md#Section-3](DATA_SOURCE_AUDIT.md)
- [DATA_SOURCE_AUDIT.md#Section-6](DATA_SOURCE_AUDIT.md)
- [data/evidence/sample_raw_products.json#L34-L39](data/evidence/sample_raw_products.json#L34-L39)

#### Inaccurate & Overstated Claims in `DATA_SOURCE_AUDIT.md`

1. **Conflating "Absent in Test Responses" with "Officially Unsupported by API":**
   - `DATA_SOURCE_AUDIT.md` asserts in Section 6: *"Any supplementary fields not present in product.compact (PPM, loyalty coupons) should be formally documented as unsupported by the upstream API"*.
   - **Correction:** The available evidence only demonstrates that PPM, volume deals, and digital deals were absent in the specific sample responses captured under the `product.compact` scope. No official Kroger API documentation, OpenAPI contract, or technical deprecation notice has been provided to substantiate that Kroger's API is fundamentally incapable of returning promotional or pricing breakdown data. They must be characterized as *"Absent from captured responses in the inspected test dataset"*, not *"officially unsupported by the upstream API"*.

2. **Inaccurate Claim on Fulfillment Evidence:**
   - `DATA_SOURCE_AUDIT.md` Section 3 claims that store-specific fulfillment evidence cannot be reliably obtained.
   - **Correction:** [data/evidence/sample_raw_products.json#L34-L39](data/evidence/sample_raw_products.json#L34-L39) proves that Kroger's API does return explicit boolean fulfillment indicators per item:
     ```json
     "fulfillment": {
       "instore": true,
       "curbside": true,
       "delivery": true
     }
     ```
   - Conflating fulfillment flags with stock inventory depth is inaccurate.

3. **Field Mapping Exactness:**
   - All 22 output fields correctly specify source paths.
   - Inferred fields (`ppm`, `volume_deals_*`, `digital_deals_*`) are correctly written as empty strings `""` in code, but the audit document must clearly delineate live proof from absence in local evidence.

---

### 3. WAF and Access: FAIL

#### Exact Code Locations
- [DATA_SOURCE_AUDIT.md#Section-1.2](DATA_SOURCE_AUDIT.md)
- [DATA_SOURCE_AUDIT.md#Section-5](DATA_SOURCE_AUDIT.md)
- [DATA_SOURCE_AUDIT.md#Section-6](DATA_SOURCE_AUDIT.md)

#### Non-Compliant Recommendations

- `DATA_SOURCE_AUDIT.md` contains multiple recommendations proposing proxy workarounds:
  - Section 1.2: *"without authenticated residential proxy infrastructure"*
  - Section 5: *"unless routed through an authorized egress IP pool or commercial proxy service"*
  - Section 6: *"without approved residential access"*
- **Review Finding:** Proposing residential proxies or egress evasion is an explicit violation of the review safety instructions (*"Remove any recommendation involving residential proxies, stealth, or bypassing access controls. Describe the current browser access failure factually. Do not suggest bypass methods."*).
- **Factual Reality:** Datacenter IP addresses connecting to `www.marianos.com` are blocked at the TLS/HTTP2 layer by Akamai Bot Manager (`net::ERR_HTTP2_PROTOCOL_ERROR` or HTTP 403 Forbidden). The browser scraper in `src/scraper.py` is currently inoperable in automated cloud environments (such as Azure Container Apps).

---

### 4. Store Identity: PASS (with minor caveats)

#### Exact Code Locations
- [src/kroger_collector.py#L290-L352](src/kroger_collector.py#L290-L352) (`verify_location`)
- [src/scraper.py#L255-L278](src/scraper.py#L255-L278) (`current_location_proof`)
- [src/schema.py#L106-L122](src/schema.py#L106-L122) (`verify_store_identity`)
- [src/schema.py#L154](src/schema.py#L154) (`validate_records` location check)

#### Analysis
- **Kroger Collector (`src/kroger_collector.py`):**
  - Live query issued to `GET /v1/locations?filter.zipCode.near=60061`.
  - Strictly requires `locationId == "53100502"`.
  - Performs cross-verification of address line 1 (`1720 N Milwaukee Ave`), city (`Vernon Hills`), state (`IL`), and zip code (`60061`).
  - Fails closed (`KrogerAPIError`) before executing any product queries if the location is missing or mismatches.
  - Generates evidence artifact `data/evidence/location_53100502.json` and attaches proof to category metadata.
- **Browser Scraper (`src/scraper.py`):**
  - Scrapes visible banner/header text and verifies presence of store name and street address.
  - *Caveat:* Does not verify internal retailer store ID `53100502` directly in the browser DOM (as the website header only renders the human-readable brand name and address).
- **Validation:**
  - `validate_records` requires `selected_store_verified is True` on all category statistics.

---

### 5. Fresh Run and Recovery: PASS (with notes)

#### Exact Code Locations
- [src/kroger_collector.py#L807-L823](src/kroger_collector.py#L807-L823)
- [src/kroger_collector.py#L720-L745](src/kroger_collector.py#L720-L745)
- [src/scraper.py#L1038-L1105](src/scraper.py#L1038-L1105)

#### Analysis
- **Fresh Scheduled Runs:**
  - In scheduled execution (without `--resume`), `resume_checkpoint` remains `None`.
  - The collector executes all configured categories and search terms from the beginning.
- **Completed Checkpoint Handling:**
  - In `kroger_collector.py:815-816`: If a user supplies `--resume` but the checkpoint has all categories marked `status == "ok"`, it logs `resume_rejected_completed_checkpoint` and falls back to a fresh run.
  - *Observation:* While this prevents erroneously reusing a completed run as a partial run, Darren's specification states: *"Never mistake a completed checkpoint for a new run."* Falling back to a fresh run without failing explicitly or clearing the checkpoint could cause ambiguity in scripted environments.
- **Interruption Behavior:**
  - Traps `KeyboardInterrupt` / `CancelledError` to write the in-flight category status as `interrupted` atomically.
  - Resumption skips `ok` categories and cleanly re-crawls incomplete or interrupted categories.

---

### 6. Atomic Publication: PASS

#### Exact Code Locations
- [src/schema.py#L265-L275](src/schema.py#L265-L275) (`publish_staging_file`)
- [src/schema.py#L235-L264](src/schema.py#L235-L264) (`load_previous_catalog`)
- [src/kroger_collector.py#L868-L877](src/kroger_collector.py#L868-L877)
- [src/scraper.py#L1213-L1221](src/scraper.py#L1213-L1221)

#### Analysis
- `publish_staging_file` creates a temporary file `final_path.name + ".tmp"` and executes `publish_temp.replace(final_path)`. Under Windows (Python 3.3+), `Path.replace()` invokes `MoveFileExW` with `MOVEFILE_REPLACE_EXISTING`, providing atomic replacement.
- Cleanup is guaranteed via `finally: if publish_temp.exists(): publish_temp.unlink()`.
- If `validation["validation_passed"]` is `False`, publication is completely skipped, exit code 2 is returned, and the previous production catalog `marianos_latest.json` is left untouched.
- `load_previous_catalog` validates the integrity of existing production catalogs (checking exact 22 fields, string types, required non-empty values, blank `gupc`/`unit_price`, and duplicate keys). If corrupt, it raises `RuntimeError` immediately rather than attempting an unsafe merge.

---

### 7. Test Quality: FAIL / NEEDS IMPROVEMENT

#### Exact Code Locations
- [tests/test_kroger_collector.py#L904-L928](tests/test_kroger_collector.py#L904-L928) (`test_resume_rejects_completed_checkpoint`)

#### Ineffective & Hollow Tests Found

1. **Hollow Assertion in Checkpoint Rejection Test:**
   - In `tests/test_kroger_collector.py` lines 904–928:
     ```python
     def test_resume_rejects_completed_checkpoint(tmp_path, mock_location):
         ...
         saved = json.loads(cp_path.read_text(encoding="utf-8"))
         expected_cats = set(kroger_collector.DEFAULT_CATEGORY_TERMS.keys())
         completed_cats = {
             item.get("category") for item in saved.get("completed_categories", [])
             if item.get("status") == "ok"
         }
         assert expected_cats.issubset(completed_cats)
     ```
   - **Critique:** This test writes a dictionary to disk, reads it back, and asserts that a set is a subset of itself! It **never calls `collect_kroger_catalog`** and **never calls `main()`**. It does not test that the collector actually rejects the checkpoint, logs `resume_rejected_completed_checkpoint`, or starts fresh.

2. **Helper-Only Isolation:**
   - Tests in `tests/test_schema.py` verify pure helper functions (`replace_category_records`, `is_approx_weight`, `apply_category_result`), but do not verify how these helpers interact with the collector's query loops and deduplication sets (`seen_keys`, `seen_upcs`). This allowed the major retention defect (Area 1) to slip past the passing test suite.

---

### 8. Git Review: PASS

#### Current Repository State
- **HEAD Commit:** `ab961aa4f8fedd931b6c8797daf7d1d61e88d0a7`
- **Branch:** `fix/darren-review-2026-10`
- **Commit Status:** Confirmed **0 commits, 0 pushes**. All modifications remain uncommitted in the local working tree.
- **Git Diff Whitespace Check (`git diff --check`):**
  ```text
  README.md:145: new blank line at EOF.
  REVIEW_NOTES.md:88: new blank line at EOF.
  src/schema.py:312: new blank line at EOF.
  tests/test_recovery_and_publish.py:437: new blank line at EOF.
  tests/test_schema.py:217: new blank line at EOF.
  ```

#### Summary of Changed Files

- Tracked files modified:
  - `README.md`
  - `REVIEW_NOTES.md`
  - `config/settings.example.json`
  - `scripts/diagnostics/kroger_pagination_check.py`
  - `src/schema.py`
  - `src/scraper.py`
  - `tests/live/kroger_auth_check.py`
  - `tests/live/kroger_location_check.py`
  - `tests/live/kroger_products_check.py`
  - `tests/test_product_parser.py`
  - `tests/test_recovery_and_publish.py`
  - `tests/test_schema.py`
- Untracked files created:
  - `DATA_SOURCE_AUDIT.md`
  - `azure/`
  - `data/checkpoints/`
  - `data/evidence/`
  - `src/kroger_collector.py`
  - `tests/test_collection_flow.py`
  - `tests/test_kroger_collector.py`

---

## Are the Changes Safe to Send to Darren?

**NO.** The changes are **NOT safe to send to Darren** in their current state.

### Reasons for Rejection
1. **Critical Ingestion Failure on Subsequent Runs:** Because `collect_kroger_catalog` populates `seen_keys` and `seen_upcs` from `previous_records`, any scheduled run against an existing production catalog will treat all previously seen products as duplicates. This will cause empty categories, trigger `incomplete` category statuses, and cause catalog validation to fail closed.
2. **Category Reclassification Duplicate Failure:** `replace_category_records` will produce duplicate store-scoped product keys whenever a product's category assignment changes, failing catalog validation.
3. **WAF Bypass Policy Violation:** `DATA_SOURCE_AUDIT.md` suggests using residential proxies to bypass Akamai Bot Manager, violating Darren's explicit instructions.
4. **Hollow Test:** `test_resume_rejects_completed_checkpoint` does not test actual checkpoint rejection logic.

---

## Short List of Required Fixes

1. **Fix Historical Deduplication in `src/kroger_collector.py`:**
   - In `collect_kroger_catalog`, do **NOT** pre-populate `seen_keys` and `seen_upcs` with `all_records` if `all_records` contains `previous_records`.
   - `seen_keys` and `seen_upcs` must only track products observed during the *current collection run* to prevent in-run duplication across overlapping search queries.
   - Merging with historical records must happen strictly at the category level via `apply_category_result` / `replace_category_records`.

2. **Fix Category Reclassification in `src/schema.py:replace_category_records`:**
   - When merging fresh records for a category, remove matching `product_key`s from `preserved_other_categories` so a product moving between categories does not generate a duplicate key.

3. **Cleanse `DATA_SOURCE_AUDIT.md` of Proxy / Bypass References:**
   - Remove all mentions of "residential proxies", "commercial proxy service", and "stealth".
   - State purely factually: Akamai Bot Manager terminates automated headless sessions with HTTP 403 / HTTP/2 protocol errors; the browser route is non-operational for automated runs.
   - Accurately reclassify absent fields as *"Absent from inspected test responses"* rather than *"officially unsupported by the upstream API"*.
   - Acknowledge that boolean fulfillment flags (`instore`, `curbside`, `delivery`) are present in raw item data.

4. **Replace Hollow Test in `tests/test_kroger_collector.py`:**
   - Update `test_resume_rejects_completed_checkpoint` to pass `--resume` with a completed checkpoint through `main()` or `collect_kroger_catalog` and verify that the completed checkpoint is actually rejected and that a fresh collection is initiated.
