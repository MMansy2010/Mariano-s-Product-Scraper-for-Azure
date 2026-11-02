from __future__ import annotations

import hashlib
import argparse
import asyncio
import csv
import json
import logging
import os
import re
import shutil
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from schema import (
    FIELDS,
    blank_record,
    clean_text,
    numeric_price,
    is_approx_weight,
    classify_deals,
    is_private_label,
    product_key,
    validate_records,
    verify_store_identity,
    replace_category_records,
    location_record_matches,
    load_previous_catalog,
    apply_category_result,
    publish_staging_file,
)

LOG = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
CHECKPOINT_DIR = DATA_DIR / "checkpoints"

VALID_CATEGORY_STATUSES = {
    "ok",
    "incomplete",
    "failed",
    "incomplete_failed_requests",
    "incomplete_due_to_failed_requests",
    "blocked_or_challenged",
    "repeated_pagination_page",
    "product_parse_failure",
    "interrupted",
    "interrupted_collection",
    "unverified_store_context",
    "unexpected_or_unverified_store_context",
}

OUTCOME_FULLY_EXHAUSTED = "fully_exhausted_and_successful"
OUTCOME_FAILED_REQUESTS = "incomplete_due_to_failed_requests"
OUTCOME_BLOCKED_OR_CHALLENGED = "blocked_or_challenged"
OUTCOME_REPEATED_PAGE = "repeated_pagination_page"
OUTCOME_PRODUCT_PARSE_FAILURE = "product_parse_failure"
OUTCOME_INTERRUPTED = "interrupted_collection"
OUTCOME_UNVERIFIED_STORE = "unexpected_or_unverified_store_context"


def publish_to_azure_blob(staging_path: Path, report_path: Path, run_stamp: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """Upload validated run artifacts and latest catalog to Azure Blob Storage if configured."""
    azure_cfg = cfg.get("azure", {})
    conn_str = os.getenv("AZURE_STORAGE_CONNECTION_STRING") or azure_cfg.get("connection_string", "")
    container_name = os.getenv("AZURE_STORAGE_CONTAINER_NAME") or azure_cfg.get("container_name", "marianos-catalog")
    if not conn_str or not container_name:
        LOG.info("azure_blob_publish_skipped (connection string or container name not configured)")
        return {"published": False, "reason": "not_configured"}

    try:
        from azure.storage.blob import BlobServiceClient
    except ImportError:
        LOG.warning("azure_blob_publish_skipped (azure-storage-blob package not installed)")
        return {"published": False, "reason": "package_not_installed"}

    try:
        client = BlobServiceClient.from_connection_string(conn_str)
        container_client = client.get_container_client(container_name)
        if not container_client.exists():
            container_client.create_container()

        catalog_blob = container_client.get_blob_client(f"runs/{run_stamp}/catalog.json")
        catalog_blob.upload_blob(staging_path.read_bytes(), overwrite=True)

        report_blob = container_client.get_blob_client(f"runs/{run_stamp}/report.json")
        report_blob.upload_blob(report_path.read_bytes(), overwrite=True)

        latest_blob = container_client.get_blob_client("marianos_latest.json")
        latest_blob.upload_blob(staging_path.read_bytes(), overwrite=True)

        LOG.info("azure_blob_publish_success container=%s run=%s", container_name, run_stamp)
        return {"published": True, "container": container_name, "run_stamp": run_stamp}
    except Exception as exc:
        LOG.error("azure_blob_publish_failed error=%s", type(exc).__name__)
        return {"published": False, "error": f"{type(exc).__name__}: {str(exc)}"}


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_DIR / f"run_{stamp}.log", encoding="utf-8"),
        ],
        force=True,
    )


def load_config(path: Path) -> dict[str, Any]:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    if not cfg.get("locations"):
        raise ValueError("At least one configured location is required.")
    return cfg


def load_categories(path: Path) -> list[dict[str, str]]:
    rows = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            url = (row.get("URL") or "").strip()
            name = (row.get("Category (Auto)") or "").strip()
            if not url or not name:
                continue
            rows.append({
                "retailer": (row.get("Retailer") or "Mariano's").strip(),
                "category": name,
                "url": url,
                "scope": (row.get("Scope") or "").strip(),
            })
    return rows


def checkpoint_fingerprint(
    location: dict[str, Any],
    categories: list[dict[str, str]],
) -> str:
    """Create a stable fingerprint for the selected location and category inputs."""
    identity = {
        "location": {
            key: location.get(key, "")
            for key in (
                "id",
                "zipcode",
                "store_id",
                "store_name",
                "store_location",
            )
        },
        "categories": [
            {
                "retailer": item.get("retailer", ""),
                "category": item.get("category", ""),
                "url": item.get("url", ""),
                "scope": item.get("scope", ""),
            }
            for item in categories
        ],
    }

    canonical = json.dumps(
        identity,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()



def is_checkpoint_compatible(
    saved: Any,
    location: dict[str, Any],
    expected_fingerprint: str,
) -> bool:
    """Return whether a saved checkpoint is safe to resume."""
    if not isinstance(saved, dict):
        return False

    if saved.get("location_id") != location.get("id"):
        return False

    if saved.get("config_fingerprint") != expected_fingerprint:
        return False

    if not isinstance(saved.get("completed_categories"), list):
        return False

    if not isinstance(saved.get("records_so_far"), list):
        return False

    if not all(
        isinstance(item, dict)
        for item in saved["completed_categories"]
    ):
        return False

    if not all(
        isinstance(record, dict) and list(record.keys()) == FIELDS
        for record in saved["records_so_far"]
    ):
        return False

    expected_fields = ("zipcode", "store_name", "store_location",
                       "category", "product_title", "url", "crawl_date")
    for record in saved["records_so_far"]:
        if not all(isinstance(value, str) for value in record.values()):
            return False
        if any(not record.get(field, "").strip() for field in expected_fields):
            return False
        if record.get("gupc") != "" or record.get("unit_price") != "":
            return False
        if is_approx_weight(record.get("weight", ""), record.get("product_title", "")):
            return False
        if not location_record_matches(record, location):
            return False

    keys = [product_key(record) for record in saved["records_so_far"]]
    if len(keys) != len(set(keys)):
        return False

    seen_categories = set()
    for item in saved["completed_categories"]:
        category = item.get("category")
        if not isinstance(category, str) or not category.strip():
            return False
        if category in seen_categories:
            return False
        seen_categories.add(category)
        if item.get("status") not in VALID_CATEGORY_STATUSES:
            return False

    return True



async def page_store_evidence(page, location: dict[str, Any] | None = None) -> dict[str, Any]:
    # Evidence only: do not claim a location is selected unless page text supports it.
    body = ""
    try:
        body = await page.locator("body").inner_text(timeout=5000)
    except Exception:
        pass
    title = await page.title()
    header_parts = []
    for selector in ["header", '[role="banner"]', '[data-testid*="store"]', '[aria-label*="store" i]']:
        try:
            locator = page.locator(selector).first
            if await locator.count():
                candidate = clean_text(await locator.inner_text(timeout=1200))
                if candidate and candidate not in header_parts:
                    header_parts.append(candidate[:1000])
        except Exception:
            continue
    header_text = " | ".join(header_parts)[:2500]
    # Verify only from the visible store/header text. Product-page content or configured
    # values alone are not evidence that the intended branch is active.
    location = location or {}
    selected = verify_store_identity(header_text, location)
    return {
        "title": clean_text(title),
        "url": page.url,
        "header_text": header_text,
        "body_excerpt": clean_text(body)[:1200],
        "expected_store_name": clean_text(location.get("store_name")),
        "expected_store_location": clean_text(location.get("store_location")),
        "selected_store_verified": selected,
    }


async def extract_jsonld(page) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    scripts = page.locator('script[type="application/ld+json"]')
    for i in range(await scripts.count()):
        try:
            raw = await scripts.nth(i).inner_text(timeout=1500)
            data = json.loads(raw)
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
                continue
            if not isinstance(item, dict):
                continue
            typ = item.get("@type", "")
            if typ == "Product" or (isinstance(typ, list) and "Product" in typ):
                out.append(item)
            for key in ("@graph", "mainEntity", "itemListElement"):
                val = item.get(key)
                if isinstance(val, (dict, list)):
                    stack.append(val)
    return out


async def discover_product_links(page) -> list[str]:
    links = await page.locator('a[href*="/p/"], a[href*="/product/"]').evaluate_all(
        "(els) => els.map(a => a.href).filter(Boolean)"
    )
    seen = set()
    result = []
    for link in links:
        if link not in seen:
            seen.add(link)
            result.append(link)
    return result


async def maybe_load_more(page) -> dict[str, Any]:
    """Scroll until the product-link count is stable; no arbitrary iteration cap."""
    prior = -1
    stable = 0
    iterations = 0
    while stable < 3:
        try:
            count = await page.locator('a[href*="/p/"], a[href*="/product/"]').count()
        except Exception as exc:
            return {"complete": False, "reason": f"link_count_error:{type(exc).__name__}",
                    "iterations": iterations, "links": max(prior, 0)}
        if count == prior:
            stable += 1
        else:
            stable = 0
        if stable >= 3:
            break
        prior = count
        iterations += 1
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(700)
    return {"complete": True, "reason": "stable_product_link_count",
            "iterations": iterations, "links": max(prior, 0)}


PPM_RE = re.compile(r"\$\s*\d+(?:\.\d{1,4})?\s*/\s*(?:fl\s*oz|oz|lb|ct|qt|gal|ea|each|100\s*g|100\s*ml)", re.I)


async def visible_product_facts(page) -> dict[str, str]:
    """Extract conservative product-page facts; never promote savings text into a price."""
    facts = {"ppm": "", "volume_description": "", "digital_description": "",
             "volume_price": "", "digital_price": "", "regular_price": "", "sale_price": ""}
    try:
        body = await page.locator("body").inner_text(timeout=3000)
    except Exception:
        body = ""
    lines = [clean_text(line) for line in body.splitlines() if clean_text(line)]
    for line in lines:
        volume, digital = classify_deals(line)
        if digital and not facts["digital_description"]:
            facts["digital_description"] = digital
            # Accept only a price explicitly stated as the price with the digital coupon.
            match = re.search(r"\$\s*(\d+(?:\.\d{1,2})?)\s*(?:with|w/)\s*(?:the\s*)?digital coupon", line, re.I)
            if match:
                facts["digital_price"] = match.group(1)
        elif volume and not facts["volume_description"]:
            facts["volume_description"] = volume
            # Retain only an explicit offer amount, never calculate a per-item price.
            amount = re.search(r"\b\d+\s*for\s*\$\s*(\d+(?:\.\d{1,2})?)\b", line, re.I)
            if not amount:
                amount = re.search(r"\$\s*(\d+(?:\.\d{1,2})?)\s*(?:each|ea\.?)", line, re.I)
            if amount:
                facts["volume_price"] = amount.group(1)

    # Use a current product price only when the page exposes a product-price element.
    current_price = ""
    for selector in ['[data-testid="product-price"]', '[data-testid*="price"]',
                     '[class*="ProductPrice"]', '[class*="product-price"]']:
        try:
            locator = page.locator(selector).first
            if await locator.count():
                current_price = numeric_price(await locator.inner_text(timeout=1000))
                if current_price:
                    break
        except Exception:
            continue

    original_price = ""
    for selector in ['del', 's', '[class*="strikethrough"]', '[class*="original-price"]']:
        try:
            locator = page.locator(selector).first
            if await locator.count():
                original_price = numeric_price(await locator.inner_text(timeout=800))
                if original_price:
                    break
        except Exception:
            continue

    if current_price and original_price:
        facts["regular_price"] = original_price
        facts["sale_price"] = current_price
    elif current_price:
        facts["regular_price"] = current_price

    # PPM is deliberately blank until a product-specific, structurally bound source is verified.
    return facts

async def discover_subcategory_links(page, parent_url: str) -> list[dict[str, str]]:
    """Discover visible /pl/ category links from a department page."""
    try:
        items = await page.locator('a[href*="/pl/"]').evaluate_all(
            "els => els.map(a => ({url:a.href, name:(a.innerText||a.getAttribute('aria-label')||'').trim()}))"
        )
    except Exception:
        return []
    parent = urlparse(parent_url)
    seen: set[str] = set()
    found: list[dict[str, str]] = []
    for item in items:
        url = clean_text(item.get("url"))
        name = clean_text(item.get("name"))
        parsed = urlparse(url)
        if not url or not name or parsed.netloc != parent.netloc or "/pl/" not in parsed.path:
            continue
        if url in seen:
            continue
        seen.add(url)
        found.append({"url": url, "sub_category": name})
    return found


async def discover_next_page_url(page) -> str:
    """Return a retailer-provided next-page URL when available; never synthesize page numbers."""
    selectors = [
        'a[aria-label*="Next" i][href]', 'a[rel="next"][href]',
        'button[aria-label*="Next" i][data-href]'
    ]
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if await loc.count():
                href = await loc.get_attribute("href") or await loc.get_attribute("data-href")
                if href:
                    return urljoin(page.url, href)
        except Exception:
            pass
    return ""


async def parse_product_page(browser, url: str, meta: dict[str, str], location: dict[str, Any],
                             crawl_date: str, timeout_ms: int) -> dict[str, Any]:
    page = await browser.new_page()
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        if response and response.status >= 400:
            reason = f"http_{response.status}"
            LOG.warning("product_http_error status=%s url=%s", response.status, url)
            return {"status": "parse_failed", "record": None, "reason": reason, "title": ""}

        await page.wait_for_timeout(500)
        products = await extract_jsonld(page)
        p = products[0] if products else {}
        title = clean_text(p.get("name"))

        if not title:
            try:
                title = clean_text(await page.locator("h1").first.inner_text(timeout=2000))
            except Exception:
                title = ""

        if not title:
            return {
                "status": "parse_failed", "record": None,
                "reason": "missing_product_title", "title": ""
            }

        raw_weight = clean_text(p.get("weight") or p.get("size"))
        if is_approx_weight(raw_weight, title):
            LOG.info("excluded_approx_weight title=%s url=%s", title, url)
            return {
                "status": "excluded_approx_weight", "record": None,
                "reason": "approximate_weight", "title": title
            }

        visible = await visible_product_facts(page)
        rec = blank_record()
        rec.update({
            "zipcode": clean_text(location.get("zipcode")),
            "store_name": clean_text(location.get("store_name")),
            "store_location": clean_text(location.get("store_location")),
            "store_logo": "",
            "store_brand": is_private_label(title),
            "category": meta.get("category", ""),
            "sub_category": meta.get("sub_category", ""),
            "product_title": title,
            "weight": raw_weight,
            "regular_price": visible["regular_price"],
            "sale_price": visible["sale_price"],
            "volume_deals_description": visible["volume_description"],
            "volume_deals_price": visible["volume_price"],
            "digital_deals_description": visible["digital_description"],
            "digital_deals_price": visible["digital_price"],
            "image_url": clean_text(p.get("image")),
            "url": clean_text(p.get("url")) or url,
            "upc": clean_text(p.get("gtin12") or p.get("gtin13") or p.get("gtin14") or p.get("gtin")),
            "gupc": "",
            "ppm": visible["ppm"],
            "unit_price": "",
            "crawl_date": crawl_date,
        })

        if isinstance(p.get("image"), list):
            rec["image_url"] = clean_text(p["image"][0]) if p["image"] else ""

        return {"status": "success", "record": rec, "reason": "", "title": title}

    except PlaywrightTimeoutError:
        LOG.warning("product_timeout url=%s", url)
        return {
            "status": "parse_failed", "record": None,
            "reason": "timeout", "title": ""
        }
    except Exception as e:
        LOG.warning("product_parse_error url=%s error=%s", url, type(e).__name__)
        return {
            "status": "parse_failed", "record": None,
            "reason": type(e).__name__, "title": ""
        }
    finally:
        await page.close()


async def crawl_category(browser, category: dict[str, str], location: dict[str, Any],
                         cfg: dict[str, Any], crawl_date: str) -> tuple[list[dict[str, str]], dict[str, Any]]:
    url = category["url"]
    name = category["category"]
    max_retries = int(cfg.get("max_retries", 3))
    timeout_ms = int(cfg.get("request_timeout_ms", 45000))
    delay = float(cfg.get("navigation_delay_seconds", 2.0))
    last_error = ""
    last_outcome = OUTCOME_FAILED_REQUESTS
    last_status = "incomplete_failed_requests"

    for attempt in range(1, max_retries + 1):
        page = await browser.new_page()
        try:
            LOG.info("category_start location=%s category=%s attempt=%d url=%s",
                     location.get("id"), name, attempt, url)
            response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            if response and response.status >= 400:
                last_error = f"http_{response.status}"
                last_outcome = OUTCOME_FAILED_REQUESTS
                last_status = "incomplete_failed_requests"
                LOG.warning("category_http_error category=%s status=%s", name, response.status)
                await page.close()
                await asyncio.sleep(delay * attempt)
                continue

            await page.wait_for_timeout(1200)
            evidence = await page_store_evidence(page, location)
            if not evidence["selected_store_verified"]:
                last_error = "selected_store_not_verified"
                last_outcome = OUTCOME_UNVERIFIED_STORE
                last_status = "unverified_store_context"
                LOG.error("store_verification_failed location=%s expected_name=%r expected_address=%r header=%r",
                          location.get("id"), location.get("store_name"), location.get("store_location"),
                          evidence.get("header_text", ""))
                await page.close()
                break

            body_lower = (evidence["body_excerpt"] + " " + evidence.get("header_text", "")).lower()
            if any(term in body_lower for term in ("access denied", "verify you are human", "robot or human", "unusual traffic")):
                last_error = "possible_block_or_challenge"
                last_outcome = OUTCOME_BLOCKED_OR_CHALLENGED
                last_status = "blocked_or_challenged"
                LOG.error("possible_block category=%s url=%s", name, page.url)
                await page.close()
                break

            scroll_result = await maybe_load_more(page)
            if not scroll_result.get("complete"):
                last_error = "incomplete_infinite_scroll"
                last_outcome = OUTCOME_FAILED_REQUESTS
                last_status = "incomplete_failed_requests"
                LOG.error("infinite_scroll_incomplete category=%s details=%r", name, scroll_result)
                await page.close()
                break

            product_links = await discover_product_links(page)

            # Discover and follow retailer-linked subcategory pages
            subcategories = await discover_subcategory_links(page, url)
            subcategory_product_links: list[tuple[str, str]] = []
            subcategory_failures: list[dict[str, Any]] = []

            for sub in subcategories:
                sub_url = sub["url"]
                sub_name = sub["sub_category"]
                sub_page = await browser.new_page()
                try:
                    sub_response = await sub_page.goto(sub_url, wait_until="domcontentloaded", timeout=timeout_ms)
                    if sub_response and sub_response.status >= 400:
                        LOG.warning("subcategory_http_error category=%s subcategory=%s status=%s", name, sub_name, sub_response.status)
                        subcategory_failures.append({
                            "subcategory": sub_name,
                            "url": sub_url,
                            "error": f"http_{sub_response.status}",
                            "outcome": OUTCOME_FAILED_REQUESTS,
                        })
                        continue

                    await sub_page.wait_for_timeout(700)
                    sub_evidence = await page_store_evidence(sub_page, location)
                    if not sub_evidence.get("selected_store_verified"):
                        LOG.error("subcategory_store_unverified category=%s subcategory=%s", name, sub_name)
                        subcategory_failures.append({
                            "subcategory": sub_name,
                            "url": sub_url,
                            "error": "selected_store_not_verified",
                            "outcome": OUTCOME_UNVERIFIED_STORE,
                        })
                        continue

                    sub_body_lower = (sub_evidence.get("body_excerpt", "") + " " + sub_evidence.get("header_text", "")).lower()
                    if any(term in sub_body_lower for term in ("access denied", "verify you are human", "robot or human", "unusual traffic")):
                        LOG.error("subcategory_block category=%s subcategory=%s", name, sub_name)
                        subcategory_failures.append({
                            "subcategory": sub_name,
                            "url": sub_url,
                            "error": "possible_block_or_challenge",
                            "outcome": OUTCOME_BLOCKED_OR_CHALLENGED,
                        })
                        continue

                    sub_scroll = await maybe_load_more(sub_page)
                    if not sub_scroll.get("complete"):
                        subcategory_failures.append({
                            "subcategory": sub_name,
                            "url": sub_url,
                            "error": "incomplete_infinite_scroll",
                            "outcome": OUTCOME_FAILED_REQUESTS,
                        })
                        continue

                    for p_url in await discover_product_links(sub_page):
                        subcategory_product_links.append((p_url, sub_name))

                    # Follow subcategory pagination pages
                    sub_next_url = await discover_next_page_url(sub_page)
                    sub_urls_seen = {sub_url}
                    while sub_next_url:
                        if sub_next_url in sub_urls_seen:
                            subcategory_failures.append({
                                "subcategory": sub_name,
                                "url": sub_next_url,
                                "error": "repeated_page_detected",
                                "outcome": OUTCOME_REPEATED_PAGE,
                            })
                            break
                        sub_urls_seen.add(sub_next_url)
                        sub_next_page = await browser.new_page()
                        try:
                            sub_next_resp = await sub_next_page.goto(sub_next_url, wait_until="domcontentloaded", timeout=timeout_ms)
                            if sub_next_resp and sub_next_resp.status >= 400:
                                subcategory_failures.append({
                                    "subcategory": sub_name,
                                    "url": sub_next_url,
                                    "error": f"http_{sub_next_resp.status}",
                                    "outcome": OUTCOME_FAILED_REQUESTS,
                                })
                                break
                            await sub_next_page.wait_for_timeout(500)
                            sub_next_scroll = await maybe_load_more(sub_next_page)
                            if not sub_next_scroll.get("complete"):
                                subcategory_failures.append({
                                    "subcategory": sub_name,
                                    "url": sub_next_url,
                                    "error": "incomplete_infinite_scroll_on_next_page",
                                    "outcome": OUTCOME_FAILED_REQUESTS,
                                })
                                break
                            for p_url in await discover_product_links(sub_next_page):
                                subcategory_product_links.append((p_url, sub_name))
                            sub_next_url = await discover_next_page_url(sub_next_page)
                        except Exception as exc:
                            subcategory_failures.append({
                                "subcategory": sub_name,
                                "url": sub_next_url,
                                "error": f"page_error:{type(exc).__name__}",
                                "outcome": OUTCOME_FAILED_REQUESTS,
                            })
                            break
                        finally:
                            await sub_next_page.close()

                except Exception as e:
                    LOG.warning("subcategory_error category=%s subcategory=%s error=%s", name, sub_name, type(e).__name__)
                    subcategory_failures.append({
                        "subcategory": sub_name,
                        "url": sub_url,
                        "error": type(e).__name__,
                        "outcome": OUTCOME_FAILED_REQUESTS,
                    })
                finally:
                    await sub_page.close()

            # Follow retailer-provided pagination links on main category
            page_url = await discover_next_page_url(page)
            page_urls_seen = {url}
            pagination_complete = True
            pagination_stop_reason = "no_next_page"
            pagination_outcome = OUTCOME_FULLY_EXHAUSTED

            while page_url:
                if page_url in page_urls_seen:
                    pagination_complete = False
                    pagination_stop_reason = "repeated_page_detected"
                    pagination_outcome = OUTCOME_REPEATED_PAGE
                    break
                page_urls_seen.add(page_url)
                next_page = await browser.new_page()
                try:
                    next_response = await next_page.goto(page_url, wait_until="domcontentloaded", timeout=timeout_ms)
                    if next_response and next_response.status >= 400:
                        pagination_complete = False
                        pagination_stop_reason = f"http_{next_response.status}"
                        pagination_outcome = OUTCOME_FAILED_REQUESTS
                        break
                    await next_page.wait_for_timeout(500)
                    next_evidence = await page_store_evidence(next_page, location)
                    if not next_evidence.get("selected_store_verified"):
                        pagination_complete = False
                        pagination_stop_reason = "unverified_store_context"
                        pagination_outcome = OUTCOME_UNVERIFIED_STORE
                        break
                    next_body_lower = (next_evidence.get("body_excerpt", "") + " " + next_evidence.get("header_text", "")).lower()
                    if any(term in next_body_lower for term in ("access denied", "verify you are human", "robot or human", "unusual traffic")):
                        pagination_complete = False
                        pagination_stop_reason = "possible_block_or_challenge"
                        pagination_outcome = OUTCOME_BLOCKED_OR_CHALLENGED
                        break
                    next_scroll = await maybe_load_more(next_page)
                    if not next_scroll.get("complete"):
                        pagination_complete = False
                        pagination_stop_reason = "incomplete_infinite_scroll_on_next_page"
                        pagination_outcome = OUTCOME_FAILED_REQUESTS
                        LOG.error(
                            "next_page_infinite_scroll_incomplete category=%s url=%s details=%r",
                            name, page_url, next_scroll
                        )
                        break
                    product_links.extend(await discover_product_links(next_page))
                    page_url = await discover_next_page_url(next_page)
                except Exception as exc:
                    pagination_complete = False
                    pagination_stop_reason = f"page_error:{type(exc).__name__}"
                    pagination_outcome = OUTCOME_FAILED_REQUESTS
                    break
                finally:
                    await next_page.close()

            # Prefer product-page records when product links exist, because those pages expose richer
            # price/deal/PPM context. Use category JSON-LD only as a fallback when no product links exist.
            category_jsonld = [] if (product_links or subcategory_product_links) else await extract_jsonld(page)
            records = []
            excluded_approx_weight = []
            product_parse_failures = []

            for p in category_jsonld:
                title = clean_text(p.get("name"))
                if not title:
                    continue
                offers = p.get("offers", {})
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                if not isinstance(offers, dict):
                    offers = {}
                weight = clean_text(p.get("weight") or p.get("size"))
                if is_approx_weight(weight, title):
                    excluded_approx_weight.append({
                        "url": clean_text(p.get("url")),
                        "title": title,
                        "reason": "approximate_weight",
                        "source": "category_jsonld",
                    })
                    continue
                rec = blank_record()
                rec.update({
                    "zipcode": clean_text(location.get("zipcode")),
                    "store_name": clean_text(location.get("store_name")),
                    "store_location": clean_text(location.get("store_location")),
                    "store_brand": is_private_label(title),
                    "category": name,
                    "sub_category": category.get("sub_category", ""),
                    "product_title": title,
                    "weight": weight,
                    "regular_price": numeric_price(offers.get("price")),
                    "image_url": clean_text(p.get("image")) if isinstance(p.get("image"), str) else "",
                    "url": clean_text(p.get("url")),
                    "upc": clean_text(p.get("gtin12") or p.get("gtin13") or p.get("gtin14") or p.get("gtin")),
                    "crawl_date": crawl_date,
                })
                records.append(rec)

            seen_product_urls: set[str] = set()
            for product_url in product_links:
                if product_url in seen_product_urls:
                    continue
                seen_product_urls.add(product_url)
                result = await parse_product_page(browser, product_url, category, location, crawl_date, timeout_ms)
                if result["status"] == "success":
                    records.append(result["record"])
                elif result["status"] == "excluded_approx_weight":
                    excluded_approx_weight.append({
                        "url": product_url,
                        "title": result["title"],
                        "reason": result["reason"],
                        "source": "product_page",
                    })
                else:
                    product_parse_failures.append({
                        "url": product_url,
                        "reason": result["reason"],
                    })
                await asyncio.sleep(delay / 2)

            for product_url, sub_category in subcategory_product_links:
                if product_url in seen_product_urls:
                    continue
                seen_product_urls.add(product_url)
                sub_meta = {**category, "sub_category": sub_category}
                result = await parse_product_page(browser, product_url, sub_meta, location, crawl_date, timeout_ms)
                if result["status"] == "success":
                    records.append(result["record"])
                elif result["status"] == "excluded_approx_weight":
                    excluded_approx_weight.append({
                        "url": product_url,
                        "title": result["title"],
                        "reason": result["reason"],
                        "source": "subcategory_product_page",
                    })
                else:
                    product_parse_failures.append({
                        "url": product_url,
                        "reason": result["reason"],
                    })
                await asyncio.sleep(delay / 2)

            await page.close()

            # Remove duplicates within this category.
            unique = {}
            for rec in records:
                key = product_key(rec)
                if key not in ("title:", "url:") and key not in unique:
                    unique[key] = rec
            rows = list(unique.values())

            # Determine machine-readable status & outcome
            if not pagination_complete:
                if pagination_outcome == OUTCOME_REPEATED_PAGE:
                    cat_status = "repeated_pagination_page"
                    cat_outcome = OUTCOME_REPEATED_PAGE
                    cat_error = "repeated_page_detected"
                elif pagination_outcome == OUTCOME_BLOCKED_OR_CHALLENGED:
                    cat_status = "blocked_or_challenged"
                    cat_outcome = OUTCOME_BLOCKED_OR_CHALLENGED
                    cat_error = "pagination_block_detected"
                elif pagination_outcome == OUTCOME_UNVERIFIED_STORE:
                    cat_status = "unverified_store_context"
                    cat_outcome = OUTCOME_UNVERIFIED_STORE
                    cat_error = "pagination_store_verification_failed"
                else:
                    cat_status = "incomplete_failed_requests"
                    cat_outcome = OUTCOME_FAILED_REQUESTS
                    cat_error = f"pagination_incomplete:{pagination_stop_reason}"
            elif product_parse_failures:
                cat_status = "product_parse_failure"
                cat_outcome = OUTCOME_PRODUCT_PARSE_FAILURE
                cat_error = f"product_parse_failures:{len(product_parse_failures)}"
            elif subcategory_failures:
                first_sub = subcategory_failures[0]
                sub_out = first_sub.get("outcome", OUTCOME_FAILED_REQUESTS)
                if sub_out == OUTCOME_FAILED_REQUESTS:
                    cat_status = "incomplete_failed_requests"
                elif sub_out == OUTCOME_BLOCKED_OR_CHALLENGED:
                    cat_status = "blocked_or_challenged"
                elif sub_out == OUTCOME_REPEATED_PAGE:
                    cat_status = "repeated_pagination_page"
                elif sub_out == OUTCOME_UNVERIFIED_STORE:
                    cat_status = "unverified_store_context"
                else:
                    cat_status = "incomplete"
                cat_outcome = sub_out
                cat_error = f"subcategory_failure:{first_sub.get('error')}"
            elif not rows:
                cat_status = "incomplete"
                cat_outcome = "incomplete_no_products_found"
                cat_error = "no_records_collected"
            else:
                cat_status = "ok"
                cat_outcome = OUTCOME_FULLY_EXHAUSTED
                cat_error = ""

            stats = {
                "category": name, "url": url, "attempt": attempt,
                "status": cat_status,
                "outcome": cat_outcome,
                "records": len(rows),
                "discovered_product_links": len(seen_product_urls),
                "discovered_subcategories": len(subcategories),
                "pagination_pages_visited": len(page_urls_seen),
                "pagination_complete": pagination_complete,
                "pagination_stop_reason": pagination_stop_reason,
                "product_parse_failures": product_parse_failures,
                "excluded_approx_weight": excluded_approx_weight,
                "excluded_approx_weight_count": len(excluded_approx_weight),
                "subcategory_failures": subcategory_failures,
                "scroll_result": scroll_result,
                "store_evidence": evidence,
                "error": cat_error,
            }
            LOG.info("category_finish location=%s category=%s records=%d links=%d status=%s outcome=%s",
                     location.get("id"), name, len(rows), len(seen_product_urls), stats["status"], stats["outcome"])
            return rows, stats

        except PlaywrightTimeoutError:
            last_error = "navigation_timeout"
            last_outcome = OUTCOME_FAILED_REQUESTS
            last_status = "incomplete_failed_requests"
            LOG.warning("category_timeout category=%s attempt=%d", name, attempt)
        except Exception as e:
            last_error = type(e).__name__
            last_outcome = OUTCOME_FAILED_REQUESTS
            last_status = "incomplete_failed_requests"
            LOG.exception("category_error category=%s attempt=%d", name, attempt)
        finally:
            if not page.is_closed():
                await page.close()
        await asyncio.sleep(delay * attempt)

    return [], {
        "category": name,
        "url": url,
        "status": last_status if last_status != "ok" else "failed",
        "outcome": last_outcome,
        "records": 0,
        "discovered_product_links": 0,
        "discovered_subcategories": 0,
        "pagination_pages_visited": 0,
        "pagination_complete": False,
        "pagination_stop_reason": "category_attempts_exhausted",
        "product_parse_failures": [],
        "excluded_approx_weight": [],
        "excluded_approx_weight_count": 0,
        "subcategory_failures": [],
        "scroll_result": {},
        "store_evidence": {},
        "error": last_error,
    }



async def run(args) -> int:
    setup_logging()
    cfg = load_config(Path(args.config))
    categories = load_categories(Path(args.categories))
    if not categories:
        raise RuntimeError("No categories found in CSV.")
    selected_locations = cfg["locations"]
    if args.location:
        selected_locations = [x for x in selected_locations if x.get("id") == args.location]
        if not selected_locations:
            raise ValueError(f"Configured location id not found: {args.location}")
    crawl_date = date.today().isoformat()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    final_path = DATA_DIR / "marianos_latest.json"
    previous_records = load_previous_catalog(final_path)
    run_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    staging_path = DATA_DIR / f"staging_{run_stamp}.json"
    report_path = DATA_DIR / f"report_{run_stamp}.json"

    # Keep stores not selected for this run untouched in the eventual publication.
    all_records: list[dict[str, str]] = [
        record for record in previous_records
        if not any(location_record_matches(record, location)
                   for location in selected_locations)
    ]
    all_stats: list[dict[str, Any]] = []
    location_proofs = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not args.headed)
        context = await browser.new_context(user_agent=cfg.get("user_agent") or None)
        try:
            if args.manual_store_selection:
                if not args.headed:
                    raise RuntimeError("--manual-store-selection requires --headed.")
                if len(selected_locations) != 1:
                    raise RuntimeError("Manual store selection requires exactly one selected location (--location ID).")
                location = selected_locations[0]
                if not location.get("store_name") or not location.get("store_location"):
                    raise RuntimeError("Configure both store_name and store_location before manual store selection.")
                preflight = await context.new_page()
                await preflight.goto(categories[0]["url"], wait_until="domcontentloaded",
                                     timeout=int(cfg.get("request_timeout_ms", 45000)))
                print("\nSelect the configured Mariano's store in the open browser window.")
                print("Then return here and press Enter. The scraper will verify the visible store name + address.")
                await asyncio.to_thread(input, "Press Enter after selecting the store... ")
                proof = await page_store_evidence(preflight, location)
                await preflight.close()
                if not proof.get("selected_store_verified"):
                    raise RuntimeError(
                        "Store selection could not be verified from visible header text. "
                        f"Expected {location.get('store_name')} / {location.get('store_location')}; "
                        f"header was: {proof.get('header_text', '')!r}"
                    )
                LOG.info("manual_store_selection_verified location=%s header=%r",
                         location.get("id"), proof.get("header_text", ""))
            for location in selected_locations:
                if not location.get("zipcode") and not location.get("store_id") and not location.get("store_location"):
                    raise RuntimeError(
                        f"Location '{location.get('id')}' has no zipcode/store_id/store_location. "
                        "Configure a real location before scraping; refusing an unverified run."
                    )
                # Stable per-location checkpoint makes a later process restart genuinely resumable.
                loc_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(location.get("id") or "location"))
                checkpoint_path = CHECKPOINT_DIR / f"checkpoint_{loc_key}.json"
                current_fingerprint = checkpoint_fingerprint(location, categories)
                historical_location_records = [
                    record for record in previous_records
                    if location_record_matches(record, location)
                ]
                location_records: list[dict[str, str]] = list(historical_location_records)
                location_stats: list[dict[str, Any]] = []
                completed_names: set[str] = set()
                
                if args.resume and checkpoint_path.exists():
                    try:
                        saved = json.loads(
                            checkpoint_path.read_text(encoding="utf-8")
                        )

                        if is_checkpoint_compatible(
                            saved, location, current_fingerprint
                        ):
                            saved_records = saved.get("records_so_far")
                            saved_stats = saved.get("completed_categories")
                            if not isinstance(saved_records, list) or not isinstance(saved_stats, list):
                                raise ValueError("checkpoint_records_or_stats_invalid")

                            location_stats = saved_stats
                            completed_names = set()

                            # Merge only categories explicitly marked successful.
                            # Partial records from failed categories are never trusted.
                            for saved_stat in location_stats:
                                saved_category = saved_stat.get("category")
                                if saved_stat.get("status") != "ok" or not isinstance(saved_category, str):
                                    continue
                                saved_category_rows = [
                                    record for record in saved_records
                                    if isinstance(record, dict)
                                    and record.get("category") == saved_category
                                ]
                                if not saved_category_rows:
                                    continue
                                location_records = replace_category_records(
                                    location_records, saved_category, saved_category_rows
                                )
                                completed_names.add(saved_category)

                            LOG.info(
                                "resume_loaded location=%s completed_categories=%d records=%d",
                                location.get("id"),
                                len(completed_names),
                                len(location_records),
                            )

                            # A fully completed checkpoint is not a resumable interrupted run.
                            expected_names = {item["category"] for item in categories}
                            if expected_names and expected_names.issubset(completed_names):
                                LOG.error(
                                    "resume_rejected_completed_checkpoint location=%s: checkpoint is already completed; refusing to re-run with --resume",
                                    location.get("id"),
                                )
                                return 2
                        else:
                            LOG.warning(
                                "checkpoint_invalid_or_mismatched location=%s; starting fresh",
                                location.get("id"),
                            )

                    except Exception as e:
                        # Discard every partial checkpoint merge. Never retain
                        # records from a checkpoint that failed validation.
                        location_records = list(historical_location_records)
                        location_stats = []
                        completed_names = set()
                        LOG.warning(
                            "checkpoint_unreadable location=%s error=%s; starting fresh",
                            location.get("id"),
                            type(e).__name__,
                        )

                try:
                    for category in categories:
                        if category["category"] in completed_names:
                            LOG.info("resume_skip location=%s category=%s", location.get("id"), category["category"])
                            continue
                        rows, stats = await crawl_category(context, category, location, cfg, crawl_date)
                        stats["location_id"] = location.get("id", "")
                        location_stats = [
                            s for s in location_stats
                            if s.get("category") != category["category"]
                        ]
                        location_stats.append(stats)

                        # Only a complete category crawl may update the catalog.
                        # Incomplete results remain in diagnostics/checkpoints, not publication.
                        location_records = apply_category_result(
                            location_records, category["category"], rows, stats.get("status")
                        )
                        # Keep the checkpoint for failed categories too, but only successful categories are skipped.
                        checkpoint = {
                            "run_id": run_stamp,
                            "location_id": location.get("id"),
                            "config_fingerprint": current_fingerprint,
                            "completed_categories": location_stats,
                            "records_so_far": location_records,
                        }
                        temp_checkpoint = checkpoint_path.with_suffix(".tmp")
                        temp_checkpoint.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")
                        temp_checkpoint.replace(checkpoint_path)
                        location_proofs.append({
                            "location_id": location.get("id"),
                            "configured_zipcode": location.get("zipcode", ""),
                            "configured_store_id": location.get("store_id", ""),
                            "category": category["category"],
                            "evidence": stats.get("store_evidence", {})
                        })
                except (KeyboardInterrupt, asyncio.CancelledError):
                    LOG.warning("run_interrupted location=%s", location.get("id"))
                    current_cat_name = category.get("category", "") if "category" in locals() else ""
                    interrupted_stats = {
                        "category": current_cat_name,
                        "url": category.get("url", "") if "category" in locals() else "",
                        "attempt": 1,
                        "status": "interrupted",
                        "outcome": OUTCOME_INTERRUPTED,
                        "records": 0,
                        "discovered_product_links": 0,
                        "discovered_subcategories": 0,
                        "pagination_pages_visited": 0,
                        "pagination_complete": False,
                        "pagination_stop_reason": "interrupted_by_operator",
                        "product_parse_failures": [],
                        "excluded_approx_weight": [],
                        "excluded_approx_weight_count": 0,
                        "subcategory_failures": [],
                        "scroll_result": {},
                        "store_evidence": {},
                        "error": "interrupted_by_operator",
                        "location_id": location.get("id", ""),
                    }
                    if current_cat_name:
                        location_stats = [s for s in location_stats if s.get("category") != current_cat_name]
                        location_stats.append(interrupted_stats)
                    checkpoint = {
                        "run_id": run_stamp,
                        "location_id": location.get("id"),
                        "config_fingerprint": current_fingerprint,
                        "completed_categories": location_stats,
                        "records_so_far": location_records,
                    }
                    temp_checkpoint = checkpoint_path.with_suffix(".tmp")
                    temp_checkpoint.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")
                    temp_checkpoint.replace(checkpoint_path)
                    raise
                all_stats.extend(location_stats)
                all_records.extend(location_records)
        finally:
            await context.close()
            await browser.close()


    # Cross-category dedupe; preserve identical products across different stores.
    unique = {}

    for rec in all_records:
        base_key = product_key(rec)

        if not rec.get("upc", "").strip() and not rec.get("url", "").strip():
            continue

        # product_key already includes zipcode, store name, and store address.
        unique.setdefault(base_key, rec)

    records = list(unique.values())

    staging_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    checks = validate_records(records, all_stats, cfg, final_path if final_path.exists() else None)
    report = {
        "run_id": run_stamp, "retailer": "Mariano's", "crawl_date": crawl_date,
        "location_proofs": location_proofs, "category_results": all_stats,
        "validation": checks, "staging_file": str(staging_path),
        "final_status": "validated" if checks["validation_passed"] else "rejected_preserving_previous_success"
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    LOG.info("run_summary total=%d unique=%d duplicates=%d validation=%s",
             checks["total_records"], checks["unique_products"], checks["duplicate_count"],
             checks["validation_passed"])
    if checks["validation_passed"]:
        publish_staging_file(staging_path, final_path)
        azure_result = publish_to_azure_blob(staging_path, report_path, run_stamp, cfg)
        report["azure_publication"] = azure_result
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        LOG.info("published path=%s", final_path)
        return 0
    LOG.error("run_rejected; previous successful output preserved; report=%s", report_path)
    return 2


def main() -> None:
    parser = argparse.ArgumentParser(description="Mariano's catalog scraper scaffold")
    parser.add_argument("--config", default=str(CONFIG_DIR / "settings.json"))
    parser.add_argument("--categories", default=str(CONFIG_DIR / "Marianos_Category_URLs.csv"))
    parser.add_argument("--location", help="Configured location id")
    parser.add_argument("--headed", action="store_true", help="Run browser headed for operator inspection")
    parser.add_argument("--resume", action="store_true", help="Resume successful categories from per-location checkpoints")
    parser.add_argument("--manual-store-selection", action="store_true",
                        help="Open a headed browser so an operator can select the configured store before scraping")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
