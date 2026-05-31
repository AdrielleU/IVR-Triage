# CLAUDE.md — Telnyx Phone IVR (cost-optimized, optional AI handoff)

## What this is

A small FastAPI app serving a **TeXML** IVR for a Telnyx phone number. Callers
hear a DTMF menu (1 sales / 2 support / 3 billing / 0 operator) and are
transferred to the right human team on **plain telephony minutes** — the human
routing never touches metered AI minutes. Inbound callers are matched to HubSpot
by phone so they can be logged and greeted by name.

Optionally, setting `AI_ASSISTANT_ID` adds a **"press 4" handoff to a Telnyx AI
Assistant**. This is the one path that uses AI minutes, and it's deliberately
built to stay single-leg (see "AI Assistant handoff" below) so it never
double-bills.

## Design constraints (do not regress)

1. **AI voice is opt-in and single-leg only.** AI Assistant minutes cost
   ~$0.05/min on top of telephony, so human routing stays AI-free by default.
   The *only* sanctioned AI path is the `AI_ASSISTANT_ID` "press 4" handoff,
   which uses `<Connect><AIAssistant>` to attach the assistant to the **existing**
   call leg. Never reach an assistant (or any transfer target) with a `<Dial>` to
   a phone number: a `<Dial>` originates a *second concurrent leg*, so you pay two
   metered telephony legs at once ("charged twice"). `<Connect>` adds no second
   leg — only the AI minute. Don't add `<Connect><AI>` (the old inline form) or
   dial-based AI. (The separate $0.10 SIP-REFER transfer surcharge is not incurred
   by TeXML `<Dial>` — it bridges, it doesn't REFER; see the cost cheat-sheet.)
2. **The CRM is never on the critical path.** `lookup_caller` has a 2s timeout
   and returns `None` on any error — a slow/down HubSpot must never block a call.
3. **Webhooks must be verifiable.** Telnyx signs with Ed25519 over
   `{timestamp}|{raw_body}`. Read the raw body before parsing the form (see
   `app/security.verified_form`), because FastAPI consumes a form body before
   dependencies run.
4. **Keep the menu short.** Every billed second counts; silent calls hang up via
   the Gather `timeout`, they don't loop.

## Layout

```
main.py                  # FastAPI app + /health
app/config.py            # env-driven Settings (extra vars ignored)
app/security.py          # verified_form(): Ed25519 verify + form parse
app/routers/texml.py     # routing logic; renders templates; failover staging
app/services/contacts.py # caller match: local CSV first, optional HubSpot fallback
app/services/schedule.py # business hours from data/hours.csv + holidays.csv
app/services/datafiles.py# mtime-cached CSV loader
app/services/routing.py  # data/routing.csv roster -> ordered ring stages per dept
app/services/hubspot.py  # HubSpot API (sync script + optional live fallback)
scripts/sync_hubspot.py  # HubSpot contacts -> data/contacts.csv
texml/*.xml.j2           # EDITABLE call XML (Jinja2, auto_reload=True)
texml/connect-ai.xml.j2  # <Connect><AIAssistant> handoff (same leg, no extra leg/fee)
data/*.csv               # EDITABLE contacts.csv / hours.csv / holidays.csv (mtime-cached)
Dockerfile  docker-compose.yml  requirements.txt  README.md  .env.example
```

Multi-tenant: one deployment serves many companies, keyed by the dialed number
(`To`). `app/services/companies.py` resolves `To` -> a `data/companies.csv` row
(name, menu_audio_url, per-dept agents/fallback); no match -> single-tenant env
config. The company key (normalized digits) is threaded through callbacks as the
`co` query param so after-dial/recording resolve the right tenant. Don't put the
whole company row in URLs — re-resolve via `co`.

Operational data lives in editable files, not code: `data/contacts.csv`
(caller matching, last-10-digit match), `data/hours.csv` (weekly schedule).
Public holidays are computed by the `holidays` lib (`HOLIDAY_COUNTRY`/`SUBDIV`,
correct floating/observed dates per year); `data/holidays.csv` is ONLY for
company-specific closures on top. Don't hard-code standard holidays. Both `texml/` and `data/` are volume-mounted in compose so
edits apply with no restart. Routing fails over in stages, advanced by
/texml/after-dial on a non-`completed` DialCallStatus, ending in voicemail.
A department's stages are resolved by `_stages()` first-hit-wins:
`data/routing.csv` (per-agent roster) -> `data/companies.csv` columns ->
`<KEY>_agents`/`<KEY>_fallback` env. Caller match is local-CSV-first; the HubSpot
API is only touched by the sync script or when HUBSPOT_LIVE_FALLBACK is on.

`data/routing.csv` (`app/services/routing.py`) is the top routing layer — one row
per destination: `company,department,name,destination,extension,priority,active`.
Rows with the same `priority` ring together; higher priorities are later
fail-over stages. A `destination` is a SIP URI, a PSTN number, OR a Telnyx AI
Assistant id (`assistant-...`). An assistant destination is **terminal**: in
`_dial_stage` it renders `<Connect><AIAssistant>` on the current leg (no new
`<Dial>` leg) rather than a transfer — so a department can ring humans first and
fail over to an AI assistant before voicemail, and different tenants/departments
can point at different assistants. `company` blank = default/single-tenant, else
the dialed number (last-10-digit key). No routing.csv -> companies.csv/env as
before. `data/routing.csv` is gitignored (internal numbers); ship from
`routing.example.csv`. `extension` is reserved for a future dial-by-extension
feature; today it's a label.

All call XML lives in `texml/` templates, rendered with Jinja2 (`auto_reload`,
`autoescape` on). Routing/business-hours/CRM/signature logic stays in Python;
prompts, menus, and voicemail wording are edited in the templates with no restart.
Don't move XML back inline. Pass *data* (not pre-built XML) into templates so
autoescape protects against a caller name / company breaking the document.

## Endpoints

- `GET/POST /texml/menu` — entry: business-hours gate, CRM lookup, greeting + gather.
- `POST /texml/handle-input` — rings the chosen department's agents (`<Dial>` of
  SIP URIs and/or PSTN numbers from `<KEY>_agents`). Digit `4` is special: when
  an `ai_assistant_id` is configured it renders `connect-ai.xml.j2`
  (`<Connect><AIAssistant>`) instead of dialing — assistant on the same leg.
- `GET/POST /texml/after-dial` — post-dial: answered → goodbye; no-answer → voicemail.
- `POST /texml/voicemail-done`, `POST /texml/recording` — voicemail capture + URL log.
- `GET /health` — health check.

Agents are SIP softphones registered to `sip.telnyx.com` (Telnyx is the SIP
server; the app never speaks SIP — it only returns `<Dial><Sip>`). RTP/SIP never
transits this app or Cloudflare; only the HTTP webhooks do.

## AI Assistant handoff (billing-critical)

Opt-in via `AI_ASSISTANT_ID` (per-company: companies.csv `ai_assistant_id`).
When set, the menu offers "press 4" and `/texml/handle-input` renders
`connect-ai.xml.j2`:

```xml
<Connect>
  <AIAssistant id="assistant-…"/>
</Connect>
```

Why `<Connect>` and not `<Dial>` — the whole point:

- `<Connect><AIAssistant>` runs the assistant **on the current inbound leg**. No
  second telephony leg. Cost during the AI conversation is just the one inbound
  leg (~$0.002/min + SIP-trunk fee) **+ ~$0.05/min AI**.
- A `<Dial>` to an AI behind a phone number would run **two** metered legs
  concurrently — the "charged twice" trap. (It also needs a separate DID for the
  AI.) Don't.

The assistant referenced by `id` owns everything about the conversation —
**voice, greeting, system instructions, and its own transfer-to-human tool** —
configured on the AI Assistant resource (Mission Control portal or the AI
Assistant API), NOT in this XML. The template passes only the `id` plus an
optional `AI_INTRO_AUDIO_URL` / TTS "connecting you…" line. So escalating from
the AI to a human is the assistant's job, kept out of this app — and if that
escalation uses a SIP-REFER transfer it's the one place the $0.10 surcharge can
appear. Keep this path single-leg; if you ever need to bridge a human in, do it
from the assistant's tools, not by wrapping the assistant in a `<Dial>`.

## Configuration (`.env`; see `.env.example`)

- `BASE_URL` (required) — public URL Telnyx hits; used to build the Gather action.
- `TELNYX_API_KEY`, `TELNYX_PUBLIC_KEY` — auth + signature key.
- `VERIFY_SIGNATURES` — `true` in production to reject spoofed webhooks.
- `SALES_AGENTS` / `SUPPORT_AGENTS` / `BILLING_AGENTS` / `OPERATOR_AGENTS` —
  comma-separated ring destinations per option; each is a SIP URI
  (`sip:user@sip.telnyx.com`) or PSTN number. Empty → straight to voicemail.
- `DIAL_TIMEOUT`, `ENABLE_VOICEMAIL`, `VOICEMAIL_MAX_SECONDS` — ring/voicemail.
- `HUBSPOT_TOKEN` (optional) — enables caller matching.
- `MENU_AUDIO_URL` (optional) — pre-recorded greeting, skips per-call TTS.
- `AI_ASSISTANT_ID` (optional) — Telnyx AI Assistant id; enables the "press 4"
  single-leg AI handoff. `AI_INTRO_AUDIO_URL` (optional) plays a pre-recorded
  "connecting you…" clip instead of TTS before the `<Connect>`.
- Business hours (optional): `ENFORCE_BUSINESS_HOURS`, `BUSINESS_TIMEZONE`,
  `BUSINESS_OPEN_HOUR`, `BUSINESS_CLOSE_HOUR`, `BUSINESS_DAYS` (Mon=0..Sun=6),
  `AFTER_HOURS_AGENTS`.
- `TUNNEL_TOKEN` (optional) — Cloudflare Tunnel token for the compose `tunnel` profile.

## Run

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill BASE_URL + team numbers
uvicorn main:app --reload --port 8000
ngrok http 8000               # put the https URL in BASE_URL
```

In Telnyx: create a **TeXML Application** pointing at `https://<BASE_URL>/texml/menu`
and assign your number to it.

## Recording & storage

- ANNOUNCE_RECORDING plays the legal disclosure at menu start (compliance). The
  disclosure must precede any <Dial record=...>; it does (menu -> transfer).
- RECORD_CALLS adds record="record-from-answer" to the transfer <Dial>.
- SAVE_RECORDINGS / TRANSCRIBE_ENABLED store recordings under
  recordings/YYYY/MM/DD/ as paired files (<base>.mp3/.txt/.json sharing a base
  name) + a top-level index.csv with relative paths — filesystem only, NO database.
  Transcription is local faster-whisper (optional dep, requirements-transcribe.txt),
  run via asyncio.to_thread off the request path. Best-effort: missing dep / download
  failure is logged, audio still saved. recordings/ is gitignored (PII).

## Resilience

- `/health` returns 503 (not 200) when the app can't serve calls (templates
  missing) so an external monitor on the public URL is a real readiness signal.
- Optional outbound heartbeat (`HEARTBEAT_URL`) is a dead-man's switch started in
  main.py's lifespan; failures to ping are logged, never fatal.
- Call survival during an outage is handled OUTSIDE this app: the Telnyx TeXML
  Application's **Failover URL** points at a Telnyx-hosted TeXML Bin
  (`texml/failover.example.xml`). Don't put the failover on this server.

## Tech stack

Python 3.11+, FastAPI, Pydantic Settings, httpx (async), cryptography (Ed25519),
uvicorn. Keep everything async; never block the event loop in a webhook.

## Cost cheat-sheet (verified vs telnyx.com/pricing, May 2026)

Per-leg billing — **every leg alive at once meters separately**. Two concurrent
legs = charged twice.

- **Voice, each leg:** $0.002/min + per-minute SIP-trunk fee (varies by
  destination; see Telnyx's SIP price sheet). Inbound and outbound both.
- **SIP interface:** $0.002/min (on-net is cheap, *not* free).
- **SIP-REFER transfer surcharge:** **$0.10 per invocation** — applies ONLY to the
  Call Control `transfer`/SIP-REFER command. **TeXML `<Dial>` does NOT incur it**
  (it bridges a new leg, it doesn't REFER). This app uses `<Dial>`, so routine
  IVR→agent handoffs are not surcharged. The fee can appear if the AI assistant
  escalates to a human via REFER.
- **AI Assistant (Conversational AI):** ~$0.05/min (STT + orchestration + native
  TTS included) **on top of** the telephony leg; premium TTS / external LLM extra.
- US number ~$1/mo · hosting $0–5/mo.

Single-leg vs double-leg, same call:
- Human via SIP agent: inbound leg + SIP leg ≈ $0.004/min while bridged, no
  surcharge. PSTN fallback adds the trunk termination fee on the second leg.
- AI via `<Connect>`: one inbound leg + ~$0.05/min AI. The trap to avoid is
  `<Dial>`-to-AI = two concurrent legs (see "AI Assistant handoff").

A ballpark 5-min IVR→SIP-agent call is ≈ $0.035 (two cheap legs, no surcharge);
the same via AI is ≈ $0.275 (the AI minute dominates). Confirm exact per-area-code
rates and whether any REFER surcharge applies on your first real invoice.

The "free AI models" claim refers to *tokens*, not the metered AI minute.
