"""Configurable keypress options for the busy / voicemail prompt (data/options.csv).

One row per option: `company,context,department,digit,label,destination,active`. At
the busy prompt the caller can "press <digit> for <label>" to route to
<destination> instead of leaving a message; no keypress falls through. Add more
rows to offer more keys.

- `context` — `busy` for now (the unavailable / no-answer prompt).
- `department` — limit this option to one department (sales/support/billing/
  operator); blank = applies to every department's busy prompt.
- `digit`   — the DTMF key to press (avoid `#`, which ends a recording).
- `label`   — spoken after "for …, press <digit>".
- `destination` — `ai` (the tenant's configured assistant), an `assistant-…` id,
  a department key (rings that chain), a PSTN number, a SIP URI, or `voicemail`.
- `company` blank = default/all tenants, else the dialed number (last-10 match).
- `active`  — `false` benches a row without deleting it.

Resolution is first-hit-wins, most specific first: (company, this dept) ->
(company, any dept) -> (default, this dept) -> (default, any dept). Absent file /
no match -> the app falls back to its default (press 1 = AI when an assistant is
configured). data/options.csv is gitignored; ship options.example.csv.
"""

import csv
import logging
from pathlib import Path

from app.services.companies import normalize
from app.services.datafiles import load_cached

log = logging.getLogger("ivr")

OPTIONS_FILE = "options.csv"


def _truthy(value: str) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "y", "on")


def _parse(path: Path) -> dict[str, dict[str, list[dict]]]:
    """company_key -> context -> [ {digit, label, destination, department} ], by digit."""
    index: dict[str, dict[str, list[dict]]] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            digit = (row.get("digit") or "").strip()
            dest = (row.get("destination") or "").strip()
            ctx = (row.get("context") or "").strip().lower()
            if not (digit and dest and ctx) or not _truthy(row.get("active", "true")):
                continue  # skip blank/inactive rows rather than break a call
            co = normalize(row.get("company", ""))
            index.setdefault(co, {}).setdefault(ctx, []).append({
                "digit": digit,
                "label": (row.get("label") or "").strip(),
                "destination": dest,
                "department": (row.get("department") or "").strip().lower(),
            })
    for contexts in index.values():
        for opts in contexts.values():
            opts.sort(key=lambda o: o["digit"])
    total = sum(len(o) for c in index.values() for o in c.values())
    log.info("Loaded %d keypress options from %s", total, path)
    return index


def get_options(co: str, context: str, department: str = "") -> list[dict]:
    """Active options for (company, context, department), first-hit-wins most
    specific first. Empty list when none are configured."""
    index = load_cached(OPTIONS_FILE, _parse)
    if not index:
        return []
    dept = (department or "").strip().lower()

    def pick(company_key: str) -> list[dict]:
        rows = index.get(company_key, {}).get(context) or []
        return [o for o in rows if o["department"] == dept] or [o for o in rows if not o["department"]]

    return pick(co) or pick("")
