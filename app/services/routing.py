"""Per-agent routing roster: data/routing.csv, one row per destination.

This is the TOP routing layer. For a dialed number's company and the chosen
department it yields ordered ring *stages*: rows sharing a `priority` ring
together, and higher priorities are later fail-over stages. A destination is a
SIP URI (sip:user@sip.telnyx.com), a PSTN number (+1...), or a Telnyx AI
Assistant id (assistant-...). When routing.csv is absent or has no active rows
for a department, callers fall back to data/companies.csv and then the env
<KEY>_agents/_fallback config — so existing setups keep working unchanged.

CSV header:
    company,department,name,destination,extension,priority,active
    ,sales,Alice,sip:alice@sip.telnyx.com,101,1,true
    ,support,AI Bot,assistant-776d0d6f,,1,true
    18005550001,sales,Acme Alice,sip:acme-alice@sip.telnyx.com,201,1,true

`company` blank = default / single-tenant; otherwise the dialed number (matched
on the last 10 digits). `extension` is a label for now (reserved for a future
dial-by-extension feature). `active` false benches a row without deleting it.
"""

import csv
import logging
from pathlib import Path

from app.services.companies import normalize
from app.services.datafiles import load_cached

log = logging.getLogger("ivr")

ROUTING_FILE = "routing.csv"


def _truthy(value: str) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "y", "on")


def _parse(path: Path) -> dict[str, dict[str, list[dict]]]:
    """company_key -> department -> [row dicts] (kept in file order per dept)."""
    index: dict[str, dict[str, list[dict]]] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            dest = (row.get("destination") or "").strip()
            dept = (row.get("department") or "").strip().lower()
            if not dest or not dept:
                continue  # skip blank / comment-ish rows instead of breaking calls
            co = normalize(row.get("company", ""))  # "" stays "" (the default set)
            try:
                priority = int((row.get("priority") or "1").strip() or "1")
            except ValueError:
                priority = 1
            index.setdefault(co, {}).setdefault(dept, []).append({
                "destination": dest,
                "name": (row.get("name") or "").strip(),
                "extension": (row.get("extension") or "").strip(),
                "priority": priority,
                "active": _truthy(row.get("active", "true")),
            })
    total = sum(len(rows) for depts in index.values() for rows in depts.values())
    log.info("Loaded %d routing rows from %s", total, path)
    return index


def get_stages(company_key: str, dept_key: str) -> list[list[str]] | None:
    """Ordered ring stages for (company, department) from routing.csv.

    Prefers rows matching the dialed company; if none, uses the blank/default
    rows. Returns None when the file is absent or has no active rows for this
    department, so the caller falls back to companies.csv / env config.
    """
    index = load_cached(ROUTING_FILE, _parse)
    if not index:
        return None
    rows = index.get(company_key, {}).get(dept_key) or index.get("", {}).get(dept_key)
    if not rows:
        return None
    active = [r for r in rows if r["active"]]
    if not active:
        return None
    return [
        [r["destination"] for r in active if r["priority"] == priority]
        for priority in sorted({r["priority"] for r in active})
    ]
