"""Login-gated dashboard: voicemails, recordings, leads, and call log.

Authentication is Cloudflare Access (verified JWT -> email). Authorization is
data/dashboard_access.csv (email -> which companies). Every view filters its rows
to the companies the logged-in user is allowed to see.

Server-rendered HTML (Jinja), no JS framework. Audio is streamed from the local
recordings/ dir through an auth-checked route — recordings/ is NEVER statically
served (it holds PII); only an authorized user pulls a specific file.
"""

import csv
import json
import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import settings
from app.services.cf_access import get_verified_email
from app.services.companies import get_company, normalize
from app.services.dashboard_access import can_see, get_access
from app.services.datafiles import load_cached

log = logging.getLogger("ivr")
router = APIRouter()

_TPL_DIR = Path(__file__).resolve().parents[2] / "texml" / "dashboard"
_env = Environment(
    loader=FileSystemLoader(str(_TPL_DIR)),
    autoescape=select_autoescape(default=True),
    auto_reload=True,
)


# ── company identity helpers ─────────────────────────────────────────────────
def _company_aliases() -> dict[str, str]:
    """Map every identifier a stored row might use (company NAME, LABEL, or number)
    to its normalized key, so we can filter rows that only carry a name/label."""
    index = load_cached("companies.csv", _parse_companies) or {}
    alias: dict[str, str] = {}
    for key, row in index.items():
        alias[key] = key
        for field in ("name", "label"):
            v = (row.get(field) or "").strip().lower()
            if v:
                alias[v] = key
    return alias


def _parse_companies(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            key = normalize(row.get("number", ""))
            if key:
                out[key] = {k: (v or "").strip() for k, v in row.items()}
    return out


def _row_key(*candidates: str) -> str:
    """Resolve the company key for a stored row from any identifier it carries."""
    alias = _company_aliases()
    for c in candidates:
        c = (c or "").strip()
        if not c:
            continue
        k = normalize(c)
        if k and k in alias:
            return k
        if c.lower() in alias:
            return alias[c.lower()]
    return ""


def _require_user(request: Request):
    """(email, access) for an authorized user, or (None, None) -> caller 401s."""
    email = get_verified_email(request)
    if not email:
        return None, None
    access = get_access(email)
    if not access:
        return email, None
    return email, access


def _label_for(key: str) -> str:
    row = get_company(key)
    return (row.get("name") if row else "") or key


# ── data loaders (each returns rows the user may see) ────────────────────────
def _recordings(access: dict) -> list[dict]:
    index = Path(settings.recordings_dir) / "index.csv"
    rows: list[dict] = []
    if not index.is_file():
        return rows
    with index.open(newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            key = _row_key(r.get("company", ""))
            if key and not can_see(access, key):
                continue
            rows.append({
                "ts": r.get("timestamp", ""), "company": r.get("company", ""),
                "ckey": key,
                "dept": r.get("dept", ""), "caller": r.get("caller", ""),
                "duration": r.get("duration_seconds", ""),
                "audio": r.get("audio_file", ""), "transcript": r.get("transcript", ""),
            })
    rows.sort(key=lambda x: x["ts"], reverse=True)
    return rows


def _leads(access: dict) -> list[dict]:
    path = Path(settings.leads_csv_path)
    rows: list[dict] = []
    if not path.is_file():
        return rows
    with path.open(newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            key = _row_key(r.get("company", ""), r.get("caller_number", ""))
            if key and not can_see(access, key):
                continue
            r["ckey"] = key
            rows.append(r)
    rows.sort(key=lambda x: x.get("ts", ""), reverse=True)
    return rows


def _call_log(access: dict) -> list[dict]:
    path = Path(settings.call_log_path)
    rows: list[dict] = []
    if not path.is_file():
        return rows
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            key = _row_key(r.get("to", ""), r.get("company", ""), r.get("label", ""))
            if key and not can_see(access, key):
                continue
            r["ckey"] = key
            rows.append(r)
    rows.reverse()  # newest first
    return rows[:500]


def _visible_companies(access: dict) -> list[dict]:
    """The companies this user may see, as [{key, name}], for the filter dropdown.
    For '*' that's every company in companies.csv; else just their listed keys."""
    if access.get("companies") == "*":
        index = load_cached("companies.csv", _parse_companies) or {}
        keys = list(index.keys())
    else:
        keys = list(access.get("companies") or [])
    return [{"key": k, "name": _label_for(k)} for k in keys]


# ── routes ───────────────────────────────────────────────────────────────────
@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_home(request: Request):
    email, access = _require_user(request)
    if not email:
        return HTMLResponse(_env.get_template("login.html").render(), status_code=401)
    if not access:
        return HTMLResponse(
            _env.get_template("denied.html").render(email=email), status_code=403)

    recs = _recordings(access)
    voicemails = [r for r in recs if r["dept"] in ("direct", "main", "") or "voicemail" in r["dept"]]
    leads = _leads(access)
    calls = _call_log(access)
    companies = _visible_companies(access)
    scope = "all companies" if access["companies"] == "*" else ", ".join(
        c["name"] for c in companies) or "none"
    return HTMLResponse(_env.get_template("index.html").render(
        email=email, role=access["role"], scope=scope, companies=companies,
        recordings=recs, voicemails=voicemails, leads=leads, calls=calls,
    ))


@router.get("/dashboard/audio/{path:path}")
async def dashboard_audio(request: Request, path: str):
    """Stream a recording file — only after auth + company check (recordings/ is
    not statically served). `path` is the index.csv relative audio_file."""
    email, access = _require_user(request)
    if not access:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    # Resolve safely under recordings_dir (block path traversal).
    base = Path(settings.recordings_dir).resolve()
    target = (base / path).resolve()
    if base not in target.parents or not target.is_file():
        return JSONResponse(status_code=404, content={"error": "not found"})

    # Confirm this file belongs to a company the user may see (look it up in index).
    index = base / "index.csv"
    allowed = False
    if index.is_file():
        with index.open(newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r.get("audio_file", "") == path:
                    key = _row_key(r.get("company", ""))
                    allowed = (not key) or can_see(access, key)
                    break
    if not allowed:
        return JSONResponse(status_code=403, content={"error": "forbidden"})
    return FileResponse(str(target), media_type="audio/mpeg")
