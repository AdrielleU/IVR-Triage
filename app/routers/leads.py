"""Lead-capture endpoints, called by the AI Assistant's webhook tool.

POST /leads  — append a follow-up row (the assistant logs name/number/issue here).
GET  /leads  — download the accumulated CSV (your call-back list).

Both are optionally guarded by settings.leads_token (?token=… or X-Leads-Token
header) so a public URL can't be spammed. Capture is best-effort: a write failure
still returns 200 so the assistant's tool call never errors mid-conversation.
"""

import hmac
import logging

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from app.config import settings
from app.services.leads import save_lead

log = logging.getLogger("ivr")
router = APIRouter()


def _authorized(request: Request) -> bool:
    """True if no token is configured, or the request presents the right one.

    Prefer the X-Leads-Token header (kept out of access logs); the ?token= query
    param is still accepted for convenience (e.g. downloading the CSV in a browser)
    but should be avoided for the assistant's POST. Compared in constant time so a
    token can't be guessed by measuring response timing.
    """
    if not settings.leads_token:
        return True
    supplied = request.headers.get("x-leads-token") or request.query_params.get("token", "")
    return hmac.compare_digest(supplied, settings.leads_token)


@router.post("/leads")
async def capture_lead(request: Request):
    """Append a lead. Accepts JSON or form fields: company, caller_number/from,
    caller_name/name, intent, issue_summary/summary, wants_callback, callback_number."""
    if not _authorized(request):
        return JSONResponse(status_code=401, content={"ok": False, "error": "unauthorized"})

    # Accept JSON (the usual AI-tool payload) or form-encoded, tolerating either.
    data: dict = {}
    try:
        if "application/json" in request.headers.get("content-type", ""):
            data = await request.json()
        else:
            data = dict(await request.form())
    except Exception as exc:  # noqa: BLE001 — bad body shouldn't 500 the assistant tool
        log.warning("Lead payload parse failed: %s", exc)

    result = save_lead(data if isinstance(data, dict) else {})
    return JSONResponse(content=result)


@router.get("/leads")
async def download_leads(request: Request):
    """Download the follow-up CSV. Token-guarded (it holds caller PII)."""
    if not _authorized(request):
        return JSONResponse(status_code=401, content={"ok": False, "error": "unauthorized"})
    from pathlib import Path

    path = Path(settings.leads_csv_path)
    if not path.is_file():
        return PlainTextResponse("No leads captured yet.\n", status_code=404)
    return FileResponse(str(path), media_type="text/csv", filename="leads.csv")
