# Mariano's #78 Data Source & Collection Audit

- **Target Store:** Mariano's, 1720 N Milwaukee Ave, Vernon Hills, IL 60061
- **Retailer Store ID:** `53100502` (Kroger Division `531`, Store `00502`)
- **Audit Date:** 2026-11-01
- **Repository:** `MMansy2010/Mariano-s-Product-Scraper-for-Azure`
- **Branch:** `fix/darren-review-2026-10`
- **Execution Context:** Strictly offline inspection based on repository code, saved evidence, and offline unit/integration test suite.

---

## 1. Exact Collection Method

The repository implements two distinct collection pathways:
1. **Primary Programmatic Collection Pipeline** (`src/kroger_collector.py`): Queries the official Kroger Developer REST API.
2. **Browser Extraction Pipeline** (`src/scraper.py`): Playwright-based headless browser automation targeting `www.marianos.com`.

### 1.1 Primary Programmatic Route (Kroger Developer REST API)

* **OAuth2 Token Endpoint:**
  `https://api.kroger.com/v1/connect/oauth2/token`
* **OAuth Grant Type & Scope:**
  * Grant Type: `client_credentials`
  * Authorization Header: `Basic <base64(client_id:client_secret)>`
  * Content-Type: `application/x-www-form-urlencoded`
  * Requested Scope: `product.compact`
  * Token Lifecycle: Standard token validity is 1800 seconds (30 minutes). The collector proactively refreshes tokens 60 seconds prior to expiration (`self.token_expiry = time.time() + expires_in - 60`).
* **Locations Endpoint & Store Identity Verification:**
  * Endpoint: `https://api.kroger.com/v1/locations`
  * Query Parameters:
    * `filter.zipCode.near=60061`
    * `filter.limit=50`
  * Strict Verification Protocol:
    The collector queries locations near ZIP `60061` and iterates through the returned list. It strictly requires an exact match on `locationId == "53100502"`. It additionally validates that `addressLine1` contains `1720 N Milwaukee Ave`, `city` matches `Vernon Hills`, `state` matches `IL`, and `zipCode` begins with `60061`. If the location is absent or if any address component mismatches, execution immediately aborts (`KrogerAPIError`) before any product calls are issued.
* **Products Endpoint & Parameters:**
  * Endpoint: `https://api.kroger.com/v1/products`
  * Query Parameters:
    * `filter.term`: Search term (e.g. `milk`, `bread`, `cheese`).
    * `filter.locationId`: `53100502` (locks inventory and pricing strictly to the Vernon Hills store).
    * `filter.limit`: Number of items per response (configured to `50`, which is the maximum allowed by the API).
    * `filter.start`: Item skip offset.
* **Pagination Behavior & Skip Offset Bounds:**
  * Kroger's `filter.start` parameter represents an item-level skip offset (0-indexed).
  * For the initial page, `filter.start` is omitted (`start=0`).
  * For subsequent pages, Kroger's API strictly enforces: `1 <= filter.start <= 250`.
  * Passing `filter.start > 250` results in an immediate HTTP 400 Bad Request.
  * Consequently, the maximum reachable items for any single search term is `250 + 50 = 300` products.
  * To collect a catalog exceeding 500 products, the collector partitions queries across multiple categories and search terms (defined in `DEFAULT_SEARCH_TERMS`), rather than attempting to paginate a single term indefinitely.

### 1.2 Browser Extraction Route (Playwright Automation)

* **Entry Point:**
  `src/scraper.py` via `run_mariano_collection()` / `python -m scraper`.
* **Pages & Endpoints Targeted:**
  * Home / Root: `https://www.marianos.com`
  * Department & Category URLs: Sourced from `config/Marianos_Category_URLs.csv` (e.g., `https://www.marianos.com/d/produce`, `https://www.marianos.com/d/dairy`, `https://www.marianos.com/pl/candles-home-fragrances/32001?pzn=relevance`).
  * Subcategory Links: Discovers visible `a[href*="/pl/"]` links within department landings.
  * Pagination: Discovers `a[rel="next"]` or `a[aria-label*="Next"]` links.
  * Product Detail Pages: `https://www.marianos.com/p/{slug}/{upc}`.
* **Current Known Access Failure (Akamai Bot Manager WAF):**
  * In automated datacenter or headless execution, outbound network requests to `www.marianos.com` are intercepted by Akamai Bot Manager.
  * Requests fail with immediate TLS connection resets (`net::ERR_HTTP2_PROTOCOL_ERROR`) or HTTP 403 Forbidden challenge pages.
  * Because of this access restriction, the browser extraction pipeline is currently non-operational for automated execution. In accordance with safety rules, no bypass techniques are proposed or implemented.

---

## 2. Exact Field Mapping (22-Column Output Schema)

The output schema requires exactly 22 columns in the order below. Every value is strictly typed as a string (`""` for missing/unavailable fields; never `null` or unquoted numbers).

| # | Output Field Name | Source JSON Path / DOM Element | Transformation Applied | Availability Status | Evidence Status |
|---|---|---|---|---|---|
| 1 | `zipcode` | `address.zipCode` (API) or location config | Extracted 5-digit ZIP, cleaned to string. | Available | **Confirmed** in 5-product live proof (`"60061"`). |
| 2 | `store_name` | `store.name` (API) or location config | Normalized text string (`"Mariano's"`). | Available | **Confirmed** in 5-product live proof (`"Mariano's"`). |
| 3 | `store_location` | `address.addressLine1`, `city`, `state`, `zipCode` | Formatted: `"1720 N Milwaukee Ave, Vernon Hills, IL 60061"`. | Available | **Confirmed** in 5-product live proof. |
| 4 | `store_logo` | Not in API `product.compact` | Default empty string `""`. | Absent in captured responses | **Confirmed** empty string in live proof. |
| 5 | `store_brand` | `raw_item.brand` compared to private label brand set | `"True"` if private label (Simple Truth, Kroger, Roundy's, Private Selection), else `"False"`. | Available | **Confirmed** in 5-product live proof (`"True"`). |
| 6 | `category` | Top-level category config or `raw_item.categories[0]` | Cleaned text string (e.g. `"Dairy"`). | Available | **Confirmed** in 5-product live proof (`"Dairy"`). |
| 7 | `sub_category` | `raw_item.categories[1]` (API) or subcategory links (DOM) | Secondary category if distinct; otherwise empty string `""` or category fallback. | Partially Available | **Confirmed** in 5-product live proof (fallback to category). |
| 8 | `product_title` | `raw_item.description` (API) or DOM product header | Cleaned string; rejects empty titles. | Available | **Confirmed** in 5-product live proof. |
| 9 | `weight` | `items[0].size` or `raw_item.size` (API) | Cleaned string. Excluded if approximate/variable weight. | Available when populated | Populated in raw schema sample (`"1 gal"`); empty string when omitted. |
| 10 | `regular_price` | `items[0].price.regular` (API) or DOM price | 2-decimal string (e.g. `"4.49"`). Excluded if missing or zero. | Available | **Confirmed** in 5-product live proof (`"4.49"`, `"4.99"`, etc.). |
| 11 | `sale_price` | `items[0].price.promo` (API) or strikethrough DOM price | 2-decimal string if promo < regular; else `""`. | Available when discounted | Populated in raw sample (`"3.79"`); confirmed `""` when not on sale. |
| 12 | `volume_deals_description` | Not in inspected `product.compact` responses | String extracted from promotional badges if present, else `""`. | Absent in captured responses | Inferred from code; confirmed `""` in live proof. |
| 13 | `volume_deals_price` | Not in inspected `product.compact` responses | Bundle price string if stated; never computed per-unit. | Absent in captured responses | Inferred from code; confirmed `""` in live proof. |
| 14 | `digital_deals_description` | Not in inspected `product.compact` responses | Digital coupon text; requires coupon/loyalty scope. | Absent in captured responses | Inferred from code; confirmed `""` in live proof. |
| 15 | `digital_deals_price` | Not in inspected `product.compact` responses | Net price with digital coupon if explicitly stated. | Absent in captured responses | Inferred from code; confirmed `""` in live proof. |
| 16 | `image_url` | `images[perspective="front"].sizes[xlarge].url` (API) | Largest available front image URL string. | Available | **Confirmed** in 5-product live proof (`https://www.kroger.com/...`). |
| 17 | `url` | `productPageURI` (API) or canonical slug `/p/{slug}/{upc}` | Absolute HTTPS Mariano's URL string. | Available | **Confirmed** in 5-product live proof (`https://www.marianos.com/...`). |
| 18 | `upc` | `raw_item.upc` or `raw_item.productId` (API) | 13-digit zero-padded UPC string. | Available | **Confirmed** in 5-product live proof. |
| 19 | `gupc` | Specification: Global UPC | Always empty string `""` per client specification. | Schema placeholder | **Confirmed** empty string `""` in live proof. |
| 20 | `ppm` | Directly displayed Price Per Measure (e.g. `$0.15/oz`) | Absent in captured `product.compact` payloads. Kept as empty string `""`. Never synthesized. | Absent in captured responses | Inferred from code; confirmed `""` in live proof. |
| 21 | `unit_price` | Specification requirement | Always empty string `""` per client specification. | Schema placeholder | **Confirmed** empty string `""` in live proof. |
| 22 | `crawl_date` | Collector execution date | ISO date string: `YYYY-MM-DD`. | Available | **Confirmed** in 5-product live proof (`"2026-10-31"`). |

---

## 3. Explicitly Missing, Absent, or Unverified Data

Based on the inspected test dataset and captured evidence:

1. **Directly Displayed PPM (Price Per Measure):**
   * The inspected `product.compact` responses return package size (e.g. `1 gal`, `16 oz`) and total package pricing, but do not return the shelf-edge PPM string (e.g. `$0.07 / fl oz`).
   * Unless authoritative documentation proves otherwise, PPM is treated as absent from the inspected captured responses. Per specification rules, the collector strictly writes `""` for `ppm` and never synthesizes a unit price.

2. **Volume-Deal Descriptions & Prices:**
   * Promotional bundle deals (e.g., "Buy 2 for $5.00") are absent from the inspected `product.compact` responses.
   * Left strictly as empty strings (`volume_deals_description = ""`, `volume_deals_price = ""`).

3. **Digital-Deal Descriptions & Prices:**
   * Digital coupon details are absent from the inspected client-credentials `product.compact` responses (coupon clipping typically requires customer loyalty / profile scopes).
   * Left strictly as empty strings (`digital_deals_description = ""`, `digital_deals_price = ""`).

4. **Observed Fulfillment Flags vs Inventory Depth:**
   * **Observed Fulfillment Eligibility:** As demonstrated in `data/evidence/sample_raw_products.json` (lines 34–39), the raw API response does provide item-level boolean fulfillment eligibility indicators under `items[0].fulfillment`:
     * `instore`: `true` / `false`
     * `curbside`: `true` / `false`
     * `delivery`: `true` / `false`
   * **Distinction from Inventory Depth:** These boolean flags indicate channel eligibility, but do not provide quantitative stock availability (exact inventory on hand).

5. **Subcategory Granularity:**
   * Many products returned by `/v1/products` only contain a single top-level category tag in the `categories` array (e.g., `["Dairy"]`), omitting specific subcategories (e.g., `Milk & Cream`).
   * Where secondary category tags exist in the API response, they are extracted. When missing, `sub_category` is populated from category context rather than invented.

---

## 4. Evidence Distinction

To maintain complete audit integrity, facts are separated into three distinct categories:

### A. Facts Demonstrated by Previous Live Proof & Saved Evidence
* **Evidence Source:** `data/api_catalog_20261031T135542Z.json`, `data/report_20261031T135542Z.json`, `data/evidence/location_53100502.json`, and `data/evidence/sample_raw_products.json`.
* OAuth2 token acquisition succeeds with valid credentials against `https://api.kroger.com/v1/connect/oauth2/token`.
* Location verification against `/v1/locations` successfully retrieves store `53100502` at `1720 N Milwaukee Ave, Vernon Hills, IL 60061`.
* Products endpoint successfully returns real Mariano's product records for store `53100502` with valid UPCs, descriptions, regular prices, brand tags, and Kroger image URLs.
* Boolean fulfillment channel flags (`instore`, `curbside`, `delivery`) are present in `sample_raw_products.json`.
* All 22 fields conform strictly to the required schema, with unpopulated fields formatted as empty strings.

### B. Facts Established Only by Reading the Code
* Checkpoint interruption recovery (`KeyboardInterrupt` handler writing `interrupted` status to checkpoint file).
* Checkpoint validation rejecting completed (`ok`) checkpoints upon `--resume` with exit code 2.
* Bounded exponential backoff on HTTP 429 (`Retry-After`) and HTTP 5xx responses in `KrogerClient._request_with_retry`.
* Historical category retention (`replace_category_records`), keeping older valid records when an item is temporarily absent from a subsequent crawl.
* Safe normalized store matching (`location_record_matches`) preventing false mismatches from punctuation differences.
* Playwright DOM parsing, JSON-LD extraction, and department navigation logic in `src/scraper.py`.

### C. Facts Not Yet Verified
* **Genuine 500+ Product Live Demonstration:** While the collector is designed to partition queries across multiple terms to collect 500+ items, this has not been run live due to strict safety rules prohibiting live network calls during code review.
* **Full Live Scraper Execution:** The browser scraper cannot be run live in this environment due to Akamai WAF blocks.

---

## 5. Access Requirements

* **Kroger Developer API Access:**
  * Requires `KROGER_CLIENT_ID` and `KROGER_CLIENT_SECRET`.
  * A registered developer application on the Kroger Developer Portal (`developer.kroger.com`) with technically verified API access and the `product.compact` scope enabled.
  * *Local Environment Note:* Valid API credentials exist in the local project `.env` file. In accordance with safety rules, credentials are never printed, logged, or copied into version control.
  * *Reviewer Note:* Neither Darren nor the buyer should be assumed to have pre-configured Kroger Developer credentials. Provisioning instructions or deployment-ready environment variable injections must be coordinated.

* **Playwright Browser Access:**
  * Outbound HTTPS connections to `www.marianos.com` from automated cloud/datacenter IP ranges encounter Akamai Bot Manager blocks (`net::ERR_HTTP2_PROTOCOL_ERROR` or HTTP 403 Forbidden).
  * Consequently, automated browser scraping is blocked in cloud environments.

---

## 6. Alternative Collection Route Assessment

* **Current Status of Browser Scraper (`src/scraper.py`):**
  * The repository contains a complete Playwright implementation capable of extracting DOM product cards, JSON-LD metadata, and visible deal badges.
  * However, when executed against `www.marianos.com` in automated environments, Akamai Bot Manager intercepts requests with `net::ERR_HTTP2_PROTOCOL_ERROR` or HTTP 403 Forbidden.
* **Feasibility Assessment:**
  * The existing browser scraper cannot collect data in automated cloud deployments under its current access conditions.
  * In strict compliance with review safety rules, no bypass techniques, evasion methods, or proxy workarounds are attempted or proposed.
* **Recommendation:**
  * The Kroger Developer REST API remains the stable, reliable, and compliant collection route for the Mariano's #78 catalog.
  * Any supplementary fields not present in captured `product.compact` responses (PPM, loyalty coupons) remain empty strings in accordance with schema rules, rather than attempting fragile or prohibited browser bypasses.
