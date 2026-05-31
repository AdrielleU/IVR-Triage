"""Opt-in historical call log: one JSON object per line (JSON-Lines).

When settings.log_calls is on, log_event() appends a human-readable JSON record
per call event (incoming, selection, dial outcome, voicemail, AI handoff) to
settings.call_log_path. JSON-Lines is append-safe (unlike a single JSON array,
which would need a full rewrite per call and corrupt under concurrency) and still
readable — open the file and read one object per line, or grep/import it.

Filesystem only, no database. Best-effort and fully defensive: the directory is
created on demand, writes are serialized with a lock, and any failure is logged
and swallowed so the call is NEVER affected. Records are PII (caller numbers /
names), so the default path lives under recordings_dir (writable + gitignored).
"""

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings

log = logging.getLogger("ivr")

# Serialize appends: log_event may be reached from the event loop and (via the
# recording callback's thread) a worker thread, so guard the file write.
_lock = threading.Lock()


def log_event(event: str, *, call_sid: str = "", company: str = "", label: str = "",
              from_number: str = "", to_number: str = "", contact: str = "",
              detail: str = "") -> None:
    """Append one call event as a JSON line. No-op unless settings.log_calls.

    Never raises: a logging failure must not break a live call.
    """
    if not settings.log_calls:
        return

    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event": event,
        "call_sid": call_sid,
        "company": company,
        "label": label,
        "from": from_number,
        "to": to_number,
        "contact": contact,
        "detail": detail,
    }
    try:
        line = json.dumps(record, ensure_ascii=False)
        path = Path(settings.call_log_path)
        with _lock:
            # Create the dir on demand so a fresh deploy / custom path can't error.
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception as exc:  # noqa: BLE001 — best-effort; a call log must never break a call
        log.warning("Call-log write failed (%s): %s", settings.call_log_path, exc)
