# Project X — code review and patch notes

## Files reviewed
- `src/scraper.py`
- `src/schema.py` (new)
- `tests/test_schema.py`
- `config/settings.example.json`
- `config/Marianos_Category_URLs.csv` (28 configured category entries)
- `README.md`, `Dockerfile`, `requirements.txt`
- `Shared_Scraper_Technical_Requirements.docx`

## Changes made in this revision
1. Moved schema and pure helper functions into `src/schema.py`, so schema tests do not need to import Playwright.
2. Fixed the dangerous price parsing case where `2 for $5` could be misread as a regular price of `$2.00`.
3. Added conservative volume-vs-digital deal text classification, private-label title tagging, and best-effort visible-text PPM/deal extraction on product pages. These heuristics still require live validation.
4. Added discovery of retailer-linked `/pl/` subcategory URLs and retailer-provided next-page URLs. This is not yet proof of exhaustive coverage or full handling of every pagination method.
5. Added stable per-location checkpoints and a `--resume` option that reloads saved records and skips categories previously marked successful.
6. Added stronger schema validation for string values, empty `gupc`/`unit_price`, approximate-weight exclusion, and location-verification evidence.
7. Added a fail-closed store identity check that requires both configured store name and street address to appear in visible header/store-selector text before category extraction. Added `--manual-store-selection` for an operator to choose a store in a headed browser, then confirm the visible identity. The same browser context is reused for all pages so site state can persist. This is not yet verified against a live Mariano's browser session.
8. Added unit coverage for store identity matching and updated README with the manual first-run workflow and its limitations.

## Local verification
- `python -m py_compile src/scraper.py src/schema.py`: passed.
- `python -m pytest -q`: 8 tests passed.
- Live Mariano's run: not performed; outbound network/DNS is unavailable in the current execution environment.

## Still required before contest submission
- Run `--headed --manual-store-selection` against the live site and confirm the exact store name/address are exposed in header/store-selector text; adapt selectors if needed.
- Verify actual selectors and data shapes on live Mariano's pages.
- Validate exact regular/sale price extraction and exact PPM/deal capture against representative real products.
- Ensure all 28 categories, nested subcategories, lazy loading and all pagination paths are exhausted.
- Add canary product checks, yield comparison/alerts, concurrency lock, Azure Blob output and twice-weekly Azure scheduling.
- Produce and manually audit a fresh real dataset with at least 500 valid records; capture location proof, counts, runtime, duplicates, completeness and operating costs.
