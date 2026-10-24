
import os
import requests
from requests.auth import HTTPBasicAuth

BASE = "https://api.kroger.com/v1"

client_id = os.getenv("KROGER_CLIENT_ID")
client_secret = os.getenv("KROGER_CLIENT_SECRET")

if not client_id or not client_secret:
    raise SystemExit("Missing Kroger credentials in this PowerShell session.")

# Get an access token
auth_response = requests.post(
    f"{BASE}/connect/oauth2/token",
    auth=HTTPBasicAuth(client_id, client_secret),
    headers={"Content-Type": "application/x-www-form-urlencoded"},
    data={
        "grant_type": "client_credentials",
        "scope": "product.compact"
    },
    timeout=20,
)
print("Token status:", auth_response.status_code)
auth_response.raise_for_status()
token = auth_response.json()["access_token"]

headers = {
    "Authorization": f"Bearer {token}",
    "Accept": "application/json",
}

# Search for Mariano's locations near ZIP 60601
location_response = requests.get(
    f"{BASE}/locations",
    headers=headers,
    params={
        "filter.zipCode.near": "60601",
        "filter.limit": 50,
    },
    timeout=30,
)
print("Location status:", location_response.status_code)

if not location_response.ok:
    print(location_response.text[:1000])
    raise SystemExit("Location search failed.")

locations = location_response.json().get("data", [])

matches = []
for store in locations:
    name = store.get("name", "")
    address = store.get("address", {})
    address_text = " ".join([
        str(address.get("addressLine1", "")),
        str(address.get("city", "")),
        str(address.get("state", "")),
        str(address.get("zipCode", "")),
    ])

    print(
        "STORE:",
        repr(name),
        "|",
        address_text,
        "| ID:",
        store.get("locationId", "")
    )

    if "mariano" in (name + " " + address_text).lower():
        matches.append(store)

if not matches:
    raise SystemExit(
        "No Mariano's match found in returned locations. "
        "Do not assume a different store is equivalent."
    )

store = matches[0]
location_id = store.get("locationId")

print("\nSelected store:", store.get("name"))
print("Location ID:", location_id)

# Search for milk at this exact location
product_response = requests.get(
    f"{BASE}/products",
    headers=headers,
    params={
        "filter.term": "milk",
        "filter.locationId": location_id,
        "filter.limit": 10,
    },
    timeout=30,
)

print("\nProduct status:", product_response.status_code)

if not product_response.ok:
    print(product_response.text[:1000])
    raise SystemExit("Product search failed.")

result = product_response.json()
products = result.get("data", [])

print("Products returned:", len(products))
print("API total, if provided:", result.get("meta", {}).get("pagination", {}).get("total"))

for product in products[:5]:
    print({
        "productId": product.get("productId"),
        "description": product.get("description"),
        "brand": product.get("brand"),
        "upc": product.get("upc"),
        "items": product.get("items", []),
    })
