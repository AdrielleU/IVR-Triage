"""Multi-tenant lookup: map the dialed number (`To`) to a company's config.

One deployment can serve many companies — each is just a different Telnyx number.
data/companies.csv (optional) is keyed by number; if it's absent or the dialed
number isn't listed, callers fall back to the single-tenant env config, so existing
single-company setups keep working unchanged.

CSV header (multiple agents in a cell separated by ; to avoid CSV comma clashes):
    number,name,menu_audio_url,sales_agents,support_agents,billing_agents,operator_agents,sales_fallback,support_fallback,billing_fallback,operator_fallback
    +18005550001,Acme Inc,,sip:a1@sip.telnyx.com;sip:a2@sip.telnyx.com,sip:a1@sip.telnyx.com,+14155550101,+14155550100,+14155550111,,,
"""

import csv
import logging
from pathlib import Path

from app.services.datafiles import load_cached

log = logging.getLogger("ivr")

COMPANIES_FILE = "companies.csv"


def normalize(number: str) -> str:
    """Comparable form of a phone number (last 10 digits, ignoring +1/format)."""
    digits = "".join(ch for ch in (number or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def _parse(path: Path) -> dict[str, dict]:
    index: dict[str, dict] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            key = normalize(row.get("number", ""))
            if key:
                index[key] = {k: (v or "").strip() for k, v in row.items()}
    log.info("Loaded %d companies from %s", len(index), path)
    return index


def get_company(number: str) -> dict | None:
    """Return the company config for a dialed number, or None (-> env fallback)."""
    index = load_cached(COMPANIES_FILE, _parse)
    if not index:
        return None
    return index.get(normalize(number))
