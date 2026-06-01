"""Per-user dashboard authorization: which companies an email may view.

Cloudflare Access proves WHO the user is (authentication). This CSV decides WHAT
they see (authorization). data/dashboard_access.csv:

    email,companies,role
    you@aiivar.com,*,admin
    citadel@ravenswood.com,6029221925,viewer
    ops@techmanager.com,8775541997;6029221925,viewer

- email      — the Cloudflare-verified login (case-insensitive).
- companies  — "*" for all, else a ;-separated list of company keys (the dialed
               number's last 10 digits, matching companies.csv / routing.csv).
- role       — "admin" (sees everything incl. the access list) or "viewer".

Not listed => no access. mtime-cached like the other data files.
"""

import csv
import logging
from pathlib import Path

from app.services.companies import normalize
from app.services.datafiles import load_cached

log = logging.getLogger("ivr")

ACCESS_FILE = "dashboard_access.csv"


def _parse(path: Path) -> dict[str, dict]:
    index: dict[str, dict] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            email = (row.get("email") or "").strip().lower()
            if not email:
                continue
            raw = (row.get("companies") or "").strip()
            if raw == "*":
                companies = "*"
            else:
                companies = {normalize(c) for c in raw.split(";") if c.strip()}
            index[email] = {
                "companies": companies,
                "role": (row.get("role") or "viewer").strip().lower(),
            }
    log.info("Loaded %d dashboard users from %s", len(index), path)
    return index


def get_access(email: str) -> dict | None:
    """The access record for a verified email, or None if not authorized."""
    if not email:
        return None
    index = load_cached(ACCESS_FILE, _parse) or {}
    return index.get(email.strip().lower())


def can_see(access: dict, company_key: str) -> bool:
    """True if this user may view the given company (normalized number key)."""
    allowed = access.get("companies")
    if allowed == "*":
        return True
    return normalize(company_key) in (allowed or set())
