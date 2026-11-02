# Mariano's Product Collector — Final Delivery Evidence Package
## Authoritative Delivery & Verification Report for Darren Review

- **Target Retailer:** Mariano's (Kroger Co.)
- **Demonstration Store:** Mariano's #78, 1720 N Milwaukee Ave, Vernon Hills, IL 60061
- **Kroger Location ID:** `53100502` (Division `531`, Store `00502`)
- **Git Branch:** `fix/darren-review-2026-10`
- **Documentation & Verification Date:** 2026-11-02
- **Published Baseline Catalog:** [`data/marianos_latest.json`](file:///c:/Users/it/Desktop/My%20projects/Website%20for%20a%20company/Mariano/Mariano-s-Product-Scraper-for-Azure/data/marianos_latest.json) & [`data/marianos_latest.csv`](file:///c:/Users/it/Desktop/My%20projects/Website%20for%20a%20company/Mariano/Mariano-s-Product-Scraper-for-Azure/data/marianos_latest.csv) (**560 genuine unique products**, 100% verified against exact 22-field contract)
- **Offline Test Suite Status:** **179 passed in 31.86s** (0 failed, 0 skipped, 0 warnings across 10 test modules)
- **Final Readiness Decision:** `READY_FOR_CODE_HANDOFF_WITH_LIMITATIONS`

### Standardized Status Labels for Deliverables
- `OFFLINE_TESTED`: 179/179 deterministic unit/integration tests passing across schema, parser, collectors, checkpoints, interruption recovery, and failure injection.
- `LIVE_API_VERIFIED`: Real Kroger developer credentials verified for OAuth 2.0 authentication, store lookup (`53100502`), product search pagination, offset semantics probing, and end-to-end collector execution.
- `LIVE_PROOF_EXECUTION_SUCCEEDED`: Minimal proof collection run executed live, evaluated 10 items, accepted 5 products, passed all validation rules, and wrote full JSONL audit trail without modifying baseline production data.
- `LIVE_COLLECTION_INCOMPLETE`: Full live collection run `20261102T180656Z` executed (19,689 raw items evaluated across 419 pages, yielding 13,862 candidate items); rejected by validation due to 7 premature truncation terms in Produce and Pantry, properly preserving the baseline production catalog byte-for-byte.
- `STATICALLY_REVIEWED_NOT_COMPILED`: Bicep template ([`azure/container-app-job.bicep`](file:///c:/Users/it/Desktop/My%20projects/Website%20for%20a%20company/Mariano/Mariano-s-Product-Scraper-for-Azure/azure/container-app-job.bicep)) verified for syntax, schedule, secrets, and environment mapping, but uncompiled because `az` and `bicep` CLI tools are not installed in the local environment.
- `CONFIGURED_NOT_DEPLOYED`: Cloud infrastructure definitions complete in Bicep, but unprovisioned due to absence of Azure CLI and subscription credentials in the environment.
- `LOCAL_STORAGE_TESTED`: Local paired atomic file replacement (`.json` and `.csv`) with automatic `.bak` recovery and crash-atomicity handling verified via unit tests.
- `AZURE_BLOB_NOT_LIVE_VERIFIED`: Live Azure Blob Storage read/write not performed due to absence of Azure Storage connection string.
- `BLOCKED`: Upstream data omissions (digital deals, volume deals, PPM, store logo) and external cloud infrastructure access.

---

## 1. Executive Summary & Delivery State

| Deliverable Area | Operational State | Verification Status | Truthful Documentation Notes |
|---|---|---|---|
| **Active Catalog** | 560 unique products | `OFFLINE_TESTED` | Baseline production pair ([`marianos_latest.json`](file:///c:/Users/it/Desktop/My%20projects/Website%20for%20a%20company/Mariano/Mariano-s-Product-Scraper-for-Azure/data/marianos_latest.json) & [`.csv`](file:///c:/Users/it/Desktop/My%20projects/Website%20for%20a%20company/Mariano/Mariano-s-Product-Scraper-for-Azure/data/marianos_latest.csv)) strictly preserved byte-for-byte. |
| **Candidate Crawl** | 13,862 candidate items | `LIVE_COLLECTION_INCOMPLETE` | Run `20261102T180656Z` evaluated 19,689 items; rejected by validation and preserved baseline. |
| **Search-Term Scope** | 93 configured terms | `LIVE_API_VERIFIED` | 100% of 93 terms attempted (86 succeeded: 51 exhausted, 35 API-limited; 7 prematurely truncated). |
| **7-Term End-to-End Test** | Production collector executed | `LIVE_API_VERIFIED` | Production `collect_kroger_catalog` executed live with isolated run ID; 1,683 products collected; 0 premature truncations. |
| **Offset Semantics** | Probed directly on API | `LIVE_API_VERIFIED` | Hard cap of `start=250` confirmed (`PRODUCT-2021`). `start` indexes pre-filtered window; zero cross-page duplicate IDs or item skips. |
| **JSONL Evidence** | 23,978 legacy + 1,683 diag | `LIVE_API_VERIFIED` | 100% valid JSONL records referencing exact store, endpoint, offset, UPC, raw payloads, and run_id. |
| **22-Field Contract** | Exact schema order | `OFFLINE_TESTED` | Single source of truth in `FIELDS`; 100% string types; `gupc` and `unit_price` strictly `""`; 0 legacy fields. |
| **Approximate Weight** | Scale/checkout filter | `OFFLINE_TESTED` | Excludes scale-weighed produce/meat; preserves fixed multipacks, weighted blankets, and foundation years. |
| **Azure Scheduled Job** | Container Apps Job Bicep | `CONFIGURED_NOT_DEPLOYED` | UTC cron `0 9 * * 2,5` (03:00 CST / 04:00 CDT); unprovisioned due to absence of Azure CLI/credentials. |
| **Azure Blob Storage** | Local rollback tested | `AZURE_BLOB_NOT_LIVE_VERIFIED` | Local atomic pair replacement verified; real Azure Blob read/write not performed. |
| **Browser Scraping** | Akamai WAF blocked | `BLOCKED` | `marianos.com` blocks headless cloud crawlers; Kroger API pipeline is authoritative. |
| **Coupon/Deal Fields** | Scope restricted | `BLOCKED` | Kroger `product.compact` API does not expose loyalty coupons, volume bundles, PPM, or store logo. |

---

## 2. Reconciled Live Collection Audit (`report_failed_20261102T180656Z.json`)

On 2026-11-02, a full collection was executed against Mariano's store #78 (`53100502`) using live developer credentials:

### Reconciled Metrics Calculated from Artifacts
- **Configured Terms Expected:** **93 terms** across 14 departments in `DEFAULT_CATEGORY_TERMS`.
  - *Authoritative Count Reconciliation*: Produce: 8, Dairy: 7, Meat & Seafood: 8, Bakery: 8, Frozen Foods: 6, Pantry: 8, Snacks: 7, Beverages: 7, Breakfast: 6, Canned Goods: 6, Household: 6, Personal Care: 6, Baby: 5, Pet Supplies: 5. Sum = **93**. (A naive assumption of $14 \times 6 = 84$ is factually wrong).
- **Dispatched / Attempted Terms:** **93 terms attempted (100% attempted)**.
- **Terms Succeeded:** **86 terms**:
  - Naturally exhausted: 51 terms (`status: "exhausted"`, `stop_reason: "exhausted"`).
  - Stopped by Kroger API offset limit (250): 35 terms (`status: "stopped_by_api_limit"`, `stop_reason: "api_limit_reached"`).
- **Terms Incomplete (Truncated):** **7 terms**:
  1. `Produce` -> `apples`: Offset 0 returned 47 items (`api_reported_total: 289`). Stopped after page 1 (`stop_reason: "premature_truncation"`).
  2. `Produce` -> `bananas`: Offset 0 returned 49 items (`api_reported_total: 165`). Stopped after page 1 (`stop_reason: "premature_truncation"`).
  3. `Produce` -> `berries`: Offset 0 returned 48 items (`api_reported_total: 419`). Stopped after page 1 (`stop_reason: "premature_truncation"`).
  4. `Produce` -> `lettuce`: Offset 0 returned 49 items (`api_reported_total: 123`). Stopped after page 1 (`stop_reason: "premature_truncation"`).
  5. `Produce` -> `tomatoes`: Offset 0 returned 49 items (`api_reported_total: 412`). Stopped after page 1 (`stop_reason: "premature_truncation"`).
  6. `Produce` -> `potatoes`: Offset 0 returned 48 items (`api_reported_total: 548`). Stopped after page 1 (`stop_reason: "premature_truncation"`).
  7. `Pantry` -> `sugar`: Offset 0 returned 49 items (`api_reported_total: 1000`). Stopped after page 1 (`stop_reason: "premature_truncation"`).
- **Terms Failed (API Errors):** **0 terms**.
- **Sum of Terminal Statuses:** $51 + 35 + 7 + 0 = \mathbf{93}$.
- **Raw Product Appearances Evaluated:** **19,689 products** across **419 pages**.
- **Within-Category Duplicate Appearances:** **3,312 duplicates**.
- **Category-Accepted Products Sum:** **15,923 products** across individual categories.
- **Cross-Category Duplicate Overlap:** **2,061 products** appeared in multiple categories.
- **Candidate Globally Unique Products:** **13,862 products** ($15,923 - 2,061 = 13,862$).
- **Invalid / Missing Price Exclusions:** **123 products**.
- **Validation Outcome:** Rejected (`validation_passed: False`) because Produce and Pantry were incomplete.
- **Baseline Protection:** Neither [`data/marianos_latest.json`](file:///c:/Users/it/Desktop/My%20projects/Website%20for%20a%20company/Mariano/Mariano-s-Product-Scraper-for-Azure/data/marianos_latest.json) nor [`data/marianos_latest.csv`](file:///c:/Users/it/Desktop/My%20projects/Website%20for%20a%20company/Mariano/Mariano-s-Product-Scraper-for-Azure/data/marianos_latest.csv) was overwritten.

---

## 3. Investigation of Kroger API Offset Semantics, Gap Analysis & End-to-End Live Testing

### A. Empirical Verification of Kroger API Offset Semantics
Direct probes against `GET https://api.kroger.com/v1/products` revealed the exact technical mechanics of Kroger's search indexing and pagination:

1. **Hard Upper Bound on `filter.start`:**
   Any request with `filter.start > 250` fails with **HTTP 400 Bad Request**:
   ```json
   {"errors":{"timestamp":1793648382919,"code":"PRODUCT-2021","reason":"Field 'start' must be a number between 1 and 250 (inclusive)"}}
   ```
   The Kroger API strictly prohibits pagination beyond `start=250`. With the maximum page size of `limit=50`, the maximum query window accessible for any search term is items 250 through 299 (covering up to index 300).
2. **`filter.start` Indexing Behavior:**
   `filter.start` represents the starting offset in Kroger's *pre-filtered* enterprise catalog window, before store-level availability filters are applied.
   - For `apples`: At `start=0, limit=50`, 3 items were filtered out by store `53100502` because they are out of stock or uncarried, returning 47 items.
   - At `start=50, limit=50`, Kroger evaluates window [50, 100). The first item returned at `start=50` (`0001480031816`) was tested against `start=47` and confirmed to be the exact item immediately following item 46 (`0088949798443`) of page 1!
   - Probing with `start=47, limit=10` confirmed that items 44–46 of page 1 and items 0–1 of page 2 appear in strict sequential order.
3. **No Pagination Gaps or Skips:**
   Cross-page duplicate and gap checks across all returned IDs for `apples`, `bananas`, and `lettuce` proved:
   - **0 duplicate IDs across pages.**
   - **0 skipped products between page boundaries.**
   - The discrepancy between `api_reported_total` (e.g. 289 for apples) and total received (286) represents exactly the 3 out-of-stock items filtered out at store level in window [0, 50).
   - `apples` (286 products), `bananas` (164 products), and `lettuce` (122 products) reached the end of all available store-stocked inventory (`start + page_len >= total`).
   - `berries` (total 419), `tomatoes` (total 412), `potatoes` (total 548), and `sugar` (total 1000) reached Kroger's API ceiling of offset 250 and are legitimately categorized as `stopped_by_api_limit` / `api_limit_reached`.

### B. End-to-End Live Execution of Production `collect_kroger_catalog`
A controlled live execution of the actual production collector ([`src/kroger_collector.py`](file:///c:/Users/it/Desktop/My%20projects/Website%20for%20a%20company/Mariano/Mariano-s-Product-Scraper-for-Azure/src/kroger_collector.py#L675)) was conducted targeting these 7 search terms:
- **Run ID:** `DIAG_SEVEN_TERMS_20261102T214000Z`
- **Isolated Evidence Dir:** `data/evidence_diag_test`
- **Isolated Checkpoint Dir:** `data/checkpoints_diag_test`

| Category | Term | Pages | Raw Received | Within-Term Duplicates | Accepted Unique | Terminal Status | Stop Reason |
|:---|:---|:---:|:---:|:---:|:---:|:---|:---|
| **Produce** | `apples` | 6 | 286 | 0 | 261 | `exhausted` | `exhausted` |
| **Produce** | `bananas` | 4 | 164 | 0 | 160 | `exhausted` | `exhausted` |
| **Produce** | `berries` | 6 | 298 | 2 | 293 | `stopped_by_api_limit` | `api_limit_reached` |
| **Produce** | `lettuce` | 3 | 122 | 0 | 105 | `exhausted` | `exhausted` |
| **Produce** | `tomatoes` | 6 | 299 | 0 | 288 | `stopped_by_api_limit` | `api_limit_reached` |
| **Produce** | `potatoes` | 6 | 298 | 0 | 284 | `stopped_by_api_limit` | `api_limit_reached` |
| **Pantry** | `sugar` | 6 | 299 | 6 | 292 | `stopped_by_api_limit` | `api_limit_reached` |

- **Total Unique Products Collected:** **1,683 products**.
- **Premature Truncations:** **0** (all 7 terms completed full pagination).
- **Category Coverage Status:** Both Produce and Pantry were assigned `coverage_status: "limited_by_api"` (`coverage_complete: False`) because terms with totals > 300 were capped at offset 250 by Kroger's API limit.
- **Evidence Verification:** Exactly 1,683 records written to `data/evidence_diag_test/product_evidence_53100502.jsonl`, all possessing `run_id: "DIAG_SEVEN_TERMS_20261102T214000Z"`.
- **Baseline Catalog Untouched:** No publication was triggered; the 560-product production catalog was preserved byte-for-byte.

---

## 4. Evidence Traceability & Run Isolation Verification

### A. Production JSONL File Audit (`data/evidence/product_evidence_53100502.jsonl`)
- **Total Records:** **23,978 records** (127 MB).
- **Records with Explicit `run_id`:** **0** (all 23,978 are historical legacy records created prior to the `run_id` schema enhancement).
- **Records Without `run_id` (Legacy):** **23,978** (preserved completely untouched without rewriting or deletion).
- **Field Completeness Across All 23,978 Records:**
  - `raw_product_payload`: 100% present (0 missing).
  - `product_id`: 100% present (0 missing).
  - `upc`: 100% present (0 missing).
  - `store_id`: 100% present (`"53100502"`).
  - `search_term`: 100% present.
  - `category`: 100% present.
  - `requested_offset`: 100% present.
  - Parse Errors / Invalid Lines: **0**.

### B. Explanation of Previous Metrics (15,923 Records vs 13,862 Products)
- The evidence writer `append_product_evidence` is invoked **strictly for unique accepted products within each category**.
- In run `20261102T180656Z`, the sum of accepted products across the 14 individual categories was **15,923**.
- Because each item was deduplicated within its category before writing evidence, there were 15,923 unique category-scoped product keys and UPCs in that run.
- When resolving across all 14 categories globally, cross-category duplicates (2,061 items appearing in multiple categories, such as snack bars in both `Snacks` and `Pantry`) were merged, yielding the candidate catalog of **13,862 globally unique products**.

---

## 5. Exact 22-Field Schema Contract & Field Completeness

The authoritative schema contract is defined in `FIELDS` in [`src/schema.py`](file:///c:/Users/it/Desktop/My%20projects/Website%20for%20a%20company/Mariano/Mariano-s-Product-Scraper-for-Azure/src/schema.py#L14-L20) as the single source of truth:

| # | Column Name | Type Contract | Published Completeness | Upstream Availability & Implementation Notes |
|---|---|---|---|---|
| 1 | `zipcode` | `str` (`"60061"`) | 100.0% | Sourced from verified store location. |
| 2 | `store_name` | `str` (`"Mariano's"`) | 100.0% | Normalized retailer brand. |
| 3 | `store_location` | `str` | 100.0% | Full verified address string (`1720 N Milwaukee Ave, Vernon Hills, IL 60061`). |
| 4 | `store_logo` | `str` (`""`) | 0.0% | **Unavailable** in Kroger API; kept strictly blank (`BLOCKED`). |
| 5 | `store_brand` | `str` (`"yes"`/`"no"`) | 100.0% | Classified via Kroger private-label brand catalog matching. |
| 6 | `category` | `str` | 100.0% | Department category taxonomy. |
| 7 | `sub_category` | `str` | 56.87% | Populated strictly when returned by upstream API; 0% inferred. |
| 8 | `product_title` | `str` | 100.0% | Cleaned product description from API payload. |
| 9 | `weight` | `str` | 100.0% | Extracted from package size attribute (`items[0].size`). |
| 10 | `regular_price` | `str` (e.g. `"4.49"`) | 100.0% | Regular shelf price extracted from `items[0].price.regular`. |
| 11 | `sale_price` | `str` (e.g. `"3.99"`) | 27.34% | Populated strictly when promo price exists and is `< regular_price`. |
| 12 | `volume_deals_description` | `str` (`""`) | 0.0% | **Unavailable** in Kroger `product.compact` API (`BLOCKED`). |
| 13 | `volume_deals_price` | `str` (`""`) | 0.0% | **Unavailable** in Kroger `product.compact` API (`BLOCKED`). |
| 14 | `digital_deals_description` | `str` (`""`) | 0.0% | **Unavailable** without customer-level OAuth authorization (`BLOCKED`). |
| 15 | `digital_deals_price` | `str` (`""`) | 0.0% | **Unavailable** without customer-level OAuth authorization (`BLOCKED`). |
| 16 | `image_url` | `str` (URL) | 100.0% | Front-perspective product image URL (`xlarge` / `large`). |
| 17 | `url` | `str` (URL) | 100.0% | Canonical Mariano's URL (`https://www.marianos.com/p/...`). |
| 18 | `upc` | `str` (13 digits) | 100.0% | 13-digit zero-padded UPC string. |
| 19 | `gupc` | `str` (`""`) | 0.0% | Specification placeholder strictly kept as empty string `""`. |
| 20 | `ppm` | `str` (`""`) | 0.0% | Price per measure is not provided in API response; never fabricated (`BLOCKED`). |
| 21 | `unit_price` | `str` (`""`) | 0.0% | Specification placeholder strictly kept as empty string `""`. |
| 22 | `crawl_date` | `str` (`YYYY-MM-DD`) | 100.0% | ISO 8601 observation date (`"2026-11-01"`). |

---

## 6. Azure Deployment Reality & Schedule Semantics

- **Bicep Template:** `STATICALLY_REVIEWED_NOT_COMPILED` ([`azure/container-app-job.bicep`](file:///c:/Users/it/Desktop/My%20projects/Website%20for%20a%20company/Mariano/Mariano-s-Product-Scraper-for-Azure/azure/container-app-job.bicep) reviewed against ARM specifications; Azure CLI and Bicep CLI are not installed locally).
- **Job Deployment:** `CONFIGURED_NOT_DEPLOYED` (No cloud resources have been provisioned).
- **Storage Status:** `LOCAL_STORAGE_TESTED`, `AZURE_BLOB_NOT_LIVE_VERIFIED` (Local paired atomic file replacement with automatic `.bak` recovery tested; live Azure Blob Storage read/write not tested).
- **Schedule Timezone & Daylight Saving Time Semantics:**
  - Schedule is UTC cron `0 9 * * 2,5` (Tuesdays and Fridays at 09:00 UTC).
  - Azure Container Apps Jobs evaluate cron triggers strictly in UTC.
  - **Winter (Central Standard Time, CST, UTC-6):** Executes at **03:00 AM Central**.
  - **Summer (Central Daylight Time, CDT, UTC-5):** Executes at **04:00 AM Central**.
  - A fixed UTC cron expression does **not** execute at 03:00 AM Central year-round. Maintaining 03:00 AM local time year-round requires updating the cron parameter semi-annually (`0 8 * * 2,5` in CDT) or triggering the job via an Azure Logic App recurrence trigger with native IANA timezone support.

---

## 7. Automated Test Suite Results

```powershell
.\.venv\Scripts\python.exe -m pytest tests/
```

- **Collected:** **179 items**
- **Passed:** **179 passed**
- **Failed:** **0**
- **Skipped:** **0**
- **Warnings / Errors:** **0**
- **Runtime:** **31.86 seconds**

---

## 8. What Darren Must Provide or Approve

1. **Azure Cloud Access:** An active Azure subscription ID, resource group name, and permissions for the cloud administrator to execute `az deployment group create` with [`azure/container-app-job.bicep`](file:///c:/Users/it/Desktop/My%20projects/Website%20for%20a%20company/Mariano/Mariano-s-Product-Scraper-for-Azure/azure/container-app-job.bicep).
2. **Azure Storage Account:** A storage connection string secret (`AZURE_STORAGE_CONNECTION_STRING`) for durable cloud blob retention.
3. **Approval on Blocked Data Fields:** Written business sign-off confirming that upstream unavailable fields (`digital_deals_*`, `volume_deals_*`, `ppm`, `store_logo`) will remain empty strings `""`, or enterprise commercial authorization from Kroger to access customer loyalty endpoints.
