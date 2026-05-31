import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import Response
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import settings
from app.security import verified_form
from app.services.contacts import lookup_caller
from app.services.recordings import process_recording
from app.services.schedule import is_open

router = APIRouter()
log = logging.getLogger("ivr")

# All call XML lives in editable templates under texml/. auto_reload picks up
# edits on the next call with no restart, so non-coders can tweak prompts,
# menus, and voicemail wording directly.
TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "texml"
_env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(default=True),  # escape dynamic values so a name like "A & B" can't break the XML
    auto_reload=True,
)

# Telnyx-supported neural voice for the prompts.
VOICE = "Polly.Joanna-Neural"

# DTMF digit -> (spoken label, config key). The config key maps to <KEY>_agents.
DEPARTMENTS = {
    "1": ("sales", "sales"),
    "2": ("technical support", "support"),
    "3": ("billing and accounts", "billing"),
    "0": ("an operator", "operator"),
}
# Reverse map so callbacks (which only carry the key) can speak a friendly label.
LABELS = {key: label for label, key in DEPARTMENTS.values()}
LABELS["after_hours"] = "our on-call line"


def _render(template: str, **ctx) -> Response:
    """Render a texml/ template to a TeXML HTTP response."""
    ctx.setdefault("voice", VOICE)
    ctx.setdefault("base_url", settings.base_url)
    xml = _env.get_template(template).render(**ctx)
    return Response(content=xml, media_type="application/xml")


def _stages(dept_key: str) -> list[list[str]]:
    """Ordered ring stages for a department: agents first, then PSTN fallback.

    e.g. SALES_AGENTS=sip:a1,sip:a2 + SALES_FALLBACK=+1cell  ->  [[a1, a2], [+1cell]]
    Empty stages are dropped, so a department with no fallback is just [[a1, a2]].
    """
    stages: list[list[str]] = []
    for suffix in ("agents", "fallback"):
        raw = getattr(settings, f"{dept_key}_{suffix}", "") or ""
        dests = [item.strip() for item in raw.split(",") if item.strip()]
        if dests:
            stages.append(dests)
    return stages


def _voicemail(dept_key: str, template: str = "voicemail.xml.j2") -> Response:
    return _render(
        template,
        department=LABELS.get(dept_key, "us"),
        dept_key=dept_key,
        max_seconds=settings.voicemail_max_seconds,
    )


def _dial_stage(dept_key: str, stage: int) -> Response:
    """Ring the given stage's destinations; past the last stage -> voicemail.

    The <Dial> action points back at /texml/after-dial with this stage number, so
    a no-answer (SIP offline, busy, timeout) fails over to the next stage.
    """
    stages = _stages(dept_key)
    if stage >= len(stages):
        return _voicemail(dept_key) if settings.enable_voicemail else _render("goodbye.xml.j2")
    return _render(
        "transfer.xml.j2",
        department=LABELS.get(dept_key, "us"),
        destinations=stages[stage],
        dial_timeout=settings.dial_timeout,
        action_url=f"{settings.base_url}/texml/after-dial?dept={dept_key}&stage={stage}",
        record_calls=settings.record_calls,
        recording_callback=f"{settings.base_url}/texml/recording?dept={dept_key}",
    )


@router.get("/menu")
@router.post("/menu")
async def initial_menu(request: Request):
    """Entry point. Telnyx passes the caller's number as `From` on every webhook —
    that's how we "see" the caller, for free, with no AI."""
    form = await verified_form(request)
    From, To, CallSid = form.get("From", ""), form.get("To", ""), form.get("CallSid", "")

    if not is_open():
        log.info("After-hours call: from=%s to=%s call_sid=%s", From, To, CallSid)
        if _stages("after_hours"):
            return _dial_stage("after_hours", 0)
        return _render(
            "after_hours.xml.j2",
            dept_key="after_hours",
            max_seconds=settings.voicemail_max_seconds,
            open_hour=settings.business_open_hour,
            close_hour=settings.business_close_hour,
        )

    contact = await lookup_caller(From)
    if contact:
        log.info(
            "Incoming call: from=%s call_sid=%s -> KNOWN name=%r company=%r tier=%r",
            From, CallSid, contact["name"], contact["company"], contact.get("tier"),
        )
    else:
        log.info("Incoming call: from=%s call_sid=%s -> no contact match", From, CallSid)

    return _render("menu.xml.j2", caller_name=contact["name"] if contact else None,
                   menu_audio_url=settings.menu_audio_url,
                   announce_recording=settings.announce_recording)


@router.post("/handle-input")
async def handle_input(request: Request):
    """Route the caller to the right department based on their keypress."""
    form = await verified_form(request)
    digit, From = form.get("Digits", ""), form.get("From", "")
    log.info("Menu selection: digit=%s from=%s", digit, From)

    dept = DEPARTMENTS.get(digit)
    if dept is None:
        return _render("invalid.xml.j2")

    _, key = dept
    if not _stages(key):
        # Nobody configured for this option — take a message instead of dropping.
        return _voicemail(key, template="unavailable.xml.j2")

    return _dial_stage(key, 0)


@router.get("/after-dial")
@router.post("/after-dial")
async def after_dial(request: Request):
    """Runs after a <Dial> ends. Answered (DialCallStatus=completed) -> hang up.
    Otherwise fail over to the next ring stage (PSTN backup), then voicemail."""
    form = await verified_form(request)
    status = form.get("DialCallStatus", "")
    dept_key = request.query_params.get("dept", "support")
    stage = int(request.query_params.get("stage", "0") or 0)
    log.info("Dial finished: dept=%s stage=%s status=%s", dept_key, stage, status or "(none)")

    if status == "completed":
        return _render("goodbye.xml.j2")
    return _dial_stage(dept_key, stage + 1)


@router.post("/voicemail-done")
async def voicemail_done(request: Request):
    """The caller finished recording. Telnyx includes the recording URL here."""
    form = await verified_form(request)
    log.info(
        "Voicemail left: dept=%s duration=%ss url=%s",
        request.query_params.get("dept", ""),
        form.get("RecordingDuration", "?"),
        form.get("RecordingUrl", "?"),
    )
    return _render("goodbye.xml.j2")


@router.post("/recording")
async def recording(request: Request):
    """recordingStatusCallback: the final recording URL, delivered after the call ends.

    Logs it always; if save_recordings/transcribe_enabled is on, downloads and
    transcribes in a background thread (off the request path — no caller impact)."""
    form = await verified_form(request)
    url = form.get("RecordingUrl", "")
    dept = request.query_params.get("dept", "")
    log.info("Recording ready: dept=%s url=%s", dept, url or "?")

    if url and (settings.save_recordings or settings.transcribe_enabled):
        # datetime.now is fine here (normal app code); to_thread keeps CPU-bound
        # transcription off the event loop.
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
        asyncio.create_task(
            asyncio.to_thread(
                process_recording,
                recording_url=url,
                call_sid=form.get("CallSid", ""),
                dept=dept,
                caller=form.get("From", ""),
                duration=form.get("RecordingDuration", ""),
                stamp=stamp,
            )
        )

    return Response(status_code=204)
