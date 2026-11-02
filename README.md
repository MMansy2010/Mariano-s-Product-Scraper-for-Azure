# Mariano's Product Scraper for Azure

Production-ready grocery catalog collection pipeline for Mariano's (Kroger Co.) on Microsoft Azure, configured for the Vernon Hills demonstration store.

## 1. Store Demonstration Identity

- **Store Name**: Mariano's
- **Street Address**: `1720 N Milwaukee Ave, Vernon Hills, IL 60061`
- **ZIP Code**: `60061`
- **Retailer Store ID**: `53100502`
- **Retailer Verification**: Verified via official Kroger/Mariano's division `531`, store number `00502` (`https://www.marianos.com/stores/details/531/00502`).
- **Store Identity Enforcement**: `verify_store_identity` enforces that both the configured store name and the street address component (`1720 N Milwaukee Ave`) appear in visible header/store-selector text before category extraction proceeds. Fails closed if store context does not match.

## 2. Exact 22-Field Output Schema

Output JSON records preserve the exact 22-field specification in `src/schema.py`:

| Index | Field Name | Type | Description |
|:---|:---|:---|:---|
| 1 | `zipcode` | `string` | 5-digit ZIP code (`"60061"`) |
| 2 | `store_name` | `string` | Retailer brand (`"Mariano's"`) |
| 3 | `store_location` | `string` | Full store address |
| 4 | `store_logo` | `string` | Retailer logo URL or empty string |
| 5 | `store_brand` | `string` | `"True"` for private label, else `"False"` |
| 6 | `category` | `string` | Top-level category from configuration |
| 7 | `sub_category` | `string` | Subcategory name |
| 8 | `product_title` | `string` | Cleaned product title |
| 9 | `weight` | `string` | Package size/weight string |
| 10 | `regular_price` | `string` | Regular retail price (e.g. `"4.99"`) |
| 11 | `sale_price` | `string` | Promotional sale price or empty string |
| 12 | `volume_deals_description` | `string` | Multi-buy promo text (e.g. `"Buy 2 Get 1 Free"`) |
| 13 | `volume_deals_price` | `string` | Volume deal price or empty string |
| 14 | `digital_deals_description` | `string` | Digital coupon text (e.g. `"$1.00 Off with digital coupon"`) |
| 15 | `digital_deals_price` | `string` | Net price after digital deal or empty string |
| 16 | `image_url` | `string` | Primary product image URL |
| 17 | `url` | `string` | Canonical product detail page URL |
| 18 | `upc` | `string` | GTIN-12 / GTIN-13 / GTIN-14 UPC code |
| 19 | `gupc` | `string` | **Always empty string** (`""`) |
| 20 | `ppm` | `string` | Price per measure from visible text (never calculated) |
| 21 | `unit_price` | `string` | **Always empty string** (`""`) |
| 22 | `crawl_date` | `string` | ISO-8601 crawl date (`"YYYY-MM-DD"`) |

- **Schema Guarantees**:
  - All values are strictly strings; missing values are empty strings (`""`), never `null`.
  - Field key order is strictly validated (`FIELDS`).
  - `gupc` and `unit_price` remain strictly empty strings.
  - Approximate-weight products (e.g., random-weight meat, bulk produce) are strictly excluded.

## 3. Fail-Closed Collection Coverage & Diagnostics

- **Coverage Flow**:
  - Discovers top-level category pages, child subcategories (`/pl/`), and pagination pages (`rel="next"` or query params).
  - Exhausts all pagination pages without arbitrary page/scroll caps.
  - Protects against infinite loops via `seen_page_urls` duplicate detection (`repeated_pagination_page`).
  - Detects bot challenge/block pages (`blocked_or_challenged`).
  - Verifies store context across all category and pagination requests (`unverified_store_context`).
  - Tracks product detail page parsing failures (`product_parse_failure`).
- **Fail-Closed Rule**: A category is NEVER marked successful (`"ok"`) if any subcategory, pagination page, or product parsing encounters an error. Partial records are retained only in diagnostics and checkpoints, never published to the active catalog.
- **Machine-Readable Outcomes**:
  - `fully_exhausted_and_successful`
  - `incomplete_due_to_failed_requests`
  - `blocked_or_challenged`
  - `repeated_pagination_page`
  - `product_parse_failure`
  - `interrupted_collection`
  - `unexpected_or_unverified_store_context`

## 4. Historical Retention & Resilient Interruption Recovery

- **Historical Retention (`replace_category_records`)**:
  - Fresh successful category crawl updates older observations of the same store-scoped product.
  - Products absent from the new crawl retain their previous observations and original `crawl_date`.
- **Atomic Staging & Safe Publication (`publish_staging_file`)**:
  - Crawl results write to `staging_{run_stamp}.json`.
  - Comprehensive validation (`validate_records`) verifies zero duplicates, zero nulls, exact field order, minimum product count, category completeness, and store identity proof.
  - Staging atomically replaces `data/marianos_latest.json` only after validation passes.
  - Failed or incomplete runs exit with code 2 and strictly preserve the previous successful catalog.
- **Interruption Recovery (`--resume`)**:
  - Traps `KeyboardInterrupt` and `asyncio.CancelledError`, marks the active category as `interrupted`, and commits the atomic checkpoint.
  - Re-running with `--resume` inspects `data/checkpoints/checkpoint_{location}.json`, skips previously completed (`ok`) categories, retries incomplete/interrupted categories, and merges without product loss.

## 5. Azure Production Deployment

- **Azure Container Apps Job (`azure/container-app-job.bicep`)**:
  - Scheduled cron: `0 3 * * 2,5` (Tuesday & Friday 3:00 AM) in `America/Chicago` timezone.
  - 1.0 vCPU, 2.0 Gi RAM, Playwright Chromium container image.
  - Azure Key Vault secret references for connection strings and credentials.
- **Azure Blob Storage Publication**:
  - When configured via `AZURE_STORAGE_CONNECTION_STRING`, atomically publishes:
    - `marianos_latest.json` (latest production catalog)
    - `runs/{run_stamp}/catalog.json` (immutable audit trail)
    - `runs/{run_stamp}/report.json` (machine-readable run report and diagnostics)

## 6. Local Setup and Execution

### Prerequisites
- Python 3.11+
- Playwright with Chromium

```bash
# Set up virtual environment
python -m venv .venv
# Windows: .venv\Scripts\activate | macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# Copy settings template
cp config/settings.example.json config/settings.json
```

### Running the Scraper
```bash
# Standard automated run
python src/scraper.py --config config/settings.json

# Interactive headed run for manual store verification
python src/scraper.py --config config/settings.json --location vernon-hills --headed --manual-store-selection

# Resume an interrupted run
python src/scraper.py --config config/settings.json --resume
```

### Running the Automated Test Suite
```bash
# Run all 49 unit and integration tests
pytest -v
```

## 7. Diagnostics and API Checks

Diagnostic tools for Kroger API and store locator validation are provided under `tests/live/` and `scripts/diagnostics/`:
```bash
# Verify Kroger API credentials
python tests/live/kroger_auth_check.py

# Verify Vernon Hills store locator response
python tests/live/kroger_location_check.py

# Test Kroger product catalog API
python tests/live/kroger_products_check.py

# Test pagination and category extraction
python scripts/diagnostics/kroger_pagination_check.py
```
