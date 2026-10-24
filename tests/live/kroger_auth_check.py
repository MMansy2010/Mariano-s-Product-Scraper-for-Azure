
import os
import requests
from requests.auth import HTTPBasicAuth

client_id = os.getenv("KROGER_CLIENT_ID")
client_secret = os.getenv("KROGER_CLIENT_SECRET")

if not client_id or not client_secret:
    raise SystemExit(
        "Missing credentials. Set KROGER_CLIENT_ID and "
        "KROGER_CLIENT_SECRET in this PowerShell session."
    )

url = "https://api.kroger.com/v1/connect/oauth2/token"

try:
    response = requests.post(
        url,
        auth=HTTPBasicAuth(client_id, client_secret),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "client_credentials",
            "scope": "product.compact"
        },
        timeout=20,
    )

    print("HTTP status:", response.status_code)

    if response.ok:
        data = response.json()
        print("Authentication: SUCCESS")
        print("Token received:", bool(data.get("access_token")))
        print("Expires in:", data.get("expires_in"), "seconds")
        print("Scope:", data.get("scope", "not returned"))
    else:
        print("Authentication: FAILED")
        print("Response:", response.text[:1000])

except requests.RequestException as exc:
    print("Request error:", type(exc).__name__, str(exc))
