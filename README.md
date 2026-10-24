# Mariano's Azure Scraper — improved engineering scaffold

This is an **engineering scaffold, not a verified contest-ready scraper**. It reads the supplied category CSV, uses Playwright, attempts Schema.org Product JSON-LD plus retailer-visible product-page text, discovers visible `/pl/` links and retailer-provided next-page links, writes ordered JSON, records diagnostics, supports per-location category checkpoints, validates output, and publishes only after validation passes.

## Verified in this workspace
- Python syntax compilation succeeds.
- Unit tests cover the 22-field schema order, conservative price parsing, approximate-weight exclusion, UPC-first deduplication, private-label tagging, deal classification, and validation rules.
- Checkpoint code now reads a stable per-location checkpoint when `--resume` is supplied and skips categories previously marked `ok`.

## Not verified yet — do not claim these are complete
- A live end-to-end run against Mariano's from this environment (outbound DNS/network access is unavailable here).
- Fully automated retailer-specific store selection. A new `--manual-store-selection` workflow lets an operator choose the configured store in a headed browser, then requires the configured store name AND address to appear in visible header/store-selector text. The same browser context is reused across category/product pages so store cookies/local storage can persist. This is a fail-closed verification guard, not yet a live-verified Mariano's selector integration; it may reject a valid store if the retailer changes its header UI.
- Complete extraction of all product cards, every subcategory, all pagination patterns, exact regular-vs-sale price semantics, PPM, and volume/digital offers across the entire catalog. The current generic DOM text heuristics are best-effort and require validation against live product pages.
- A real 500+ record dataset, Azure deployment, twice-weekly scheduler, Azure Blob publication, canary checks, or actual cloud cost measurements.

## Setup
Use Python 3.11+.

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp config/settings.example.json config/settings.json
```

Edit `config/settings.json` with a **verified, permitted** store identifier/ZIP and the exact expected store name plus street address as shown by Mariano's. Do not guess the store ID and do not commit credentials or secrets. The name and address are both required for verification.

## Local run

```bash
python src/scraper.py --config config/settings.json
```

For an interactive store selection (recommended for the first local test), run a single location in a visible browser:

```bash
python src/scraper.py --config config/settings.json --location YOUR_LOCATION_ID --headed --manual-store-selection
```

When the browser opens, select the configured store in the Mariano's UI, return to the terminal, and press Enter. The scraper will stop if the visible header/store-selector text does not contain both the expected store name and address. This flow has not yet been confirmed against a live browser session in this environment.

To continue from a saved per-location checkpoint:

```bash
python src/scraper.py --config config/settings.json --resume
```

Review `logs/`, `data/report_*.json`, `data/checkpoints/`, and staging output. A rejected run exits with code 2 and preserves any existing successful `data/marianos_latest.json`.

## Azure target architecture (not deployed)
- Azure Container Apps Job or a Playwright-compatible container workload.
- Azure Blob Storage for immutable run artifacts and last-known-good output.
- Azure Key Vault for any permitted credentials.
- Azure Monitor / Log Analytics for logs and alerts.
- A twice-weekly schedule using the configured `America/Chicago` timezone, plus a distributed lock to prevent overlapping runs.

Azure resources must be provisioned in the owner's subscription. Do not put production credentials in the contest submission.

## Required production work before acceptance
1. Replace/confirm the generic manual store-selection workflow against the live retailer UI, and prove it on a headed live run.
2. Validate live retailer selectors/API responses under permitted access.
3. Recursively discover subcategories and fully handle pagination/lazy loading; report coverage for every CSV category.
4. Validate price fields, PPM, and distinct volume/digital deal text against real pages; never calculate missing values.
5. Add canary-product checks, per-category yield comparisons, challenge/shape detection, and interruption/restart tests.
6. Run a fresh observed collection with at least 500 valid real records; randomly verify records against the retailer.
7. Deploy to Azure, configure twice-weekly scheduling, test storage publication, and measure actual costs.

## Schema
Each output record contains exactly the required 22 fields in the required order. Missing values are empty strings. `gupc` and `unit_price` are empty at scraper stage. PPM is never calculated. Approximate-weight products are excluded.
