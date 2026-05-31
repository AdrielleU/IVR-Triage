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
from app.services.calllog import log_event
from app.services.companies import get_company, normalize
from app.services.contacts import lookup_caller
from app.services.options import get_options
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

# Loop guard: an invalid keypress re-prompts the menu (Redirect), carrying an
# ?attempt counter. After this many invalid tries we stop re-prompting and take a
# message instead, so a caller mashing wrong keys can't loop the menu forever.
MAX_MENU_ATTEMPTS = 3


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


def _logc(event: str, company: dict | None, *, frm: str = "", to: str = "",
          sid: str = "", contact: str | None = None, detail: str = "") -> None:
    """Thin wrapper over log_event that fills in company name + label (no-op unless
    LOG_CALLS is on; never raises)."""
    log_event(event, call_sid=sid, company=_company_name(company) or "",
              label=_company_label(company), from_number=frm, to_number=to,
              contact=contact or "", detail=detail)


def _menu_audio(company: dict | None, co: str = "") -> str | None:
    # Routed through prompt_audio so the menu greeting supports the same local-file
    # hosting / per-company convention as the other prompts (full URL still works).
    return prompt_audio("menu", company, co)


def _ai_assistant_id(company: dict | None) -> str:
    """The tenant's Telnyx AI Assistant id, or "" if AI handoff isn't enabled."""
    return (company.get("ai_assistant_id") if company else "") or settings.ai_assistant_id


def _fallback_action(company: dict | None) -> str:
    """What a non-responsive caller gets at a dead end: "voicemail" | "ai" | "close".
    From companies.csv `fallback_action`, else FALLBACK_ACTION env, else "voicemail"."""
    raw = (company.get("fallback_action") if company else "") or settings.fallback_action or "voicemail"
    return raw.strip().lower()


def _fallback_response(company: dict | None, co: str,
                       caller_name: str | None = None, caller_number: str = "") -> Response:
    """Terminal action for a non-responsive caller, per the company's `fallback`:
    connect the AI assistant, record voicemail, or politely close. "ai" with no
    assistant configured degrades to voicemail so a caller is never just dropped."""
    action = _fallback_action(company)
    if action == "ai" and _ai_assistant_id(company):
        return _connect_ai(_ai_assistant_id(company))
    if action == "close":
        return _render("closing.xml.j2", audio_url=prompt_audio("closing", company, co))
    return _voicemail(company, "main", co)  # "voicemail" (and the safe default)


# Routing destinations that mean "the configured AI assistant" rather than a dialable
# SIP/PSTN endpoint. The "ai" sentinel resolves to the tenant's ai_assistant_id, so a
# routing row never has to hard-code the raw id (and can't break on its format).
_AI_SENTINELS = {"ai", "assistant", "ai-assistant"}


def _assistant_for(dest: str, company: dict | None) -> str | None:
    """The AI assistant id a routing destination refers to, or None if it's a normal
    dialable destination. Accepts a literal "assistant-…" id or the "ai" sentinel
    (which resolves to the tenant's configured id; None if none is configured)."""
    d = (dest or "").strip()
    if d.startswith("assistant-"):
        return d
    if d.lower() in _AI_SENTINELS:
        return _ai_assistant_id(company) or None
    return None


def _busy_options(company: dict | None, co: str) -> list[dict]:
    """Keypress options offered at the busy/voicemail prompt: from data/options.csv
    (context "busy"), else a default of "press 1 -> AI" when an assistant is
    configured, else none. Each is {digit, label, destination}."""
    opts = get_options(co, "busy")
    if opts:
        return opts
    if _ai_assistant_id(company):
        return [{"digit": "1", "label": "our virtual assistant", "destination": "ai"}]
    return []


def _route_to(company: dict | None, co: str, destination: str,
              caller_name: str | None, caller_number: str) -> Response:
    """Send the caller to a busy-prompt option's destination: an AI assistant, a
    department's ring chain, or a raw PSTN/SIP number (which then falls over to
    voicemail like any other dial)."""
    aid = _assistant_for(destination, company)
    if aid:
        return _connect_ai(aid)
    if destination.strip().lower() in ("voicemail", "vm", "message"):
        return _voicemail(company, "main", co)  # explicit "leave a message" choice
    if _stages(company, destination, co):  # a department key with a configured chain
        return _dial_stage(company, destination, 0, co,
                           caller_name=caller_name, caller_number=caller_number)
    # A raw number/SIP: one-shot dial that fails over to voicemail (dept "main" has
    # no stages, so /after-dial's stage+1 lands straight in voicemail).
    return _render(
        "transfer.xml.j2",
        department="us",
        destinations=[destination],
        dial_timeout=settings.dial_timeout,
        from_display=_from_display(company, "operator", caller_name, caller_number),
        action_url=f"{settings.base_url}/texml/after-dial?dept=main&stage=0&co={co}",
        record_calls=settings.record_calls,
        recording_callback=f"{settings.base_url}/texml/recording?dept=main&co={co}",
        intro_text="Please hold while we connect you.",
    )


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
               template: str = "voicemail.xml.j2", offer_options: bool = False,
               attempt: int = 1) -> Response:
    # The "no agents configured" case (unavailable.xml.j2) can have its own clip
    # ("all agents are busy, leave your name and number…"); if none is set it falls
    # back to the generic voicemail clip, then to TTS.
    if "unavailable" in template:
        audio_url = prompt_audio("unavailable", company, co) or prompt_audio("voicemail", company, co)
    else:
        audio_url = prompt_audio("voicemail", company, co)
    # When offer_options is set, the prompt gathers configurable "press <digit> for
    # <label>" options (data/options.csv, else default press-1->AI). Both the keypress
    # and the no-press timeout post back to /vm-option with this attempt index, which
    # bounds how many times the prompt repeats before the call is politely closed.
    options = _busy_options(company, co) if offer_options else []
    option_action = (f"{settings.base_url}/texml/vm-option?co={co}&dept={dept_key}&attempt={attempt}"
                     if options else "")
    return _render(
        template,
        department=LABELS.get(dept_key, "us"),
        dept_key=dept_key,
        co=co,
        max_seconds=settings.voicemail_max_seconds,
        audio_url=audio_url,
        options=options,
        option_action=option_action,
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
            return _voicemail(company, dept_key, co, offer_options=True)
        return _render("goodbye.xml.j2", audio_url=prompt_audio("goodbye", company, co))
    # An AI-assistant destination is terminal: <Connect> it to THIS leg instead of
    # opening a new <Dial> leg. Give an assistant its own priority/stage; if a stage
    # mixes one in, the assistant wins (you wouldn't ring a human and an AI at once).
    # A destination is an assistant if it's a literal "assistant-…" id or the "ai"
    # sentinel resolved to the tenant's configured id.
    for dest in stages[stage]:
        assistant = _assistant_for(dest, company)
        if assistant:
            return _connect_ai(assistant)
    # Drop any AI sentinels we couldn't resolve (e.g. "ai" with no assistant configured)
    # so we never <Dial> a literal "ai"; an emptied stage falls over to the next one.
    dialable = [d for d in stages[stage] if d.strip().lower() not in _AI_SENTINELS]
    if not dialable:
        return _dial_stage(company, dept_key, stage + 1, co, caller_name=caller_name,
                           caller_number=caller_number, intro_audio_url=intro_audio_url,
                           intro_text=intro_text)
    return _render(
        "transfer.xml.j2",
        department=LABELS.get(dept_key, "us"),
        destinations=dialable,
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
        _logc("after_hours", company, frm=From, to=To, sid=CallSid)
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
        _logc("direct", company, frm=From, to=To, sid=CallSid,
              contact=contact["name"] if contact else None, detail="auto-ring")
        return _dial_stage(
            company, "direct", 0, co,
            caller_name=contact["name"] if contact else None, caller_number=From,
            intro_audio_url=prompt_audio("menu", company, co),
            intro_text="Please hold while we connect you.",
        )

    # Loop guard: too many no-input/invalid menu attempts -> stop re-prompting and
    # close the call politely (a caller who never makes a valid choice is treated
    # like the busy prompt's non-responder).
    attempt = int(request.query_params.get("attempt", "0") or 0)
    if attempt >= MAX_MENU_ATTEMPTS:
        # No valid selection after the menu re-prompts -> politely terminate. The menu
        # deliberately does NOT fall to voicemail/AI; fallback_action only applies once
        # the caller has CHOSEN a department (see _fallback_response).
        log.info("Menu giving up after %d attempts -> closing: from=%s", attempt, From)
        _logc("menu_giveup", company, frm=From, to=To, sid=CallSid, detail=str(attempt))
        return _render("closing.xml.j2", audio_url=prompt_audio("closing", company, co))

    contact = await lookup_caller(From)
    if contact:
        log.info(
            "Incoming call: from=%s to=%s company=%r -> KNOWN name=%r tier=%r",
            From, To, _company_name(company), contact["name"], contact.get("tier"),
        )
    else:
        log.info("Incoming call: from=%s to=%s company=%r -> no contact match",
                 From, To, _company_name(company))
    _logc("incoming", company, frm=From, to=To, sid=CallSid,
          contact=contact["name"] if contact else None,
          detail=(contact.get("tier") or "known") if contact else "unknown")

    return _render("menu.xml.j2", caller_name=contact["name"] if contact else None,
                   company_name=_company_name(company),
                   menu_audio_url=_menu_audio(company, co),
                   ai_enabled=bool(_ai_assistant_id(company)),
                   announce_recording=settings.announce_recording,
                   action_url=f"{settings.base_url}/texml/handle-input?attempt={attempt}",
                   reprompt_url=f"{settings.base_url}/texml/menu?attempt={attempt + 1}")


@router.post("/handle-input")
async def handle_input(request: Request):
    """Route the caller to the right department based on their keypress."""
    form = await verified_form(request)
    digit, From, To = form.get("Digits", ""), form.get("From", ""), form.get("To", "")
    CallSid = form.get("CallSid", "")
    attempt = int(request.query_params.get("attempt", "0") or 0)
    company = get_company(To)
    co = normalize(To)
    log.info("Menu selection: digit=%s from=%s company=%r", digit, From, _company_name(company))

    # AI handoff: <Connect> the assistant onto this leg instead of <Dial>ing out.
    if digit == AI_DIGIT and _ai_assistant_id(company):
        log.info("Connecting caller to AI assistant: from=%s company=%r", From, _company_name(company))
        _logc("selection", company, frm=From, to=To, sid=CallSid, detail="4:ai-assistant")
        return _connect_ai(_ai_assistant_id(company))

    dept = DEPARTMENTS.get(digit)
    if dept is None:
        _logc("selection", company, frm=From, to=To, sid=CallSid, detail=f"{digit or 'none'}:invalid")
        # Re-prompt, advancing the attempt counter so the menu loop is capped.
        return _render("invalid.xml.j2", audio_url=prompt_audio("invalid", company, co),
                       menu_url=f"{settings.base_url}/texml/menu?attempt={attempt + 1}")

    _, key = dept
    if not _stages(company, key, co):
        # Nobody configured for this option — take a message instead of dropping.
        _logc("selection", company, frm=From, to=To, sid=CallSid, detail=f"{key}:unavailable")
        return _voicemail(company, key, co, template="unavailable.xml.j2", offer_options=True)

    _logc("selection", company, frm=From, to=To, sid=CallSid, detail=key)

    # Re-resolve the caller so the agent's phone shows "LABEL-GRP-Name". Local-CSV
    # first and mtime-cached, so this is essentially free and still off the path.
    contact = await lookup_caller(From)
    return _dial_stage(company, key, 0, co,
                       caller_name=contact["name"] if contact else None, caller_number=From)


@router.get("/vm-option")
@router.post("/vm-option")
async def voicemail_option(request: Request):
    """Busy/voicemail prompt keypress handler. A digit matching a configured option
    routes to its destination (AI / department / number / voicemail). A non-press
    (the Gather timed out and Redirected here) or an unmapped key re-plays the prompt
    up to busy_prompt_repeats times, then plays the closing message and hangs up — so
    a non-responsive caller can't hold the line."""
    form = await verified_form(request)
    digit, From, To = form.get("Digits", ""), form.get("From", ""), form.get("To", "")
    co = request.query_params.get("co", "") or normalize(To)
    dept = request.query_params.get("dept", "")
    attempt = int(request.query_params.get("attempt", "1") or 1)
    company = get_company(co)

    option = next((o for o in _busy_options(company, co) if o["digit"] == digit), None)
    if option:
        log.info("Busy-prompt option: digit=%s -> %s from=%s company=%r",
                 digit, option["destination"], From, _company_name(company))
        _logc("vm_option", company, frm=From, to=To, sid=form.get("CallSid", ""),
              detail=f"{digit}:{option['destination']}")
        contact = await lookup_caller(From)
        return _route_to(company, co, option["destination"],
                         contact["name"] if contact else None, From)

    # No press (timeout redirect) or an unmapped key: re-prompt up to the cap, then
    # do the company's fallback action (voicemail / ai / close).
    if attempt + 1 <= settings.busy_prompt_repeats:
        return _voicemail(company, dept, co, offer_options=True, attempt=attempt + 1)
    action = _fallback_action(company)
    log.info("Busy prompt unanswered after %d plays -> %s: from=%s", attempt, action, From)
    _logc("busy_giveup", company, frm=From, to=To, sid=form.get("CallSid", ""),
          detail=f"{attempt}:{action}")
    contact = await lookup_caller(From)
    return _fallback_response(company, co, contact["name"] if contact else None, From)


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
    _logc("dial", company, frm=form.get("From", ""), to=form.get("To", ""),
          sid=form.get("CallSid", ""), detail=f"{dept_key}:stage{stage}:{status or 'none'}")

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
    dept = request.query_params.get("dept", "")
    company = get_company(co)
    log.info(
        "Voicemail left: dept=%s duration=%ss url=%s",
        dept,
        form.get("RecordingDuration", "?"),
        form.get("RecordingUrl", "?"),
    )
    _logc("voicemail", company, frm=form.get("From", ""), to=form.get("To", ""),
          sid=form.get("CallSid", ""), detail=f"{dept}:{form.get('RecordingDuration', '?')}s")
    return _render("goodbye.xml.j2", audio_url=prompt_audio("goodbye", company, co))


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
