"""Match inbound callers to a contact.

Primary source is a local CSV (data/contacts.csv) — instant, no network on the
call path. Refresh it from HubSpot with scripts/sync_hubspot.py, or drop in a CSV
exported from any other system. If a number isn't found locally and
HUBSPOT_LIVE_FALLBACK is on, we fall back to the HubSpot API.

CSV format (header required):
    phone,name,company,tier
    +14155551234,Jane Doe,Acme Inc,vip
"""

import csv
import logging
from pathlib import Path

from app.config import settings
from app.services.datafiles import load_cached

log = logging.getLogger("ivr")

CONTACTS_FILE = "contacts.csv"


def _normalize(phone: str) -> str:
    """Reduce a phone string to comparable digits (last 10, to ignore +1/format)."""
    digits = "".join(ch for ch in phone if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def _parse(path: Path) -> dict[str, dict]:
    """Parse the contacts CSV into {normalized_phone: contact dict}."""
    index: dict[str, dict] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            key = _normalize(row.get("phone", ""))
            if not key:
                continue
            index[key] = {
                "name": (row.get("name") or "").strip() or None,
                "company": (row.get("company") or "").strip() or None,
                "tier": (row.get("tier") or "").strip() or None,
            }
    log.info("Loaded %d contacts from %s", len(index), path)
    return index


async def lookup_caller(phone: str) -> dict | None:
    """Return the matched contact for `phone`, or None. Local CSV first."""
    if not phone:
        return None

    index = load_cached(CONTACTS_FILE, _parse) or {}
    contact = index.get(_normalize(phone))
    if contact:
        return contact

    if settings.hubspot_live_fallback and settings.hubspot_token:
        # Lazy import so HubSpot is only touched when explicitly enabled.
        from app.services.hubspot import lookup_caller as hubspot_lookup

        return await hubspot_lookup(phone)

    return None
