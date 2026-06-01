"""Lead capture: append one CSV row per AI-assistant-captured lead, for follow-up.

The AI Assistant calls a webhook tool ("log this lead") that hits POST /leads on
this app; save_lead() appends a row to settings.leads_csv_path. CSV (not JSON) so
the follow-up list opens straight in Excel/Sheets — one row per caller to call back.

Filesystem only, no database. Best-effort and fully defensive, like calllog.py:
the dir + header are created on demand, writes are serialized with a lock, and any
failure is logged and swallowed. Rows hold caller PII, so the default path lives
under recordings_dir (writable + gitignored).
"""

import csv
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings

log = logging.getLogger("ivr")

# Serialize appends (the endpoint is async; concurrent calls could interleave writes).
_lock = threading.Lock()

# Column order of the follow-up CSV. Extra fields posted by the assistant are
# ignored; missing ones are blank — so the schema is stable for Excel.
FIELDS = [
    "ts", "company", "caller_number", "caller_name",
    "intent", "issue_summary", "wants_callback", "callback_number",
]


def save_lead(data: dict) -> dict:
    """Append one lead row. Returns {"ok": bool}. Never raises."""
    row = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "company": (data.get("company") or "").strip(),
        "caller_number": (data.get("caller_number") or data.get("from") or "").strip(),
        "caller_name": (data.get("caller_name") or data.get("name") or "").strip(),
        "intent": (data.get("intent") or "").strip(),
        "issue_summary": (data.get("issue_summary") or data.get("summary") or "").strip(),
        "wants_callback": (str(data.get("wants_callback") or "")).strip(),
        "callback_number": (data.get("callback_number") or "").strip(),
    }
    try:
        path = Path(settings.leads_csv_path)
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            new_file = not path.exists() or path.stat().st_size == 0
            with path.open("a", encoding="utf-8", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
                if new_file:
                    writer.writeheader()
                writer.writerow(row)
        log.info("Lead captured: company=%s number=%s intent=%s",
                 row["company"], row["caller_number"], row["intent"])
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001 — best-effort; capture must never 500 the AI tool
        log.warning("Lead write failed (%s): %s", settings.leads_csv_path, exc)
        return {"ok": False}
