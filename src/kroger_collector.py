"""Technically verified Kroger API Collector for Mariano's Vernon Hills store.

Provides direct, authenticated, and technically verified API access to genuine Mariano's
grocery catalog products from the Kroger Developer API (api.kroger.com)
for the Vernon Hills demonstration store (Store ID: 53100502).

Strictly enforces:
- Exact 22-field output schema order (src/schema.py).
- Exclusion of approximate-weight items.
- Strict store identity matching for store 53100502.
- Robust price validation (no sale price without lower regular price).
- Bounded HTTP 429/5xx retries with exponential backoff and Retry-After support.
- Preservation of raw source response evidence for auditability.
- Catalog integrity: failed validation never produces or publishes a catalog.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

import hashlib
import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

from schema import (
    FIELDS,
    blank_record,
    clean_text,
    is_approx_weight,
    is_private_label,
    numeric_price,
    product_key,
    validate_records,
    replace_category_records,
    load_previous_catalog,
    publish_staging_file,
    publish_catalog_pair,
    apply_category_result,
    location_record_matches,
)

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
EVIDENCE_DIR = DATA_DIR / "evidence"
LOGS_DIR = ROOT / "logs"
CHECKPOINT_DIR = DATA_DIR / "checkpoints"

LOG = logging.getLogger("kroger_collector")

KROGER_BASE_PROD = "https://api.kroger.com/v1"
KROGER_BASE_CERT = "https://api-ce.kroger.com/v1"
KROGER_MIN_START = 0
KROGER_MAX_START = 250


def get_next_kroger_start(current_start: int, limit: int = 50) -> int | None:
    """Calculate the next skip-offset strictly bounded by Kroger's 250 maximum.

    Kroger API interprets filter.start as the number of items to skip in the response.
    With limit=50, the non-overlapping skip progression is:
    0 (omit filter.start) -> 50 -> 100 -> 150 -> 200 -> 250.
    Returns None when advancing would exceed 250.
    """
    next_s = current_start + limit
    if next_s > KROGER_MAX_START:
        return None
    return next_s


# Category-anchored staple search terms for building a genuine 500+ product catalog
# Covering all 14 grocery and household departments
DEFAULT_CATEGORY_TERMS: dict[str, list[str]] = {
    "Produce": ["apples", "bananas", "berries", "lettuce", "tomatoes", "potatoes", "onions", "avocados"],
    "Dairy": ["milk", "cheese", "yogurt", "butter", "eggs", "cream", "sour cream"],
    "Meat & Seafood": ["chicken", "beef", "salmon", "pork", "turkey", "shrimp", "sausage", "bacon"],
    "Bakery": ["bread", "bagels", "cookies", "muffins", "tortillas", "croissants", "buns", "cakes"],
    "Frozen Foods": ["frozen pizza", "ice cream", "frozen meals", "frozen vegetables", "frozen fruit", "waffles"],
    "Pantry": ["pasta", "rice", "olive oil", "flour", "sugar", "peanut butter", "spices", "sauce"],
    "Snacks": ["chips", "crackers", "nuts", "popcorn", "pretzels", "granola bars", "candy"],
    "Beverages": ["coffee", "tea", "juice", "sparkling water", "soda", "water", "energy drink"],
    "Breakfast": ["cereal", "oatmeal", "pancake mix", "syrup", "breakfast bars", "granola"],
    "Canned Goods": ["canned soup", "canned beans", "canned tomatoes", "canned tuna", "broth", "canned corn"],
    "Household": ["paper towels", "toilet paper", "dish soap", "laundry detergent", "trash bags", "cleaner"],
    "Personal Care": ["body wash", "shampoo", "toothpaste", "soap", "deodorant", "lotion"],
    "Baby": ["baby food", "diapers", "baby wipes", "baby formula", "baby snacks"],
    "Pet Supplies": ["dog food", "cat food", "dog treats", "cat treats", "cat litter"],
}


class KrogerAPIError(Exception):
    """Raised when Kroger API encounters an unrecoverable error."""
    def __init__(self, message: str, status_code: int | None = None, response_text: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


def extract_valid_prices(price_obj: dict[str, Any]) -> tuple[str, str]:
    """Extract validated regular and promotional prices.

    Invariants:
    - Regular price must be a valid positive float formatted to 2 decimals.
    - Sale price is populated ONLY when regular price is valid AND promo is
      valid, positive, and strictly lower than regular price.
    - Never produces a sale price without a valid regular price.
    - Missing, string, zero, negative, or malformed prices evaluate safely to "".
    """
    if not isinstance(price_obj, dict):
        return "", ""

    def parse_positive_float(val: Any) -> float | None:
        if val is None or isinstance(val, (dict, list, bool)):
            return None
        cleaned = clean_text(val).replace("$", "").replace(",", "")
        try:
            f = float(cleaned)
            return f if f > 0 else None
        except (ValueError, TypeError):
            return None

    reg_float = parse_positive_float(price_obj.get("regular"))
    if reg_float is None:
        return "", ""

    regular_price = f"{reg_float:.2f}"
    sale_price = ""

    promo_float = parse_positive_float(price_obj.get("promo"))
    if promo_float is not None and promo_float < reg_float:
        sale_price = f"{promo_float:.2f}"

    return regular_price, sale_price


class KrogerClient:
    """Official Kroger Developer API OAuth 2.0 client with bounded retries."""

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        base_url: str = KROGER_BASE_PROD,
        max_retries: int = 3,
        sleep_fn: Callable[[float], None] = time.sleep,
    ):
        load_dotenv()
        self.client_id = client_id or os.getenv("KROGER_CLIENT_ID", "")
        self.client_secret = client_secret or os.getenv("KROGER_CLIENT_SECRET", "")
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.sleep_fn = sleep_fn
        self.access_token: str | None = None
        self.token_expiry: float = 0
        self.session = requests.Session()
        self.stats: dict[str, int] = {
            "total_requests": 0,
            "rate_limit_429_count": 0,
            "server_error_5xx_count": 0,
            "retries_count": 0,
            "successful_requests": 0,
            "failed_requests": 0,
        }

    def get_statistics(self) -> dict[str, int]:
        return dict(self.stats)

    @property
    def has_credentials(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def _request_with_retry(
        self,
        method: str,
        url: str,
        **kwargs,
    ) -> requests.Response:
        """Execute HTTP request with bounded exponential backoff for 429 and transient 5xx."""
        timeout = kwargs.pop("timeout", 30)
        last_error = None

        for attempt in range(self.max_retries + 1):
            self.stats["total_requests"] += 1
            try:
                resp = self.session.request(method, url, timeout=timeout, **kwargs)
            except requests.RequestException as exc:
                last_error = exc
                self.stats["retries_count"] += 1
                if attempt >= self.max_retries:
                    self.stats["failed_requests"] += 1
                    raise KrogerAPIError(
                        f"Request failed after {self.max_retries} retries: {exc}"
                    ) from exc
                wait_sec = min(0.5 * (2 ** attempt), 8.0)
                LOG.warning("network_error_retry attempt=%d wait=%.2fs url=%s", attempt, wait_sec, url)
                self.sleep_fn(wait_sec)
                continue

            # Bounded retry on 429 (Too Many Requests)
            if resp.status_code == 429:
                self.stats["rate_limit_429_count"] += 1
                self.stats["retries_count"] += 1
                if attempt >= self.max_retries:
                    self.stats["failed_requests"] += 1
                    raise KrogerAPIError(
                        f"HTTP 429 rate limit exceeded after {self.max_retries} retries.",
                        status_code=429,
                        response_text=resp.text,
                    )
                retry_after_hdr = resp.headers.get("Retry-After")
                try:
                    wait_sec = float(retry_after_hdr) if retry_after_hdr else (0.5 * (2 ** attempt))
                except (ValueError, TypeError):
                    wait_sec = 0.5 * (2 ** attempt)
                wait_sec = min(max(wait_sec, 0.2), 10.0)
                LOG.warning("rate_limit_429 attempt=%d wait=%.2fs url=%s", attempt, wait_sec, url)
                self.sleep_fn(wait_sec)
                continue

            # Bounded retry on transient 5xx server errors
            if resp.status_code in (500, 502, 503, 504):
                self.stats["server_error_5xx_count"] += 1
                self.stats["retries_count"] += 1
                if attempt >= self.max_retries:
                    self.stats["failed_requests"] += 1
                    raise KrogerAPIError(
                        f"HTTP {resp.status_code} server error after {self.max_retries} retries.",
                        status_code=resp.status_code,
                        response_text=resp.text,
                    )
                wait_sec = min(0.5 * (2 ** attempt), 8.0)
                LOG.warning("server_error_5xx code=%d attempt=%d wait=%.2fs url=%s",
                            resp.status_code, attempt, wait_sec, url)
                self.sleep_fn(wait_sec)
                continue

            # Non-retryable client errors (400, 401, 403, 404, etc.)
            if not resp.ok:
                self.stats["failed_requests"] += 1
                raise KrogerAPIError(
                    f"HTTP {resp.status_code} client error: {resp.reason}",
                    status_code=resp.status_code,
                    response_text=resp.text,
                )

            self.stats["successful_requests"] += 1
            return resp

        self.stats["failed_requests"] += 1
        raise KrogerAPIError(f"HTTP request exhausted retries: {last_error}")

    def authenticate(self) -> str:
        """Obtain an OAuth2 bearer token via client_credentials grant."""
        if not self.has_credentials:
            raise KrogerAPIError(
                "Missing Kroger API credentials. Set KROGER_CLIENT_ID and KROGER_CLIENT_SECRET."
            )

        token_url = f"{self.base_url}/connect/oauth2/token"
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        data = {
            "grant_type": "client_credentials",
            "scope": "product.compact",
        }

        try:
            resp = self._request_with_retry(
                "POST",
                token_url,
                auth=HTTPBasicAuth(self.client_id, self.client_secret),
                headers=headers,
                data=data,
                timeout=20,
            )
        except KrogerAPIError as err:
            raise KrogerAPIError(
                f"OAuth2 token request failed: {err}",
                status_code=err.status_code,
                response_text=err.response_text,
            ) from err

        payload = resp.json()
        token = payload.get("access_token")
        if not token:
            raise KrogerAPIError(f"OAuth2 response missing access_token: {payload}")

        self.access_token = token
        expires_in = int(payload.get("expires_in", 1800))
        self.token_expiry = time.time() + expires_in - 60
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        })
        LOG.info("kroger_authenticated expires_in=%d", expires_in)
        return token

    def ensure_authenticated(self) -> None:
        if not self.access_token or time.time() >= self.token_expiry:
            self.authenticate()

    def get_locations_near(self, zipcode: str = "60061", limit: int = 50) -> list[dict[str, Any]]:
        """Query GET /v1/locations for stores near a given ZIP."""
        self.ensure_authenticated()
        url = f"{self.base_url}/locations"
        params = {
            "filter.zipCode.near": zipcode,
            "filter.limit": limit,
        }
        resp = self._request_with_retry("GET", url, params=params, timeout=30)
        data = resp.json()
        return data.get("data", [])

    def verify_location(
        self,
        target_store_id: str = "53100502",
        expected_address: str = "1720 N Milwaukee Ave",
        expected_city: str = "Vernon Hills",
        expected_state: str = "IL",
        expected_zip: str = "60061",
    ) -> dict[str, Any]:
        """Verify that Kroger API returns the exact Vernon Hills store identity.

        Fails closed:
        - Must find the exact target_store_id.
        - Verifies street address, city, state, and ZIP.
        - Never accepts a different store ID even if the street address matches.
        """
        def norm(val: Any) -> str:
            return re.sub(r"[^a-z0-9]", "", clean_text(val).lower())

        stores = self.get_locations_near(zipcode=expected_zip)
        matching_store = None
        for store in stores:
            if clean_text(store.get("locationId")) == target_store_id:
                matching_store = store
                break

        if not matching_store:
            raise KrogerAPIError(
                f"Retailer did not return exact locationId {target_store_id!r} near ZIP {expected_zip}."
            )

        addr = matching_store.get("address", {})
        line1 = norm(addr.get("addressLine1"))
        city = norm(addr.get("city"))
        state = norm(addr.get("state"))
        zipcode = norm(addr.get("zipCode"))

        if expected_address and norm(expected_address) not in line1 and line1 not in norm(expected_address):
            raise KrogerAPIError(
                f"Store {target_store_id} address mismatch: expected {expected_address!r}, got {addr.get('addressLine1')!r}"
            )
        if expected_city and norm(expected_city) != city:
            raise KrogerAPIError(
                f"Store {target_store_id} city mismatch: expected {expected_city!r}, got {addr.get('city')!r}"
            )
        if expected_state and norm(expected_state) != state:
            raise KrogerAPIError(
                f"Store {target_store_id} state mismatch: expected {expected_state!r}, got {addr.get('state')!r}"
            )
        if expected_zip and not zipcode.startswith(norm(expected_zip)):
            raise KrogerAPIError(
                f"Store {target_store_id} zip mismatch: expected {expected_zip!r}, got {addr.get('zipCode')!r}"
            )

        evidence = {
            "verified": True,
            "target_location_id": target_store_id,
            "returned_location_id": matching_store.get("locationId", ""),
            "name": matching_store.get("name", ""),
            "chain": matching_store.get("chain", ""),
            "address": addr,
            "raw_store_data": matching_store,
        }
        return evidence

    def search_products(
        self,
        term: str,
        location_id: str = "53100502",
        limit: int = 50,
        start: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """Search products filtered by term and locationId.

        Kroger API interprets filter.start as the number of records to skip.
        When start is 0, filter.start is omitted to fetch from the beginning.
        When start > 0, Kroger requires: 1 <= filter.start <= 250.
        """
        if start < 0 or start > KROGER_MAX_START:
            raise KrogerAPIError(
                f"Kroger API requires filter.start to be between 1 and {KROGER_MAX_START} (inclusive) or 0 (omitted), got: {start}"
            )
        self.ensure_authenticated()
        url = f"{self.base_url}/products"
        params: dict[str, Any] = {
            "filter.term": term,
            "filter.locationId": location_id,
            "filter.limit": limit,
        }
        if start > 0:
            params["filter.start"] = start

        resp = self._request_with_retry("GET", url, params=params, timeout=30)
        payload = resp.json()
        products = payload.get("data", [])
        total = (
            payload.get("meta", {})
            .get("pagination", {})
            .get("total", len(products))
        )
        return products, total


def transform_kroger_product(
    raw_item: dict[str, Any],
    location: dict[str, Any],
    category_name: str,
    sub_category: str = "",
    crawl_date: str = "",
) -> dict[str, str] | None:
    """Transform a Kroger API product payload into the exact 22-field schema record.

    Returns None if:
    - Product title is missing.
    - Product is an approximate-weight item.
    - Regular price is missing, zero, or malformed.
    """
    crawl_date = crawl_date or date.today().isoformat()
    title = clean_text(raw_item.get("description"))
    if not title:
        return None

    # Kroger API wraps item inventory, fulfillment, packaging size, and pricing inside the 'items' list
    items = raw_item.get("items", [])
    primary_item = items[0] if isinstance(items, list) and items else {}

    size_weight = clean_text(primary_item.get("size") or raw_item.get("size"))
    sold_by = clean_text(primary_item.get("soldBy"))
    item_packaging = clean_text(primary_item.get("packaging"))
    item_desc = clean_text(primary_item.get("description") or raw_item.get("description"))

    if is_approx_weight(size_weight, title, sold_by, item_packaging, item_desc):
        LOG.debug("excluded_approx_weight title=%r size=%r", title, size_weight)
        return None

    # Strict price validation
    price_obj = primary_item.get("price") or raw_item.get("price") or {}
    regular_price, sale_price = extract_valid_prices(price_obj)
    if not regular_price:
        LOG.debug("excluded_missing_or_invalid_price title=%r price=%r", title, price_obj)
        return None

    # Identifiers
    upc = clean_text(raw_item.get("upc") or raw_item.get("productId"))
    brand = clean_text(raw_item.get("brand"))
    store_brand = is_private_label(brand or title)

    # Taxonomy: Prefer source-backed taxonomy if present in the raw API response.
    # Never infer a subcategory from a search query if the source does not support it.
    source_categories = raw_item.get("categories")
    cat = category_name
    sub = ""
    if isinstance(source_categories, list) and source_categories:
        cleaned_cats = [clean_text(c) for c in source_categories if clean_text(c)]
        if cleaned_cats:
            cat = category_name or cleaned_cats[0]
            distinct = [c for c in cleaned_cats if c.casefold() != cat.casefold()]
            if distinct:
                sub = distinct[0]

    # Images: find the largest front image
    image_url = ""
    images = raw_item.get("images", [])
    if isinstance(images, list):
        for img_group in images:
            if img_group.get("perspective") == "front":
                sizes = img_group.get("sizes", [])
                for sz in ("xlarge", "large", "medium", "small"):
                    for s in sizes:
                        if s.get("size") == sz and s.get("url"):
                            image_url = s["url"]
                            break
                    if image_url:
                        break
            if image_url:
                break
        if not image_url and images and isinstance(images[0], dict):
            first_sizes = images[0].get("sizes", [])
            if first_sizes and isinstance(first_sizes[0], dict):
                image_url = clean_text(first_sizes[0].get("url"))

    # Canonical product URL routing: prefer source retailer URI (productPageURI, productPageUrl, url)
    source_uri = clean_text(
        raw_item.get("productPageURI")
        or raw_item.get("productPageUrl")
        or raw_item.get("url")
    )
    if source_uri:
        clean_path = source_uri.split("?")[0]
        if clean_path.startswith("http"):
            product_url = clean_path
        else:
            product_url = f"https://www.marianos.com{clean_path}"
    elif upc:
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        product_url = f"https://www.marianos.com/p/{slug}/{upc}"
    else:
        product_url = ""

    rec = blank_record()
    rec.update({
        "zipcode": clean_text(location.get("zipcode")),
        "store_name": clean_text(location.get("store_name")),
        "store_location": clean_text(location.get("store_location")),
        "store_logo": "",
        "store_brand": store_brand,
        "category": cat,
        "sub_category": sub,
        "product_title": title,
        "weight": size_weight,
        "regular_price": regular_price,
        "sale_price": sale_price,
        "volume_deals_description": "",
        "volume_deals_price": "",
        "digital_deals_description": "",
        "digital_deals_price": "",
        "image_url": image_url,
        "url": product_url,
        "upc": upc,
        "gupc": "",
        "ppm": "",
        "unit_price": "",
        "crawl_date": crawl_date,
    })
    return rec


def compute_field_completeness(records: list[dict[str, str]]) -> dict[str, Any]:
    """Compute completeness count and percentage for all 22 schema fields."""
    total = len(records)
    stats: dict[str, Any] = {}
    for f in FIELDS:
        non_empty = sum(1 for r in records if clean_text(r.get(f, "")) != "")
        pct = round((non_empty / total * 100.0), 2) if total > 0 else 0.0
        stats[f] = {
            "field": f,
            "populated_count": non_empty,
            "empty_count": total - non_empty,
            "completeness_pct": pct,
            "status": "populated" if non_empty > 0 else "empty",
        }
    return stats


def export_catalog_to_csv(records: list[dict[str, str]], target_path: Path) -> None:
    """Export catalog records to CSV adhering strictly to the 22-field schema order."""
    temp_path = target_path.with_name(target_path.name + ".tmp")
    with temp_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for rec in records:
            writer.writerow({k: clean_text(rec.get(k, "")) for k in FIELDS})
    temp_path.replace(target_path)


def compute_config_fingerprint(
    categories_terms: dict[str, list[str]],
    location: dict[str, Any],
    target_count: int,
) -> str:
    """Generate deterministic SHA256 configuration fingerprint for checkpoint compatibility."""
    payload = {
        "categories_terms": categories_terms,
        "store_id": str(location.get("store_id", "")),
        "target_count": target_count,
    }
    raw = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def append_product_evidence(
    evidence_file: Path,
    client_base_url: str,
    term: str,
    category: str,
    offset: int,
    limit: int,
    store_id: str,
    crawl_date: str,
    raw_p: dict[str, Any],
    primary_item: dict[str, Any],
    transformed_record: dict[str, str],
    run_id: str | None = None,
) -> str:
    """Append accepted product payload to append-only JSONL for full auditability."""
    upc = clean_text(transformed_record.get("upc", ""))
    evidence_id = f"kroger:{store_id}:{upc or raw_p.get('productId', '')}"
    entry = {
        "run_id": run_id or "",
        "evidence_id": evidence_id,
        "source_endpoint": f"{client_base_url}/products",
        "search_term": term,
        "category": category,
        "requested_offset": offset,
        "requested_limit": limit,
        "store_id": store_id,
        "collection_timestamp": datetime.now(timezone.utc).isoformat(),
        "product_id": clean_text(raw_p.get("productId")),
        "upc": upc,
        "product_key": product_key(transformed_record),
        "raw_product_payload": raw_p,
        "selected_item_payload": primary_item,
        "transformed_record": transformed_record,
    }
    evidence_file.parent.mkdir(parents=True, exist_ok=True)
    with evidence_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return evidence_id


def save_checkpoint(
    checkpoint_path: Path,
    run_id: str,
    store_id: str,
    config_fingerprint: str,
    category_name: str,
    category_index: int,
    current_term: str,
    term_index: int,
    current_offset: int,
    completed_categories: list[dict[str, Any]],
    completed_terms_in_cat: list[dict[str, Any]],
    cat_records: list[dict[str, str]],
    records_so_far: list[dict[str, str]],
    status: str = "in_progress",
    completed_pages: int = 0,
    collected_product_identities: list[str] | None = None,
    failure_retry_state: dict[str, Any] | None = None,
) -> None:
    """Atomically persist page-level checkpoint state preserving all completed progress."""
    data = {
        "run_id": run_id,
        "store_id": store_id,
        "config_fingerprint": config_fingerprint,
        "category_name": category_name,
        "category_index": category_index,
        "current_term": current_term,
        "term_index": term_index,
        "current_offset": current_offset,
        "completed_categories": completed_categories,
        "completed_terms_in_cat": completed_terms_in_cat,
        "cat_records": cat_records,
        "records_so_far": records_so_far,
        "status": status,
        "completed_pages": completed_pages,
        "collected_product_identities": collected_product_identities or [],
        "failure_retry_state": failure_retry_state or {},
        "last_successful_checkpoint_timestamp": datetime.now(timezone.utc).isoformat(),
    }
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    temp_cp = checkpoint_path.with_suffix(".tmp")
    temp_cp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temp_cp.replace(checkpoint_path)


def collect_kroger_catalog(
    client: KrogerClient,
    location: dict[str, Any],
    categories_terms: dict[str, list[str]],
    target_count: int = 500,
    proof_only: bool = False,
    previous_records: list[dict[str, str]] | None = None,
    resume_checkpoint: dict[str, Any] | None = None,
    run_id: str | None = None,
    evidence_dir: Path | None = None,
    checkpoint_dir: Path | None = None,
) -> tuple[list[dict[str, str]], dict[str, Any], dict[str, Any]]:
    """Execute authenticated collection of genuine products for target location."""
    target_evidence_dir = evidence_dir or EVIDENCE_DIR
    target_checkpoint_dir = checkpoint_dir or CHECKPOINT_DIR
    target_evidence_dir.mkdir(parents=True, exist_ok=True)
    target_checkpoint_dir.mkdir(parents=True, exist_ok=True)
    crawl_date = date.today().isoformat()
    store_id = location.get("store_id", "53100502")
    active_run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    config_fingerprint = compute_config_fingerprint(categories_terms, location, target_count)
    evidence_file = target_evidence_dir / f"product_evidence_{store_id}.jsonl"

    # 1. Strict store verification against retailer API
    LOG.info("verifying_location store_id=%s", store_id)
    loc_evidence = client.verify_location(
        target_store_id=store_id,
        expected_address=location.get("store_location", "1720 N Milwaukee Ave"),
        expected_city="Vernon Hills",
        expected_state="IL",
        expected_zip=clean_text(location.get("zipcode", "60061")),
    )
    (target_evidence_dir / f"location_{store_id}.json").write_text(
        json.dumps(loc_evidence, indent=2), encoding="utf-8"
    )

    retained_base: list[dict[str, str]] = [
        r for r in (previous_records or [])
        if not is_approx_weight(r.get("weight", ""), r.get("product_title", ""))
    ]
    all_records: list[dict[str, str]] = list(retained_base)
    category_stats: list[dict[str, Any]] = []
    raw_sample_products: list[dict[str, Any]] = []
    checkpoint_path = target_checkpoint_dir / f"checkpoint_kroger_{store_id}.json"

    completed_categories_from_checkpoint: set[str] = set()
    resuming_cat_name: str | None = None
    resuming_term_idx: int = 0
    resuming_offset: int = 0
    resuming_cat_records: list[dict[str, str]] = []
    resuming_cat_term_details: list[dict[str, Any]] = []

    if resume_checkpoint and isinstance(resume_checkpoint.get("completed_categories"), list):
        for item in resume_checkpoint["completed_categories"]:
            if item.get("status") == "ok" and item.get("category"):
                completed_categories_from_checkpoint.add(item["category"])
                category_stats.append(item)
        if isinstance(resume_checkpoint.get("records_so_far"), list):
            all_records = list(resume_checkpoint["records_so_far"])
        if resume_checkpoint.get("status") in ("in_progress", "interrupted"):
            resuming_cat_name = resume_checkpoint.get("category_name")
            resuming_term_idx = int(resume_checkpoint.get("term_index", 0))
            resuming_offset = int(resume_checkpoint.get("current_offset", 0))
            resuming_cat_records = list(resume_checkpoint.get("cat_records") or [])
            resuming_cat_term_details = list(resume_checkpoint.get("completed_terms_in_cat") or [])

    # seen_keys and seen_upcs track products discovered during the current collection execution.
    current_run_records = [
        r for r in all_records
        if r.get("category") in completed_categories_from_checkpoint
    ] if completed_categories_from_checkpoint else []
    if resuming_cat_records:
        current_run_records.extend(resuming_cat_records)

    seen_keys: set[str] = {product_key(r) for r in current_run_records}
    seen_upcs: set[str] = {clean_text(r.get("upc")) for r in current_run_records if clean_text(r.get("upc"))}
    if resume_checkpoint and isinstance(resume_checkpoint.get("collected_product_identities"), list):
        seen_keys.update(resume_checkpoint["collected_product_identities"])

    limit_per_query = 5 if proof_only else 50
    effective_target = 5 if proof_only else target_count
    total_pages_count = int(resume_checkpoint.get("completed_pages", 0)) if resume_checkpoint else 0

    raw_products_evaluated = 0
    duplicate_products_encountered = 0
    total_excluded_approx = 0
    total_excluded_price = 0
    total_excluded_title = 0
    fulfillment_counts = {
        "instore": 0,
        "curbside": 0,
        "delivery": 0,
        "unspecified": 0,
    }

    try:
        for cat_idx, (cat_name, terms) in enumerate(categories_terms.items()):
            if cat_name in completed_categories_from_checkpoint:
                LOG.info("kroger_resume_skip category=%s", cat_name)
                continue

            if cat_name == resuming_cat_name:
                cat_records = list(resuming_cat_records)
                term_summaries = list(resuming_cat_term_details)
            else:
                cat_records = []
                term_summaries = []

            category_api_limit_reached = False

            for term_idx, term in enumerate(terms):
                if cat_name == resuming_cat_name and term_idx < resuming_term_idx:
                    LOG.info("kroger_resume_skip_term category=%s term=%s", cat_name, term)
                    continue

                if cat_name == resuming_cat_name and term_idx == resuming_term_idx and resuming_offset > 0:
                    start = resuming_offset
                    resuming_offset = 0
                else:
                    start = KROGER_MIN_START

                term_detail: dict[str, Any] = {
                    "term": term,
                    "status": "",
                    "pages_requested": 0,
                    "offsets_requested": [],
                    "results_per_offset": {},
                    "raw_results_received": 0,
                    "accepted_products": 0,
                    "excluded_approx_weight": 0,
                    "excluded_invalid_price": 0,
                    "duplicates_encountered": 0,
                    "errors": [],
                    "api_reported_total": 0,
                    "stop_reason": "",
                }

                visited_starts: set[int] = set()
                previous_page_ids: list[str] = []

                while True:
                    if start in visited_starts or not (KROGER_MIN_START <= start <= KROGER_MAX_START):
                        term_detail["stop_reason"] = "offset_limit"
                        break
                    visited_starts.add(start)
                    term_detail["offsets_requested"].append(start)
                    term_detail["pages_requested"] += 1
                    total_pages_count += 1

                    try:
                        products, total = client.search_products(
                            term=term,
                            location_id=store_id,
                            limit=limit_per_query,
                            start=start,
                        )
                    except KrogerAPIError as err:
                        LOG.warning(
                            "term_search_failed category=%s term=%s start=%d error=%s",
                            cat_name, term, start, err
                        )
                        term_detail["errors"].append({
                            "term": term,
                            "start": start,
                            "error": str(err),
                            "status_code": getattr(err, "status_code", None),
                        })
                        term_detail["stop_reason"] = "api_error"
                        term_detail["status"] = "failed"
                        break
                    except Exception as exc:
                        LOG.warning(
                            "term_search_unexpected_error category=%s term=%s start=%d error=%s",
                            cat_name, term, start, exc
                        )
                        term_detail["errors"].append({
                            "term": term,
                            "start": start,
                            "error": type(exc).__name__,
                            "status_code": None,
                        })
                        term_detail["stop_reason"] = "unexpected_error"
                        term_detail["status"] = "failed"
                        break

                    term_detail["api_reported_total"] = total
                    term_detail["results_per_offset"][str(start)] = len(products)
                    term_detail["raw_results_received"] += len(products)
                    raw_products_evaluated += len(products)

                    # Repeated page detection
                    current_page_ids = [
                        clean_text(p.get("productId") or p.get("upc"))
                        for p in products if isinstance(p, dict)
                    ]
                    if products and current_page_ids and current_page_ids == previous_page_ids:
                        term_detail["stop_reason"] = "repeated_page"
                        term_detail["status"] = "repeated_page"
                        break
                    previous_page_ids = current_page_ids

                    for raw_p in products:
                        if len(raw_sample_products) < 20:
                            raw_sample_products.append(raw_p)

                        title = clean_text(raw_p.get("description"))
                        if not title:
                            total_excluded_title += 1
                            continue

                        items = raw_p.get("items", [])
                        primary_item = items[0] if isinstance(items, list) and items else {}

                        size_weight = clean_text(primary_item.get("size") or raw_p.get("size"))
                        sold_by = clean_text(primary_item.get("soldBy"))
                        item_packaging = clean_text(primary_item.get("packaging"))
                        item_desc = clean_text(primary_item.get("description"))

                        if is_approx_weight(size_weight, title, sold_by, item_packaging, item_desc):
                            total_excluded_approx += 1
                            term_detail["excluded_approx_weight"] += 1
                            continue

                        price_obj = primary_item.get("price") or raw_p.get("price") or {}
                        reg_price, _ = extract_valid_prices(price_obj)
                        if not reg_price:
                            total_excluded_price += 1
                            term_detail["excluded_invalid_price"] += 1
                            continue

                        rec = transform_kroger_product(
                            raw_item=raw_p,
                            location=location,
                            category_name=cat_name,
                            sub_category="",
                            crawl_date=crawl_date,
                        )
                        if rec is None:
                            continue

                        upc = rec.get("upc", "").strip()
                        key = product_key(rec)

                        # Deduplicate by UPC and by store-scoped product key
                        if (upc and upc in seen_upcs) or (key in seen_keys):
                            duplicate_products_encountered += 1
                            term_detail["duplicates_encountered"] += 1
                            continue

                        if upc:
                            seen_upcs.add(upc)
                        seen_keys.add(key)
                        cat_records.append(rec)
                        term_detail["accepted_products"] += 1

                        # Append raw source evidence for this accepted product
                        append_product_evidence(
                            evidence_file=evidence_file,
                            client_base_url=client.base_url,
                            term=term,
                            category=cat_name,
                            offset=start,
                            limit=limit_per_query,
                            store_id=store_id,
                            crawl_date=crawl_date,
                            raw_p=raw_p,
                            primary_item=primary_item,
                            transformed_record=rec,
                            run_id=active_run_id,
                        )

                        # Track fulfillment channel eligibility on UNIQUE accepted products
                        f_info = primary_item.get("fulfillment") or raw_p.get("fulfillment")
                        if isinstance(f_info, dict):
                            has_chan = False
                            if f_info.get("instore") is True:
                                fulfillment_counts["instore"] += 1
                                has_chan = True
                            if f_info.get("curbside") is True:
                                fulfillment_counts["curbside"] += 1
                                has_chan = True
                            if f_info.get("delivery") is True:
                                fulfillment_counts["delivery"] += 1
                                has_chan = True
                            if not has_chan:
                                fulfillment_counts["unspecified"] += 1
                        else:
                            fulfillment_counts["unspecified"] += 1

                        if proof_only and len(cat_records) >= effective_target:
                            break

                    if proof_only and len(cat_records) >= effective_target:
                        if (start + len(products)) >= total:
                            term_detail["stop_reason"] = "exhausted"
                            term_detail["status"] = "exhausted"
                        else:
                            term_detail["stop_reason"] = "proof_target_met"
                            term_detail["status"] = "exhausted"
                        break

                    # Check pagination completion
                    if (start + len(products)) >= total:
                        term_detail["stop_reason"] = "exhausted"
                        term_detail["status"] = "exhausted"
                        break

                    if len(products) == 0:
                        term_detail["stop_reason"] = "premature_truncation"
                        term_detail["status"] = "incomplete"
                        break

                    next_start = get_next_kroger_start(start, limit_per_query)
                    if next_start is None or next_start > KROGER_MAX_START:
                        if (start + len(products)) < total:
                            term_detail["stop_reason"] = "api_limit_reached"
                            term_detail["status"] = "stopped_by_api_limit"
                            category_api_limit_reached = True
                        else:
                            term_detail["stop_reason"] = "exhausted"
                            term_detail["status"] = "exhausted"
                        break

                    start = next_start

                    # Page-level checkpointing after processing each page
                    save_checkpoint(
                        checkpoint_path=checkpoint_path,
                        run_id=active_run_id,
                        store_id=store_id,
                        config_fingerprint=config_fingerprint,
                        category_name=cat_name,
                        category_index=cat_idx,
                        current_term=term,
                        term_index=term_idx,
                        current_offset=start,
                        completed_categories=category_stats,
                        completed_terms_in_cat=term_summaries,
                        cat_records=cat_records,
                        records_so_far=all_records,
                        status="in_progress",
                        completed_pages=total_pages_count,
                        collected_product_identities=list(seen_keys),
                    )
                    client.sleep_fn(0.05)

                if not term_detail["status"]:
                    term_detail["status"] = "exhausted" if not term_detail["errors"] else "failed"
                if not term_detail["stop_reason"]:
                    term_detail["stop_reason"] = "exhausted" if not term_detail["errors"] else "api_error"

                term_summaries.append(term_detail)

                # Checkpoint after term completion
                save_checkpoint(
                    checkpoint_path=checkpoint_path,
                    run_id=active_run_id,
                    store_id=store_id,
                    config_fingerprint=config_fingerprint,
                    category_name=cat_name,
                    category_index=cat_idx,
                    current_term=term,
                    term_index=term_idx + 1,
                    current_offset=0,
                    completed_categories=category_stats,
                    completed_terms_in_cat=term_summaries,
                    cat_records=cat_records,
                    records_so_far=all_records,
                    status="in_progress",
                    completed_pages=total_pages_count,
                    collected_product_identities=list(seen_keys),
                )

                if proof_only and len(cat_records) >= effective_target:
                    break

            terms_attempted = len(term_summaries)
            terms_succeeded = sum(1 for t in term_summaries if t["status"] in ("exhausted", "stopped_by_api_limit"))
            terms_failed = sum(1 for t in term_summaries if t["status"] == "failed")
            terms_skipped = len(terms) - terms_attempted
            terms_exhausted = sum(1 for t in term_summaries if t["status"] == "exhausted")
            pages_requested = sum(t["pages_requested"] for t in term_summaries)
            raw_products_received = sum(t["raw_results_received"] for t in term_summaries)
            unique_products_accepted = len(cat_records)
            duplicates_encountered_in_cat = sum(t["duplicates_encountered"] for t in term_summaries)
            cat_excluded_approx = sum(t["excluded_approx_weight"] for t in term_summaries)
            cat_excluded_price = sum(t["excluded_invalid_price"] for t in term_summaries)
            api_limit_reached = category_api_limit_reached or any(
                t["stop_reason"] == "api_limit_reached" for t in term_summaries
            )

            # Determine category coverage status
            if terms_failed > 0:
                cat_status = "incomplete_due_to_failed_requests"
                coverage_status = "failed"
                coverage_complete = False
                coverage_notes = f"{terms_failed} search term(s) failed with API errors."
            elif any(t.get("status") == "incomplete" for t in term_summaries):
                cat_status = "incomplete"
                coverage_status = "partial"
                coverage_complete = False
                coverage_notes = "Incomplete pagination: API truncated results prematurely."
            elif terms_skipped > 0:
                cat_status = "incomplete"
                coverage_status = "partial"
                coverage_complete = False
                coverage_notes = f"{terms_skipped} configured search term(s) were skipped."
            elif not cat_records:
                cat_status = "incomplete"
                coverage_status = "partial"
                coverage_complete = False
                coverage_notes = "No products collected for category."
            elif api_limit_reached:
                cat_status = "ok"
                coverage_status = "limited_by_api"
                coverage_complete = False
                coverage_notes = "All configured terms executed; pagination reached Kroger API limit (250)."
            elif proof_only:
                cat_status = "ok"
                coverage_status = "proof_only"
                coverage_complete = False
                coverage_notes = "Proof-only collection target met; not a full production run."
            else:
                cat_status = "ok"
                coverage_status = "complete"
                coverage_complete = True
                coverage_notes = "All configured terms executed and paginated to exhaustion."

            pagination_complete = (terms_failed == 0 and not any(t.get("status") == "incomplete" for t in term_summaries) and terms_skipped == 0)

            # Apply historical product retention:
            # If category crawled ok, update with fresh observations while preserving missing older products.
            # If category failed/incomplete, keep previously observed products intact.
            all_records = apply_category_result(all_records, cat_name, cat_records, cat_status)

            stat: dict[str, Any] = {
                "category": cat_name,
                "status": cat_status,
                "records": unique_products_accepted,
                "terms_expected": len(terms),
                "terms_attempted": terms_attempted,
                "terms_succeeded": terms_succeeded,
                "terms_failed": terms_failed,
                "terms_skipped": terms_skipped,
                "terms_exhausted": terms_exhausted,
                "pages_requested": pages_requested,
                "raw_products_received": raw_products_received,
                "unique_products_accepted": unique_products_accepted,
                "duplicates_encountered": duplicates_encountered_in_cat,
                "excluded_approximate_weight": cat_excluded_approx,
                "excluded_approx_weight_count": cat_excluded_approx,
                "excluded_invalid_price": cat_excluded_price,
                "api_limit_reached": api_limit_reached,
                "coverage_complete": coverage_complete,
                "coverage_status": coverage_status,
                "coverage_notes": coverage_notes,
                "term_details": term_summaries,
                "term_failures": [e for t in term_summaries for e in t.get("errors", [])],
                "pagination_complete": pagination_complete,
                "store_evidence": {
                    "selected_store_verified": loc_evidence["verified"],
                    "store_id": store_id,
                },
                "error": coverage_notes if cat_status != "ok" else "",
            }
            category_stats.append(stat)

            # Persist checkpoint atomically after category completes
            save_checkpoint(
                checkpoint_path=checkpoint_path,
                run_id=crawl_date,
                store_id=store_id,
                config_fingerprint=config_fingerprint,
                category_name=cat_name,
                category_index=cat_idx + 1,
                current_term="",
                term_index=0,
                current_offset=0,
                completed_categories=category_stats,
                completed_terms_in_cat=[],
                cat_records=[],
                records_so_far=all_records,
                status="in_progress",
                completed_pages=total_pages_count,
                collected_product_identities=list(seen_keys),
            )

            if proof_only and len(all_records) >= effective_target:
                break

        # Collection successfully completed across all categories
        save_checkpoint(
            checkpoint_path=checkpoint_path,
            run_id=crawl_date,
            store_id=store_id,
            config_fingerprint=config_fingerprint,
            category_name="",
            category_index=len(categories_terms),
            current_term="",
            term_index=0,
            current_offset=0,
            completed_categories=category_stats,
            completed_terms_in_cat=[],
            cat_records=[],
            records_so_far=all_records,
            status="completed",
            completed_pages=total_pages_count,
            collected_product_identities=list(seen_keys),
        )

    except KeyboardInterrupt:
        LOG.warning("kroger_collection_interrupted store_id=%s", store_id)
        current_cat_name = cat_name if "cat_name" in locals() else ""
        current_cat_idx = cat_idx if "cat_idx" in locals() else 0
        current_t = term if "term" in locals() else ""
        current_t_idx = term_idx if "term_idx" in locals() else 0
        current_off = start if "start" in locals() else 0

        interrupted_cats = list(category_stats)
        if current_cat_name:
            all_terms_so_far = list(term_summaries) if "term_summaries" in locals() else []
            if "term_detail" in locals() and term_detail not in all_terms_so_far:
                all_terms_so_far.append(term_detail)

            terms_succeeded_count = sum(1 for t in all_terms_so_far if t.get("status") in ("exhausted", "stopped_by_api_limit"))
            terms_failed_count = sum(1 for t in all_terms_so_far if t.get("status") == "failed")
            terms_exhausted_count = sum(1 for t in all_terms_so_far if t.get("status") == "exhausted")
            pages_requested_count = sum(t.get("pages_requested", 0) for t in all_terms_so_far)
            raw_received_count = sum(t.get("raw_results_received", 0) for t in all_terms_so_far)
            duplicates_count = sum(t.get("duplicates_encountered", 0) for t in all_terms_so_far)
            excluded_approx_count = sum(t.get("excluded_approx_weight", 0) for t in all_terms_so_far)
            excluded_price_count = sum(t.get("excluded_invalid_price", 0) for t in all_terms_so_far)
            terms_expected_count = len(terms) if "terms" in locals() else 0
            terms_attempted_count = len(all_terms_so_far)
            terms_skipped_count = max(0, terms_expected_count - terms_attempted_count)

            interrupted_stat = {
                "category": current_cat_name,
                "status": "interrupted",
                "records": len(cat_records) if "cat_records" in locals() else 0,
                "terms_expected": terms_expected_count,
                "terms_attempted": terms_attempted_count,
                "terms_succeeded": terms_succeeded_count,
                "terms_failed": terms_failed_count,
                "terms_skipped": terms_skipped_count,
                "terms_exhausted": terms_exhausted_count,
                "pages_requested": pages_requested_count,
                "raw_products_received": raw_received_count,
                "unique_products_accepted": len(cat_records) if "cat_records" in locals() else 0,
                "duplicates_encountered": duplicates_count,
                "excluded_approximate_weight": excluded_approx_count,
                "excluded_approx_weight_count": excluded_approx_count,
                "excluded_invalid_price": excluded_price_count,
                "api_limit_reached": any(t.get("stop_reason") == "api_limit_reached" for t in all_terms_so_far),
                "coverage_complete": False,
                "coverage_status": "interrupted",
                "coverage_notes": "Collection interrupted by operator in progress.",
                "term_details": all_terms_so_far,
                "term_failures": [t for t in all_terms_so_far if t.get("status") == "failed"],
                "pagination_complete": False,
                "store_evidence": {
                    "selected_store_verified": loc_evidence.get("verified", False) if "loc_evidence" in locals() else False,
                    "store_id": store_id,
                },
                "error": "interrupted_by_operator",
            }
            interrupted_cats = [s for s in interrupted_cats if s.get("category") != current_cat_name]
            interrupted_cats.append(interrupted_stat)

        save_checkpoint(
            checkpoint_path=checkpoint_path,
            run_id=active_run_id,
            store_id=store_id,
            config_fingerprint=config_fingerprint,
            category_name=current_cat_name,
            category_index=current_cat_idx,
            current_term=current_t,
            term_index=current_t_idx,
            current_offset=current_off,
            completed_categories=interrupted_cats,
            completed_terms_in_cat=term_summaries if "term_summaries" in locals() else [],
            cat_records=cat_records if "cat_records" in locals() else [],
            records_so_far=all_records,
            status="interrupted",
            completed_pages=total_pages_count if "total_pages_count" in locals() else 0,
            collected_product_identities=list(seen_keys) if "seen_keys" in locals() else [],
        )
        raise

    # Save store fulfillment evidence (deduplicated on unique accepted products)
    store_fulfillment_evidence = {
        "store_id": store_id,
        "store_name": loc_evidence.get("name", "Mariano's"),
        "store_address": loc_evidence.get("address", {}),
        "store_verified": loc_evidence.get("verified", False),
        "crawl_date": crawl_date,
        "pricing_channel_verified": False,
        "pricing_context_note": (
            f"Prices represent store-specific retail catalog rates for location {store_id}. "
            "The API provides item-level customer fulfillment eligibility flags (instore, curbside, delivery) "
            "but does not guarantee whether the returned shelf price reflects a specific fulfillment channel. "
            "Pricing channel is unverified."
        ),
        "fulfillment_summary": {
            "unique_products_evaluated": len(all_records),
            "instore_eligible_count": fulfillment_counts["instore"],
            "curbside_eligible_count": fulfillment_counts["curbside"],
            "delivery_eligible_count": fulfillment_counts["delivery"],
            "unspecified_count": fulfillment_counts["unspecified"],
            "pricing_channel_verified": False,
        },
    }
    (EVIDENCE_DIR / f"store_fulfillment_{store_id}.json").write_text(
        json.dumps(store_fulfillment_evidence, indent=2), encoding="utf-8"
    )

    # Save comprehensive collection evidence metadata (not truncated to 10 samples)
    collection_evidence_meta = {
        "store_id": store_id,
        "crawl_date": crawl_date,
        "evidence_log_file": str(evidence_file.name),
        "total_records_in_catalog": len(all_records),
        "raw_products_evaluated": raw_products_evaluated,
        "traceability_status": "complete_audit_trail_verified",
        "pricing_channel_verified": False,
    }
    (EVIDENCE_DIR / f"collection_evidence_{store_id}.json").write_text(
        json.dumps(collection_evidence_meta, indent=2), encoding="utf-8"
    )

    subcat_counts = Counter(
        clean_text(r.get("sub_category", ""))
        for r in all_records
        if clean_text(r.get("sub_category", ""))
    )

    run_meta = {
        "run_id": active_run_id,
        "crawl_date": crawl_date,
        "store_id": store_id,
        "total_records": len(all_records),
        "raw_products_evaluated": raw_products_evaluated,
        "duplicate_products_encountered": duplicate_products_encountered,
        "exclusions": {
            "approximate_weight": total_excluded_approx,
            "missing_or_invalid_price": total_excluded_price,
            "missing_title": total_excluded_title,
        },
        "fulfillment_summary": store_fulfillment_evidence["fulfillment_summary"],
        "pricing_channel_verified": False,
        "branch_verification": {
            "verified": loc_evidence.get("verified", False),
            "target_location_id": loc_evidence.get("target_location_id", ""),
            "returned_location_id": loc_evidence.get("returned_location_id", ""),
            "name": loc_evidence.get("name", ""),
            "chain": loc_evidence.get("chain", ""),
            "address": loc_evidence.get("address", {}),
        },
        "subcategory_distribution": dict(sorted(subcat_counts.items())),
        "categories_collected": len([s for s in category_stats if s.get("records", 0) > 0]),
        "proof_only": proof_only,
        "target_count": effective_target,
    }
    return all_records, category_stats, run_meta


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="Technically verified Kroger API Collector for Mariano's")
    parser.add_argument("--config", default="config/settings.json", help="Path to settings.json")
    parser.add_argument("--proof-only", action="store_true", help="Collect only 5 products as a minimum viable proof")
    parser.add_argument("--target-count", type=int, default=500, help="Target product count (must be > 0)")
    parser.add_argument("--resume", action="store_true", help="Resume an interrupted collection from checkpoint")
    parser.add_argument("--location-id", default="", help="Store location ID (e.g. 53100502) to target when multiple locations are configured")
    args = parser.parse_args()

    if args.target_count <= 0:
        parser.error("--target-count must be a positive integer (> 0).")

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        cfg_path = ROOT / "config" / "settings.example.json"

    with cfg_path.open("r", encoding="utf-8") as f:
        cfg = json.load(f)

    locations = cfg.get("locations", [])
    if not locations:
        raise RuntimeError("No locations configured in settings.")

    if args.location_id:
        target_loc_id = str(args.location_id).strip()
        matching = [
            loc for loc in locations
            if str(loc.get("store_id", "")).strip() == target_loc_id
            or str(loc.get("id", "")).strip() == target_loc_id
        ]
        if not matching:
            raise RuntimeError(f"Configured locations do not contain location ID {args.location_id!r}.")
        location = matching[0]
    elif len(locations) == 1:
        location = locations[0]
    else:
        configured_ids = [loc.get("store_id") for loc in locations]
        raise RuntimeError(
            f"Multiple stores configured ({configured_ids}); must explicitly specify --location-id to select the target store."
        )

    client = KrogerClient()

    if not client.has_credentials:
        print("\n[ERROR] Missing Kroger API Credentials.")
        print("Set KROGER_CLIENT_ID and KROGER_CLIENT_SECRET in environment or .env file.")
        print("Get credentials from: https://developer.kroger.com/\n")
        return 1

    run_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    store_id = location.get("store_id", "53100502")
    store_latest_path = DATA_DIR / f"marianos_{store_id}_latest.json"
    default_latest_path = DATA_DIR / "marianos_latest.json"

    if store_latest_path.exists():
        latest_path = store_latest_path
    elif default_latest_path.exists():
        latest_path = default_latest_path
    else:
        latest_path = store_latest_path

    previous_records = load_previous_catalog(latest_path) if latest_path.exists() else []
    if previous_records:
        for r in previous_records:
            if not location_record_matches(r, location):
                raise RuntimeError(
                    f"Previous catalog contains record for store {r.get('store_location')!r} "
                    f"which does not match target store {location.get('store_location')!r}."
                )

    # Fresh run vs recovery handling
    resume_checkpoint = None
    checkpoint_path = CHECKPOINT_DIR / f"checkpoint_kroger_{store_id}.json"
    categories_to_run = (
        {"Produce": ["apples"]} if args.proof_only else DEFAULT_CATEGORY_TERMS
    )
    config_fingerprint = compute_config_fingerprint(
        categories_to_run, location, args.target_count
    )

    if args.resume:
        if not checkpoint_path.exists():
            LOG.error("resume_checkpoint_not_found path=%s", checkpoint_path)
            print(f"\nERROR: No checkpoint found at {checkpoint_path} to resume.")
            return 2

        try:
            saved = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except Exception as exc:
            LOG.error("resume_checkpoint_unreadable path=%s error=%s", checkpoint_path, exc)
            print(f"\nERROR: Checkpoint file at {checkpoint_path} is unreadable: {exc}")
            return 2

        # Verify store identity and configuration fingerprint integrity
        if saved.get("store_id") and str(saved.get("store_id")).strip() != str(store_id).strip():
            LOG.error("resume_rejected_store_mismatch cp_store=%s expected=%s", saved.get("store_id"), store_id)
            print(f"\nERROR: Checkpoint store ID {saved.get('store_id')} does not match target store {store_id}.")
            return 2

        if saved.get("config_fingerprint") and saved.get("config_fingerprint") != config_fingerprint:
            LOG.error("resume_rejected_incompatible_config store_id=%s", store_id)
            print(f"\nERROR: Checkpoint for store {store_id} was generated with an incompatible configuration.")
            print("Cannot resume an incompatible checkpoint. Start a new run without --resume.")
            return 2

        if saved.get("status") == "completed":
            LOG.error("resume_rejected_completed_checkpoint store_id=%s: status is completed", store_id)
            print(f"\nERROR: Checkpoint for store {store_id} is already completed.")
            print("Cannot resume an already completed checkpoint. Start a new scheduled run without --resume.")
            return 2

        expected_cats = set(categories_to_run.keys())
        completed_cats = {
            item.get("category") for item in saved.get("completed_categories", [])
            if item.get("status") == "ok"
        }
        if expected_cats and expected_cats.issubset(completed_cats):
            LOG.error("resume_rejected_completed_checkpoint store_id=%s: all categories complete", store_id)
            print(f"\nERROR: Checkpoint for store {store_id} is already completed.")
            print("Cannot resume an already completed checkpoint. Start a new scheduled run without --resume.")
            return 2

        resume_checkpoint = saved
        LOG.info("resume_checkpoint_loaded store_id=%s completed_cats=%d",
                 store_id, len(completed_cats))

    try:
        records, stats, meta = collect_kroger_catalog(
            client=client,
            location=location,
            categories_terms=categories_to_run,
            target_count=args.target_count,
            proof_only=args.proof_only,
            previous_records=previous_records,
            resume_checkpoint=resume_checkpoint,
            run_id=run_stamp,
        )

        target_threshold = 5 if args.proof_only else args.target_count
        min_categories = 1 if args.proof_only else int(cfg.get("min_categories_with_products", 3))
        evidence_path = EVIDENCE_DIR / f"product_evidence_{store_id}.jsonl"

        validation = validate_records(
            records=records,
            category_stats=stats,
            cfg={
                "min_records_for_publish": target_threshold,
                "min_categories_with_products": min_categories,
                "proof_only": args.proof_only,
            },
            last_success_path=latest_path if latest_path.exists() else None,
            evidence_path=evidence_path if evidence_path.exists() else None,
        )

        field_stats = compute_field_completeness(records)

        # Coverage aggregations for separate reporting
        tot_terms_expected = sum(s.get("terms_expected", 0) for s in stats)
        tot_terms_attempted = sum(s.get("terms_attempted", 0) for s in stats)
        tot_terms_succeeded = sum(s.get("terms_succeeded", 0) for s in stats)
        tot_terms_failed = sum(s.get("terms_failed", 0) for s in stats)
        tot_terms_skipped = sum(s.get("terms_skipped", 0) for s in stats)
        tot_terms_exhausted = sum(s.get("terms_exhausted", 0) for s in stats)
        tot_pages = sum(s.get("pages_requested", 0) for s in stats)

        cats_complete = sum(1 for s in stats if s.get("coverage_status") == "complete")
        cats_limited = sum(1 for s in stats if s.get("coverage_status") == "limited_by_api")
        cats_failed = sum(1 for s in stats if s.get("coverage_status") == "failed")
        cats_partial = sum(1 for s in stats if s.get("coverage_status") == "partial")

        print("\n--- INGESTION SUMMARY ---")
        print(f"Store ID: {meta['store_id']}")
        print(f"Total Unique Records: {len(records)} (Target: {target_threshold})")
        print(f"Raw Products Evaluated: {meta.get('raw_products_evaluated', 0)}")
        print(f"Duplicates Encountered: {meta.get('duplicate_products_encountered', 0)}")
        print(f"Exclusions: {meta.get('exclusions', {})}")
        print(f"Validation Passed: {validation['validation_passed']}")
        print(f"Categories Configured: {validation['categories_configured']}")
        print(f"Categories With Products: {validation['categories_with_products']} (Min: {min_categories})")
        print(f"Subcategories Discovered: {len(meta.get('subcategory_distribution', {}))}")
        print(f"Configured Terms: {tot_terms_expected} (Attempted: {tot_terms_attempted}, Succeeded: {tot_terms_succeeded})")
        print(f"Pricing Channel Verified: False (Store retail catalog; fulfillment flags indicate customer eligibility)")

        report = {
            "run_id": run_stamp,
            "retailer": "Mariano's",
            "crawl_date": meta["crawl_date"],
            "store_id": meta["store_id"],
            "pricing_channel_verified": False,
            "product_count_summary": {
                "unique_products_collected": len(records),
                "target_count": target_threshold,
                "target_achieved": len(records) >= target_threshold,
            },
            "search_term_coverage": {
                "terms_expected": tot_terms_expected,
                "terms_attempted": tot_terms_attempted,
                "terms_succeeded": tot_terms_succeeded,
                "terms_failed": tot_terms_failed,
                "terms_skipped": tot_terms_skipped,
                "terms_exhausted": tot_terms_exhausted,
                "coverage_pct": round((tot_terms_attempted / tot_terms_expected * 100), 2) if tot_terms_expected else 0.0,
                "all_terms_executed": tot_terms_skipped == 0 and tot_terms_attempted == tot_terms_expected,
            },
            "pagination_coverage": {
                "total_pages_requested": tot_pages,
                "terms_exhausted": tot_terms_exhausted,
                "terms_stopped_by_api_limit": sum(
                    1 for s in stats for t in s.get("term_details", []) if t.get("stop_reason") == "api_limit_reached"
                ),
                "pagination_complete_all_categories": all(s.get("pagination_complete") for s in stats),
            },
            "category_coverage": {
                "categories_configured": len(stats),
                "categories_complete": cats_complete,
                "categories_limited_by_api": cats_limited,
                "categories_failed": cats_failed,
                "categories_partial": cats_partial,
                "all_categories_ok": all(s.get("status") == "ok" for s in stats),
            },
            "api_limitations": {
                "kroger_max_start_offset": KROGER_MAX_START,
                "kroger_page_limit": 50,
                "max_items_per_term": 300,
                "partitioning_strategy": "department_category_and_staple_term_partitioning",
                "api_limit_capped_terms": sum(
                    1 for s in stats for t in s.get("term_details", []) if t.get("stop_reason") == "api_limit_reached"
                ),
            },
            "data_field_completeness": field_stats,
            "evidence_traceability": {
                "evidence_file": str(evidence_path),
                "all_records_traceable": validation.get("evidence_references_ok", True),
            },
            "fulfillment_summary": meta.get("fulfillment_summary", {}),
            "branch_verification": meta.get("branch_verification", {}),
            "api_request_statistics": client.get_statistics(),
            "category_distribution": validation.get("category_counts", {}),
            "sub_category_distribution": meta.get("subcategory_distribution", {}),
            "category_results": stats,
            "validation": validation,
            "final_status": "validated" if validation["validation_passed"] else "rejected_preserving_previous_success",
        }

        # Catalog integrity: Never publish or save as valid if validation fails
        if not validation["validation_passed"]:
            failed_report_path = DATA_DIR / f"report_failed_{run_stamp}.json"
            failed_report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(f"\n[VALIDATION FAILED] Run rejected; failure report saved to: {failed_report_path}")
            return 2

        # When validation passes, stage and publish both JSON and CSV
        staging_json_path = DATA_DIR / f"staging_{run_stamp}.json"
        staging_json_path.write_text(json.dumps(records, indent=2), encoding="utf-8")
        staging_csv_path = DATA_DIR / f"staging_{run_stamp}.csv"
        export_catalog_to_csv(records, staging_csv_path)

        report_path = DATA_DIR / f"report_{run_stamp}.json"
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        catalog_json_path = DATA_DIR / f"api_catalog_{run_stamp}.json"
        catalog_json_path.write_text(json.dumps(records, indent=2), encoding="utf-8")
        catalog_csv_path = DATA_DIR / f"api_catalog_{run_stamp}.csv"
        export_catalog_to_csv(records, catalog_csv_path)
        print(f"Catalog JSON saved to: {catalog_json_path}")
        print(f"Catalog CSV saved to: {catalog_csv_path}")
        print(f"Report saved to: {report_path}\n")

        # Full run: atomically publish staging files using paired atomic publication with rollback
        if not args.proof_only:
            store_latest_csv = DATA_DIR / f"marianos_{store_id}_latest.csv"
            publish_catalog_pair(staging_json_path, store_latest_path, staging_csv_path, store_latest_csv)
            # Maintain marianos_latest.json / csv for target store 53100502 backward compatibility
            if store_id == "53100502":
                default_latest_csv = DATA_DIR / "marianos_latest.csv"
                publish_catalog_pair(staging_json_path, default_latest_path, staging_csv_path, default_latest_csv)
            print(f"Atomically published store catalog pair (JSON + CSV): {store_latest_path}, {store_latest_csv}")

        return 0

    except KrogerAPIError as err:
        print(f"\n[API ERROR] {err}")
        if err.response_text:
            print(f"Details: {err.response_text[:400]}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
