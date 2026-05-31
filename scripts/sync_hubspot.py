"""Download all HubSpot contacts to data/contacts.csv for instant caller matching.

Run manually or on a cron (e.g. hourly):
    python scripts/sync_hubspot.py

Needs HUBSPOT_TOKEN in the environment / .env (Private App token, CRM read scope).
The IVR reads the resulting CSV with no API calls on the call path. "Other sources"
just means: produce a CSV with the same header and drop it in data/contacts.csv.
"""

import csv
import sys
from pathlib import Path

import httpx

# Allow running as a standalone script from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402

SEARCH_URL = "https://api.hubapi.com/crm/v3/objects/contacts"
PROPERTIES = ["firstname", "lastname", "phone", "mobilephone", "company", "hs_lead_status"]
OUT = Path(settings.data_dir) / "contacts.csv"


def fetch_all() -> list[dict]:
    """Page through every contact in the portal."""
    rows: list[dict] = []
    after: str | None = None
    headers = {"Authorization": f"Bearer {settings.hubspot_token}"}

    with httpx.Client(timeout=30.0) as client:
        while True:
            params = {"limit": 100, "properties": ",".join(PROPERTIES)}
            if after:
                params["after"] = after
            resp = client.get(SEARCH_URL, headers=headers, params=params)
            resp.raise_for_status()
            body = resp.json()

            for result in body.get("results", []):
                props = result.get("properties", {})
                phone = (props.get("phone") or props.get("mobilephone") or "").strip()
                if not phone:
                    continue
                name = " ".join(filter(None, [props.get("firstname"), props.get("lastname")]))
                rows.append({
                    "phone": phone,
                    "name": name.strip(),
                    "company": (props.get("company") or "").strip(),
                    "tier": (props.get("hs_lead_status") or "").strip(),
                })

            after = body.get("paging", {}).get("next", {}).get("after")
            if not after:
                break
    return rows


def main() -> None:
    if not settings.hubspot_token:
        sys.exit("HUBSPOT_TOKEN is not set. Add it to .env or the environment.")

    rows = fetch_all()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["phone", "name", "company", "tier"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} contacts to {OUT}")


if __name__ == "__main__":
    main()
