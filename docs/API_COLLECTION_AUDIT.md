# Kroger API Collection & Integrity Audit

- **Target Retailer:** Mariano's
- **Demonstration Store:** Store #78, 1720 N Milwaukee Ave, Vernon Hills, IL 60061
- **Retailer Store ID:** `53100502`
- **Execution Date:** 2026-11-01
- **Output Files:** `data/marianos_latest.csv`, `data/marianos_latest.json`, `data/report_20261101T181048Z.json`

---

## 1. Exact API Endpoints & Authentication

The automated product ingestion pipeline executes directly against the official Kroger Developer REST API (`https://api.kroger.com/v1`):

1. **OAuth2 Token Endpoint:**
   - URL: `https://api.kroger.com/v1/connect/oauth2/token`
   - Grant Type: `client_credentials`
   - Authentication Header: `Basic <base64(client_id:client_secret)>`
   - Granted Scope: `product.compact`
   - Token Validity: 1,800 seconds (30 minutes), proactively refreshed 60 seconds prior to expiration.
2. **Locations Endpoint:**
   - URL: `https://api.kroger.com/v1/locations`
   - Parameters: `filter.zipCode.near=60061&filter.limit=50`
   - Used for independent, fail-closed verification of store identity prior to collecting products.
3. **Products Endpoint:**
   - URL: `https://api.kroger.com/v1/products`
   - Parameters:
     - `filter.term`: Department staple search query.
     - `filter.locationId`: `53100502` (strictly locks product availability and pricing to Vernon Hills #78).
     - `filter.limit`: `50` (maximum allowed per page).
     - `filter.start`: Skip offset (`0` omitted, `50`, `100`, `150`, `200`, `250`).
   - Hard Offset Constraint: Kroger strictly caps `filter.start <= 250`. An unpartitioned query cannot exceed ~300 items per search term.

---

## 2. Store #78 Verification Evidence

Before collecting any products, `verify_location` queried the Locations API with ZIP `60061` and asserted all location attributes against the expected demonstration store. The API returned:

```json
{
  "locationId": "53100502",
  "name": "Marianos - Marianos Vernon Hills",
  "chain": "MARIANOS",
  "address": {
    "addressLine1": "1720 N Milwaukee Ave",
    "city": "Vernon Hills",
    "state": "IL",
    "zipCode": "60061",
    "county": "Lake County"
  },
  "departments": [ ... ]
}
```

- **Verification Result:** Verified `True`.
- **Evidence Path:** `data/evidence/location_53100502.json`.
- **Fail-Closed Policy:** If the store ID is absent or if street address, city, state, or ZIP mismatches, the collector halts execution with exit code 1 without querying products.

---

## 3. Product Collection Methodology & Department Quotas

### Collection Architecture
To satisfy the requirement of harvesting at least 500 genuine, eligible products within Kroger's 250-skip API limit, the collector partitions queries across 14 distinct grocery and household departments:
- *Produce, Dairy, Meat & Seafood, Bakery, Frozen Foods, Pantry, Snacks, Beverages, Breakfast, Canned Goods, Household, Personal Care, Baby, Pet Supplies*.

### Department Quota Logic
The collector enforces a quota of **40 products per department**:
$$\text{target\_per\_cat} = \max\left(40, \left\lfloor\frac{\text{target\_count}}{\text{num\_departments}}\right\rfloor + 5\right) = \max\left(40, \left\lfloor\frac{500}{14}\right\rfloor + 5\right) = 40$$

Once a department reaches 40 valid, deduplicated, fixed-weight products, collection advances to the next department. This guarantees balanced departmental coverage ($14 \times 40 = 560$ products) and prevents redundant API queries.

> [!IMPORTANT]
> **Scope Notice & `pagination_complete` Semantics:** This 560-product dataset is a partitioned discovery sample designed to prove automated ingestion and schema conformity. It does **not** represent exhaustive catalog coverage of Mariano's entire store inventory, which contains tens of thousands of SKUs across all aisles.
> In collection diagnostic metrics, `pagination_complete: true` denotes that the requested department quota was successfully fulfilled or that pagination across the assigned query terms was naturally exhausted without premature truncation or upstream errors—not that every item in Mariano's store was fetched.

---

## 4. 560-Record Result & Uniqueness Verification

### Ingestion Metrics
- **Total Unique Products in Output:** **560**
- **Raw API Products Evaluated:** 697
- **In-Run Duplicates Filtered:** 0 (search terms across departments did not return overlapping items)
- **Validation Result:** `validation_passed: true` (0 duplicates, 0 nulls, 0 non-strings, 0 schema violations)
- **API Calls:** 16 total requests (1 OAuth token + 1 Location check + 14 department searches), 0 retries, 0 rate limits (HTTP 429).

### Uniqueness Checks
- **Unique UPCs:** **560 / 560 (100% unique)**.
- **Unique Canonical URLs:** **560 / 560 (100% unique)**.

### Detailed Review of Shared Normalized Title + Size Records
The catalog contains exactly one instance where two records share the same normalized title and package size:
1. **UPC `0001111016044`**:
   - Title: `Kroger® Condensed Cream of Chicken Soup`
   - Weight: `10.5 oz`
   - Regular Price: `$0.85`
   - Product URL: `https://www.marianos.com/p/kroger-condensed-cream-of-chicken-soup/0001111016044`
   - Image URL: `https://www.kroger.com/product/images/xlarge/front/0001111016044`
2. **UPC `0001111004523`**:
   - Title: `Kroger® Condensed Cream of Chicken Soup`
   - Weight: `10.5 oz`
   - Regular Price: `$1.69`
   - Product URL: `https://www.marianos.com/p/kroger-condensed-cream-of-chicken-soup/0001111004523`
   - Image URL: `https://www.kroger.com/product/images/xlarge/front/0001111004523`

*Factual Audit Finding:* The Kroger API returns these as two distinct items with different UPCs, distinct product image URLs, distinct canonical product URLs, and different regular shelf prices (`$0.85` vs. `$1.69`). Because the API payload does not provide ingredient or recipe disclosures, no claim is made regarding internal formulation differences; they are retained strictly as separate retailer SKUs returned by the store inventory endpoint.

---

## 5. Field Completeness Statistics (22 Schema Fields)

All 560 records conform strictly to the 22-column specification. All values are strings (`""` for missing fields; never `null`).

| # | Field | Populated | Empty | Completeness | Notes |
|---|---|---|---|---|---|
| 1 | `zipcode` | 560 | 0 | 100.0% | Store ZIP: `60061` |
| 2 | `store_name` | 560 | 0 | 100.0% | Normalized: `Mariano's` |
| 3 | `store_location` | 560 | 0 | 100.0% | `1720 N Milwaukee Ave, Vernon Hills, IL 60061` |
| 4 | `store_logo` | 0 | 560 | 0.0% | Not provided in Kroger API |
| 5 | `store_brand` | 560 | 0 | 100.0% | `yes` (Private label) or `no` (National brand) |
| 6 | `category` | 560 | 0 | 100.0% | Assigned department (14 departments) |
| 7 | `sub_category` | 337 | 223 | 60.18% | Extracted when secondary tag exists in API taxonomy |
| 8 | `product_title` | 560 | 0 | 100.0% | Cleaned product description |
| 9 | `weight` | 560 | 0 | 100.0% | Package size (approximate-weight items excluded) |
| 10 | `regular_price` | 560 | 0 | 100.0% | Valid positive 2-decimal string (`$0.49` – `$42.99`) |
| 11 | `sale_price` | 150 | 410 | 26.79% | Populated only when promo price < regular price |
| 12 | `volume_deals_description` | 0 | 560 | 0.0% | Not provided in `product.compact` |
| 13 | `volume_deals_price` | 0 | 560 | 0.0% | Not provided in `product.compact` |
| 14 | `digital_deals_description` | 0 | 560 | 0.0% | Requires customer loyalty OAuth scope |
| 15 | `digital_deals_price` | 0 | 560 | 0.0% | Requires customer loyalty OAuth scope |
| 16 | `image_url` | 560 | 0 | 100.0% | Largest front perspective image URL |
| 17 | `url` | 560 | 0 | 100.0% | Canonical product page URI (`https://www.marianos.com/p/...`) |
| 18 | `upc` | 560 | 0 | 100.0% | Standard 10–13 digit UPC |
| 19 | `gupc` | 0 | 560 | 0.0% | Client specification placeholder (strictly `""`) |
| 20 | `ppm` | 0 | 560 | 0.0% | Directly displayed PPM absent; never synthesized |
| 21 | `unit_price` | 0 | 560 | 0.0% | Client specification placeholder (strictly `""`) |
| 22 | `crawl_date` | 560 | 0 | 100.0% | Execution ISO date (`2026-11-01`) |

---

## 6. Critical API Limitations & Missing Fields

1. **Directly Displayed PPM (`ppm`):**
   - The Kroger `product.compact` response returns package weight and total package price, but does not expose the physical shelf-edge PPM string (e.g. `$0.15/oz`).
   - In accordance with client specification rules ("Do not calculate PPM"), `ppm` is recorded strictly as `""`.
2. **Volume Deals (`volume_deals_*`):**
   - Promotional bundle terms (e.g., "Buy 2 for $5", "BOGO") are absent from the `product.compact` payload. Kept strictly as `""`.
3. **Digital Coupons (`digital_deals_*`):**
   - Clip-to-card digital coupons require an authenticated Customer OAuth2 account session with user profile scopes. They are not accessible via machine-to-machine `client_credentials`. Kept strictly as `""`.
4. **Store Logo (`store_logo`):**
   - Neither `/v1/products` nor `/v1/locations` provides retailer logo image assets. Kept strictly as `""`.

---

## 7. Actual vs. Simulated Historical Retention

It is important to clearly distinguish what occurred during this run versus what is verified in the test suite:

- **Actual Run Behavior (Baseline Establishment):**
  - Prior to this run, `data/marianos_latest.json` and `data/marianos_latest.csv` did not exist in the working directory.
  - Therefore, this run was the initial baseline creation. `load_previous_catalog` found no pre-existing catalog and published 560 freshly discovered products.
- **Simulated Retention Testing (Pipeline Invariants):**
  - The retention layer (`replace_category_records`, `apply_category_result`) is covered by unit and integration tests (`tests/test_kroger_collector.py` and `tests/test_recovery_and_publish.py`).
  - Dedicated test `test_retention_pipeline_preserves_unavailable_eligible_product_with_original_date` explicitly validates that if an eligible item is absent from a subsequent crawl response, the pipeline preserves it with its original `crawl_date` and historic price while updating newly observed items.
