"""Store call/voicemail recordings as paired files — no database needed.

Files are organized by date (year/month/day). Within a day, three files share one
base name (so audio and transcript can never be mismatched). One top-level index.csv
lists everything with relative paths, so you can still search across all days:

    recordings/
      2026/05/30/
        18-22-05_sales_+14155551234_<callsid>.mp3   audio
        18-22-05_sales_+14155551234_<callsid>.txt   transcript (if transcribe_enabled)
        18-22-05_sales_+14155551234_<callsid>.json  metadata sidecar
      index.csv    one row per recording, with its year/month/day path

Transcription uses local faster-whisper (optional dep). Everything runs in a worker
thread off the request path, so it never blocks webhooks or the caller.
"""

import csv
import json
import logging
import re
from pathlib import Path

import httpx

from app.config import settings

log = logging.getLogger("ivr")

_model = None  # lazily loaded faster-whisper model, reused across calls


def _get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel  # optional dep; imported only when used

        log.info("Loading whisper model %r (first use)...", settings.whisper_model)
        _model = WhisperModel(settings.whisper_model, device="cpu", compute_type="int8")
    return _model


def _safe(value: str) -> str:
    """Filesystem-safe token (keep + for E.164), capped length."""
    return re.sub(r"[^A-Za-z0-9+]", "-", (value or "unknown"))[:40]


def process_recording(*, recording_url: str, call_sid: str, dept: str,
                      caller: str, duration: str, stamp: str, company: str = "") -> None:
    """Download, optionally transcribe, and persist one recording. Thread-safe to
    call from asyncio.to_thread. Best-effort: any step failing is logged, not raised."""
    if not recording_url:
        return

    out = Path(settings.recordings_dir)
    # stamp is "YYYY-MM-DDTHH-MM-SS" -> file under recordings/YYYY/MM/DD/, named by time.
    date_part, _, time_part = stamp.partition("T")
    year, month, day = date_part.split("-")
    day_dir = out / year / month / day
    day_dir.mkdir(parents=True, exist_ok=True)

    base = "_".join(_safe(p) for p in [time_part, company, dept, caller, call_sid] if p)
    rel_dir = f"{year}/{month}/{day}"  # stored in the index so files are findable
    audio_path = day_dir / f"{base}.mp3"
    txt_path = day_dir / f"{base}.txt"

    try:
        with httpx.Client(timeout=60.0, follow_redirects=True) as client:
            resp = client.get(recording_url)
            resp.raise_for_status()
            audio_path.write_bytes(resp.content)
    except Exception as exc:  # noqa: BLE001
        log.warning("Recording download failed (%s): %s", recording_url, exc)
        return

    transcript = ""
    if settings.transcribe_enabled:
        try:
            segments, _info = _get_model().transcribe(str(audio_path))
            transcript = " ".join(seg.text.strip() for seg in segments).strip()
            txt_path.write_text(transcript, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 — missing dep or decode error
            log.warning("Transcription failed for %s: %s", base, exc)

    audio_rel = f"{rel_dir}/{audio_path.name}"
    transcript_rel = f"{rel_dir}/{txt_path.name}" if transcript else ""
    meta = {
        "timestamp": stamp, "company": company, "dept": dept, "caller": caller,
        "call_sid": call_sid, "duration_seconds": duration, "recording_url": recording_url,
        "audio_file": audio_rel,
        "transcript_file": transcript_rel or None,
        "transcript": transcript,
    }
    (day_dir / f"{base}.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    # One global index at the recordings/ root, with paths relative to it.
    index = out / "index.csv"
    write_header = not index.exists()
    with index.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if write_header:
            writer.writerow(["timestamp", "company", "dept", "caller", "call_sid",
                             "duration_seconds", "audio_file", "transcript_file", "transcript"])
        writer.writerow([stamp, company, dept, caller, call_sid, duration,
                         audio_rel, transcript_rel, transcript])

    log.info("Stored recording %s (transcript chars=%d)", audio_rel, len(transcript))
