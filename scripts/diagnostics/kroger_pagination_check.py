
import os
import time
from dotenv import load_dotenv
import requests
from requests.auth import HTTPBasicAuth

load_dotenv()

BASE = "https://api.kroger.com/v1"
STORE_ID = os.getenv("KROGER_STORE_ID", "53100502")

client_id = os.getenv("KROGER_CLIENT_ID")
client_secret = os.getenv("KROGER_CLIENT_SECRET")
if not client_id or not client_secret:
    raise SystemExit("Missing Kroger credentials in this session.")

auth = requests.post(
    f"{BASE}/connect/oauth2/token",
    auth=HTTPBasicAuth(client_id, client_secret),
    headers={"Content-Type": "application/x-www-form-urlencoded"},
    data={
        "grant_type": "client_credentials",
        "scope": "product.compact",
    },
    timeout=20,
)
auth.raise_for_status()
headers = {
    "Authorization": f"Bearer {auth.json()['access_token']}",
    "Accept": "application/json",
}

session = requests.Session()
session.headers.update(headers)

seen = {}
start = 0
limit = 50
reported_total = None

while True:
    response = session.get(
        f"{BASE}/products",
        params={
            "filter.term": "milk",
            "filter.locationId": STORE_ID,
            "filter.limit": limit,
            "filter.start": start,
        },
        timeout=30,
    )

    if not response.ok:
        print("Request failed:", response.status_code)
        print(response.text[:500])
        break

    payload = response.json()
    products = payload.get("data", [])
    reported_total = (
        payload.get("meta", {})
        .get("pagination", {})
        .get("total", reported_total)
    )

    if not products:
        break

    for product in products:
        product_id = product.get("productId") or product.get("upc")
        if product_id:
            seen[product_id] = product

    print(
        f"Page start={start}: received={len(products)}, "
        f"unique so far={len(seen)}, API total={reported_total}"
    )

    start += len(products)

    if len(products) < limit:
        break

    time.sleep(0.25)

print("\n--- SUMMARY ---")
print("API-reported total:", reported_total)
print("Unique products collected:", len(seen))

if seen:
    sample = next(iter(seen.values()))
    print("Product keys:", list(sample.keys()))
    print("Sample product:", sample)

    items = sample.get("items", [])
    if items:
        print("Item keys:", list(items[0].keys()))
        print("Price fields:", list(items[0].get("price", {}).keys()))
