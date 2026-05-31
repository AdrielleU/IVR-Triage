import asyncio
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import Response
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import settings
from app.security import verified_form
from app.services.audio import prompt_audio
from app.services.companies import get_company, normalize
from app.services.contacts import lookup_caller
from app.services.recordings import process_recording
from app.services.routing import get_stages
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

# DTMF digit that hands the caller to the AI Assistant. Handled separately from
# DEPARTMENTS because the AI path is a <Connect> (assistant on the existing leg),
# NOT a <Dial> (new leg + $0.10 transfer fee). Only offered when an assistant id
# is configured for the tenant.
AI_DIGIT = "4"


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


def _company_label(company: dict | None) -> str:
    """Short tenant code (e.g. "RAV") shown first in the agent's caller-ID display.
    From companies.csv `label`, else the COMPANY_LABEL env default, else "".
    Sanitized to letters/digits and upper-cased; meant to be ~3 chars."""
    raw = (company.get("label") if company else "") or settings.company_label or ""
    return re.sub(r"[^A-Za-z0-9]", "", raw).upper()


def _menu_audio(company: dict | None, co: str = "") -> str | None:
    # Routed through prompt_audio so the menu greeting supports the same local-file
    # hosting / per-company convention as the other prompts (full URL still works).
    return prompt_audio("menu", company, co)


def _ai_assistant_id(company: dict | None) -> str:
    """The tenant's Telnyx AI Assistant id, or "" if AI handoff isn't enabled."""
    return (company.get("ai_assistant_id") if company else "") or settings.ai_assistant_id


def _connect_ai(assistant_id: str) -> Response:
    """Attach an AI Assistant to the CURRENT leg. No new <Dial> leg — only the AI
    minute stacks on the single inbound leg. Used by the press-4 handoff and by a
    routing.csv `assistant-...` destination."""
    return _render(
        "connect-ai.xml.j2",
        assistant_id=assistant_id,
        intro_audio_url=settings.ai_intro_audio_url,
        intro_text="Connecting you to our virtual assistant. One moment please.",
    )


def _stages(company: dict | None, dept_key: str, co: str = "") -> list[list[str]]:
    """Ordered ring stages for a department.

    Resolution order, first hit wins: data/routing.csv (per-agent roster, keyed by
    the company `co`) -> data/companies.csv columns -> single-tenant env config
    (<KEY>_agents then <KEY>_fallback). Each stage is a list of destinations
    (SIP URI / PSTN number / assistant id) that ring together; later stages are
    fail-over.
    """
    routed = get_stages(co, dept_key)
    if routed is not None:
        return routed
    if company is not None:
        agents = _split(company.get(f"{dept_key}_agents", ""))
        fallback = _split(company.get(f"{dept_key}_fallback", ""))
    else:
        agents = _split(getattr(settings, f"{dept_key}_agents", "") or "")
        fallback = _split(getattr(settings, f"{dept_key}_fallback", "") or "")
    return [stage for stage in (agents, fallback) if stage]


# Telnyx fromDisplayName allows only letters, digits, spaces and - _ ~ ! +
# (no colon/parens). Strip anything else so the <Dial> isn't rejected.
_DISPLAY_DISALLOWED = re.compile(r"[^A-Za-z0-9 \-_~!+]")


def _display_number(number: str) -> str:
    """Caller number for the display name, minus the +/country-code noise:
    "+15551234567" -> "5551234567". Keeps the local digits (drops a leading US
    "1"); leaves shorter/foreign numbers as their bare digits."""
    digits = re.sub(r"\D", "", number or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def _from_display(company: dict | None, dept_key: str, caller_name: str | None,
                  caller_number: str = "") -> str:
    """Caller-ID display name the agent's softphone shows, formatted as
    "LABEL-GRP-Who" — e.g. "RAV-SUP-Jane Doe", or "RAV-SUP-5551234567" when the
    caller isn't in contacts. LABEL is the tenant code (omitted if unset), GRP is
    the first 3 letters/digits of the department, and Who is the matched contact
    name, falling back to the caller's number (country code stripped). Sanitized to
    Telnyx's allowed charset (letters/digits/spaces and - _ ~ ! +), capped 128."""
    label = _company_label(company)
    grp = re.sub(r"[^A-Za-z0-9]", "", dept_key)[:3].upper()  # sales->SAL, after_hours->AFT
    who = caller_name or _display_number(caller_number)
    raw = "-".join(p for p in (label, grp, who) if p)
    return re.sub(r"\s+", " ", _DISPLAY_DISALLOWED.sub(" ", raw)).strip()[:128]


def _voicemail(company: dict | None, dept_key: str, co: str,
               template: str = "voicemail.xml.j2") -> Response:
    # The "no agents configured" case (unavailable.xml.j2) can have its own clip
    # ("all agents are busy, leave your name and number…"); if none is set it falls
    # back to the generic voicemail clip, then to TTS.
    if "unavailable" in template:
        audio_url = prompt_audio("unavailable", company, co) or prompt_audio("voicemail", company, co)
    else:
        audio_url = prompt_audio("voicemail", company, co)
    return _render(
        template,
        department=LABELS.get(dept_key, "us"),
        dept_key=dept_key,
        co=co,
        max_seconds=settings.voicemail_max_seconds,
        audio_url=audio_url,
    )


def _dial_stage(company: dict | None, dept_key: str, stage: int, co: str,
                caller_name: str | None = None, caller_number: str = "",
                intro_audio_url: str | None = None,
                intro_text: str | None = None) -> Response:
    """Ring the given stage's destinations; past the last stage -> voicemail.

    The <Dial> action points back at /texml/after-dial with the stage and the
    company key (`co`), so a no-answer fails over to the next stage for the right
    company even if the callback omits the dialed number. caller_name (matched in
    the CRM) and caller_number form the agent's SIP caller-ID display name
    ("LABEL-GRP-Who"). intro_audio_url / intro_text override the default
    "Connecting you to <dept>" line — used by a direct line to play its own
    greeting before auto-ringing.
    """
    stages = _stages(company, dept_key, co)
    if stage >= len(stages):
        if settings.enable_voicemail:
            return _voicemail(company, dept_key, co)
        return _render("goodbye.xml.j2", audio_url=prompt_audio("goodbye", company, co))
    # An AI-assistant destination is terminal: <Connect> it to THIS leg instead of
    # opening a new <Dial> leg. Give an assistant its own priority/stage; if a stage
    # mixes one in, the assistant wins (you wouldn't ring a human and an AI at once).
    assistant = next((d for d in stages[stage] if d.startswith("assistant-")), None)
    if assistant:
        return _connect_ai(assistant)
    return _render(
        "transfer.xml.j2",
        department=LABELS.get(dept_key, "us"),
        destinations=stages[stage],
        dial_timeout=settings.dial_timeout,
        from_display=_from_display(company, dept_key, caller_name, caller_number),
        action_url=f"{settings.base_url}/texml/after-dial?dept={dept_key}&stage={stage}&co={co}",
        record_calls=settings.record_calls,
        recording_callback=f"{settings.base_url}/texml/recording?dept={dept_key}&co={co}",
        intro_audio_url=intro_audio_url,
        intro_text=intro_text,
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
        if _stages(company, "after_hours", co):
            return _dial_stage(company, "after_hours", 0, co, caller_number=From)
        return _render(
            "after_hours.xml.j2",
            dept_key="after_hours",
            co=co,
            max_seconds=settings.voicemail_max_seconds,
            open_hour=settings.business_open_hour,
            close_hour=settings.business_close_hour,
            audio_url=prompt_audio("after_hours", company, co),
        )

    # Direct line (no IVR): a number with a `direct` ring chain configured skips the
    # menu entirely — play its greeting, then auto-ring SIP -> personal line ->
    # voicemail. A number without a `direct` chain falls through to the menu below.
    if _stages(company, "direct", co):
        contact = await lookup_caller(From)
        log.info("Direct line: from=%s to=%s company=%r -> auto-ring (no menu)",
                 From, To, _company_name(company))
        return _dial_stage(
            company, "direct", 0, co,
            caller_name=contact["name"] if contact else None, caller_number=From,
            intro_audio_url=prompt_audio("menu", company, co),
            intro_text="Please hold while we connect you.",
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
                   menu_audio_url=_menu_audio(company, co),
                   ai_enabled=bool(_ai_assistant_id(company)),
                   announce_recording=settings.announce_recording)


@router.post("/handle-input")
async def handle_input(request: Request):
    """Route the caller to the right department based on their keypress."""
    form = await verified_form(request)
    digit, From, To = form.get("Digits", ""), form.get("From", ""), form.get("To", "")
    company = get_company(To)
    co = normalize(To)
    log.info("Menu selection: digit=%s from=%s company=%r", digit, From, _company_name(company))

    # AI handoff: <Connect> the assistant onto this leg instead of <Dial>ing out.
    if digit == AI_DIGIT and _ai_assistant_id(company):
        log.info("Connecting caller to AI assistant: from=%s company=%r", From, _company_name(company))
        return _connect_ai(_ai_assistant_id(company))

    dept = DEPARTMENTS.get(digit)
    if dept is None:
        return _render("invalid.xml.j2", audio_url=prompt_audio("invalid", company, co))

    _, key = dept
    if not _stages(company, key, co):
        # Nobody configured for this option — take a message instead of dropping.
        return _voicemail(company, key, co, template="unavailable.xml.j2")

    # Re-resolve the caller so the agent's phone shows "LABEL-GRP-Name". Local-CSV
    # first and mtime-cached, so this is essentially free and still off the path.
    contact = await lookup_caller(From)
    return _dial_stage(company, key, 0, co,
                       caller_name=contact["name"] if contact else None, caller_number=From)


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
        return _render("goodbye.xml.j2", audio_url=prompt_audio("goodbye", company, co))
    contact = await lookup_caller(form.get("From", ""))
    return _dial_stage(company, dept_key, stage + 1, co,
                       caller_name=contact["name"] if contact else None,
                       caller_number=form.get("From", ""))


@router.post("/voicemail-done")
async def voicemail_done(request: Request):
    """The caller finished recording. Telnyx includes the recording URL here."""
    form = await verified_form(request)
    co = request.query_params.get("co", "") or normalize(form.get("To", ""))
    log.info(
        "Voicemail left: dept=%s duration=%ss url=%s",
        request.query_params.get("dept", ""),
        form.get("RecordingDuration", "?"),
        form.get("RecordingUrl", "?"),
    )
    return _render("goodbye.xml.j2", audio_url=prompt_audio("goodbye", get_company(co), co))


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
