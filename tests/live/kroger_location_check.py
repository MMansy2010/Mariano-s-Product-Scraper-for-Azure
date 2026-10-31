import os
import re
import requests
from requests.auth import HTTPBasicAuth

BASE = "https://api.kroger.com/v1"

client_id = os.getenv("KROGER_CLIENT_ID")
client_secret = os.getenv("KROGER_CLIENT_SECRET")

if not client_id or not client_secret:
    raise SystemExit("Missing Kroger API credentials.")

# Authenticate
auth = requests.post(
    f"{BASE}/connect/oauth2/token",
    auth=HTTPBasicAuth(client_id, client_secret),
    headers={"Content-Type": "application/x-www-form-urlencoded"},
    data={"grant_type": "client_credentials", "scope": "product.compact"},
    timeout=20,
)
print("Authentication:", auth.status_code)
auth.raise_for_status()

token = auth.json()["access_token"]
headers = {
    "Authorization": f"Bearer {token}",
    "Accept": "application/json",
}

# Search around the requested ZIP code
response = requests.get(
    f"{BASE}/locations",
    headers=headers,
    params={
        "filter.zipCode.near": "60061",
        "filter.limit": 50,
    },
    timeout=30,
)

print("Location search:", response.status_code)
response.raise_for_status()

locations = response.json().get("data", [])

def normalize(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())

exact_matches = []

for store in locations:
    address = store.get("address", {})
    line1 = address.get("addressLine1", "")
    city = address.get("city", "")
    state = address.get("state", "")
    zipcode = address.get("zipCode", "")
    name = store.get("name", "")
    location_id = store.get("locationId", "")

    address_text = f"{line1}, {city}, {state} {zipcode}"

    print(
        f"STORE: {name} | {address_text} | ID: {location_id}"
    )

    exact_address = (
        normalize(line1) == normalize("1720 N Milwaukee Ave")
        and normalize(city) == normalize("Vernon Hills")
        and normalize(state) == normalize("IL")
        and str(zipcode).startswith("60061")
    )

    if exact_address:
        exact_matches.append(store)

print("\nReturned locations:", len(locations))
print("Exact address matches:", len(exact_matches))

for store in exact_matches:
    print("\nEXACT MATCH:")
    print("Name:", store.get("name"))
    print("Location ID:", store.get("locationId"))
    print("Address:", store.get("address"))
