import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import Response
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import settings
from app.security import verified_form
from app.services.companies import get_company, normalize
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


def _split(raw: str) -> list[str]:
    """Split a destinations cell on ; or , into a clean list."""
    return [p.strip() for p in (raw or "").replace(";", ",").split(",") if p.strip()]


def _company_name(company: dict | None) -> str | None:
    return (company.get("name") if company else None) or (settings.company_name or None)


def _menu_audio(company: dict | None) -> str | None:
    return (company.get("menu_audio_url") if company else None) or settings.menu_audio_url


def _stages(company: dict | None, dept_key: str) -> list[list[str]]:
    """Ordered ring stages for a department: agents first, then PSTN fallback.

    Per-company values come from companies.csv; if no company matched the dialed
    number, fall back to the single-tenant env config (<KEY>_agents/_fallback).
    """
    if company is not None:
        agents = _split(company.get(f"{dept_key}_agents", ""))
        fallback = _split(company.get(f"{dept_key}_fallback", ""))
    else:
        agents = _split(getattr(settings, f"{dept_key}_agents", "") or "")
        fallback = _split(getattr(settings, f"{dept_key}_fallback", "") or "")
    return [stage for stage in (agents, fallback) if stage]


def _voicemail(dept_key: str, co: str, template: str = "voicemail.xml.j2") -> Response:
    return _render(
        template,
        department=LABELS.get(dept_key, "us"),
        dept_key=dept_key,
        co=co,
        max_seconds=settings.voicemail_max_seconds,
    )


def _dial_stage(company: dict | None, dept_key: str, stage: int, co: str) -> Response:
    """Ring the given stage's destinations; past the last stage -> voicemail.

    The <Dial> action points back at /texml/after-dial with the stage and the
    company key (`co`), so a no-answer fails over to the next stage for the right
    company even if the callback omits the dialed number.
    """
    stages = _stages(company, dept_key)
    if stage >= len(stages):
        return _voicemail(dept_key, co) if settings.enable_voicemail else _render("goodbye.xml.j2")
    return _render(
        "transfer.xml.j2",
        department=LABELS.get(dept_key, "us"),
        destinations=stages[stage],
        dial_timeout=settings.dial_timeout,
        action_url=f"{settings.base_url}/texml/after-dial?dept={dept_key}&stage={stage}&co={co}",
        record_calls=settings.record_calls,
        recording_callback=f"{settings.base_url}/texml/recording?dept={dept_key}&co={co}",
    )


@router.get("/menu")
@router.post("/menu")
async def initial_menu(request: Request):
    """Entry point. Telnyx passes the caller's number as `From` on every webhook —
    that's how we "see" the caller, for free, with no AI."""
    form = await verified_form(request)
    From, To, CallSid = form.get("From", ""), form.get("To", ""), form.get("CallSid", "")
    company = get_company(To)          # which company was dialed (None -> env defaults)
    co = normalize(To)                 # company key carried through callbacks

    if not is_open():
        log.info("After-hours call: from=%s to=%s call_sid=%s", From, To, CallSid)
        if _stages(company, "after_hours"):
            return _dial_stage(company, "after_hours", 0, co)
        return _render(
            "after_hours.xml.j2",
            dept_key="after_hours",
            co=co,
            max_seconds=settings.voicemail_max_seconds,
            open_hour=settings.business_open_hour,
            close_hour=settings.business_close_hour,
        )

    contact = await lookup_caller(From)
    if contact:
        log.info(
            "Incoming call: from=%s to=%s company=%r -> KNOWN name=%r tier=%r",
            From, To, _company_name(company), contact["name"], contact.get("tier"),
        )
    else:
        log.info("Incoming call: from=%s to=%s company=%r -> no contact match",
                 From, To, _company_name(company))

    return _render("menu.xml.j2", caller_name=contact["name"] if contact else None,
                   company_name=_company_name(company),
                   menu_audio_url=_menu_audio(company),
                   announce_recording=settings.announce_recording)


@router.post("/handle-input")
async def handle_input(request: Request):
    """Route the caller to the right department based on their keypress."""
    form = await verified_form(request)
    digit, From, To = form.get("Digits", ""), form.get("From", ""), form.get("To", "")
    company = get_company(To)
    co = normalize(To)
    log.info("Menu selection: digit=%s from=%s company=%r", digit, From, _company_name(company))

    dept = DEPARTMENTS.get(digit)
    if dept is None:
        return _render("invalid.xml.j2")

    _, key = dept
    if not _stages(company, key):
        # Nobody configured for this option — take a message instead of dropping.
        return _voicemail(key, co, template="unavailable.xml.j2")

    return _dial_stage(company, key, 0, co)


@router.get("/after-dial")
@router.post("/after-dial")
async def after_dial(request: Request):
    """Runs after a <Dial> ends. Answered (DialCallStatus=completed) -> hang up.
    Otherwise fail over to the next ring stage (PSTN backup), then voicemail."""
    form = await verified_form(request)
    status = form.get("DialCallStatus", "")
    dept_key = request.query_params.get("dept", "support")
    stage = int(request.query_params.get("stage", "0") or 0)
    co = request.query_params.get("co", "") or normalize(form.get("To", ""))
    company = get_company(co)
    log.info("Dial finished: dept=%s stage=%s status=%s", dept_key, stage, status or "(none)")

    if status == "completed":
        return _render("goodbye.xml.j2")
    return _dial_stage(company, dept_key, stage + 1, co)


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
    co = request.query_params.get("co", "") or normalize(form.get("To", ""))
    company = _company_name(get_company(co)) or ""
    log.info("Recording ready: company=%r dept=%s url=%s", company, dept, url or "?")

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
                company=company,
                caller=form.get("From", ""),
                duration=form.get("RecordingDuration", ""),
                stamp=stamp,
            )
        )

    return Response(status_code=204)
