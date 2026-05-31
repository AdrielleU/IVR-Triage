# CLAUDE.md — Telnyx Phone IVR (no-AI, cost-optimized)

## What this is

A small FastAPI app serving a **TeXML** IVR for a Telnyx phone number. Callers
hear a DTMF menu (1 sales / 2 support / 3 billing / 0 operator) and are
transferred to the right human team. **No AI voice** — calls ride plain
telephony minutes, not AI minutes. Inbound callers are matched to HubSpot by
phone so they can be logged and greeted by name.

## Design constraints (do not regress)

1. **No AI voice.** AI Assistant minutes cost $0.05–0.08/min vs ~$0.01/min for
   plain telephony. The whole point is to avoid them. Don't reintroduce
   `<Connect><AI>` unless explicitly asked.
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
app/services/hubspot.py  # HubSpot API (sync script + optional live fallback)
scripts/sync_hubspot.py  # HubSpot contacts -> data/contacts.csv
texml/*.xml.j2           # EDITABLE call XML (Jinja2, auto_reload=True)
data/*.csv               # EDITABLE contacts.csv / hours.csv / holidays.csv (mtime-cached)
Dockerfile  docker-compose.yml  requirements.txt  README.md  .env.example
```

Operational data lives in editable files, not code: `data/contacts.csv`
(caller matching, last-10-digit match), `data/hours.csv` (weekly schedule).
Public holidays are computed by the `holidays` lib (`HOLIDAY_COUNTRY`/`SUBDIV`,
correct floating/observed dates per year); `data/holidays.csv` is ONLY for
company-specific closures on top. Don't hard-code standard holidays. Both `texml/` and `data/` are volume-mounted in compose so
edits apply with no restart. Routing fails over in stages: `<KEY>_agents` (SIP)
-> `<KEY>_fallback` (PSTN) -> voicemail, advanced by /texml/after-dial on a
non-`completed` DialCallStatus. Caller match is local-CSV-first; the HubSpot API
is only touched by the sync script or when HUBSPOT_LIVE_FALLBACK is on.

All call XML lives in `texml/` templates, rendered with Jinja2 (`auto_reload`,
`autoescape` on). Routing/business-hours/CRM/signature logic stays in Python;
prompts, menus, and voicemail wording are edited in the templates with no restart.
Don't move XML back inline. Pass *data* (not pre-built XML) into templates so
autoescape protects against a caller name / company breaking the document.

## Endpoints

- `GET/POST /texml/menu` — entry: business-hours gate, CRM lookup, greeting + gather.
- `POST /texml/handle-input` — rings the chosen department's agents (`<Dial>` of
  SIP URIs and/or PSTN numbers from `<KEY>_agents`).
- `GET/POST /texml/after-dial` — post-dial: answered → goodbye; no-answer → voicemail.
- `POST /texml/voicemail-done`, `POST /texml/recording` — voicemail capture + URL log.
- `GET /health` — health check.

Agents are SIP softphones registered to `sip.telnyx.com` (Telnyx is the SIP
server; the app never speaks SIP — it only returns `<Dial><Sip>`). RTP/SIP never
transits this app or Cloudflare; only the HTTP webhooks do.

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

## Cost cheat-sheet (2026)

Plain inbound ~$0.005–0.01/min · AI minute $0.05–0.08/min (avoided) · transfer
~$0.10 each · US number ~$1/mo · hosting $0–5/mo. The "free AI models" claim
refers to *tokens*, not the metered AI minute.
