# Telnyx Phone IVR (no-AI, cost-optimized)

A small FastAPI app that serves a **TeXML** IVR for a Telnyx number. Callers hear
a menu (1 sales / 2 support / 3 billing / 0 operator), get rung through to your
agents (SIP softphones or phone numbers), and land in **voicemail** if nobody
answers. No AI voice — calls ride plain telephony minutes (~$0.01/min), not AI
minutes ($0.05–0.08/min). Inbound callers are matched against HubSpot by phone
so they're logged and greeted by name.

All the call XML lives in **editable templates** under `texml/` — change a
greeting, menu, or voicemail prompt and the next call uses it, no restart.

## How a call flows

```
Caller dials your number
      │
      ▼
Telnyx ──webhook──► /texml/menu        (business-hours check, HubSpot lookup, greeting + DTMF)
      │  caller presses a key
      ▼
Telnyx ──webhook──► /texml/handle-input   (rings that department's agents via <Dial>)
      │  no one answers (or none configured)
      ▼
Telnyx ──webhook──► /texml/after-dial  ──► voicemail (<Record>) ──► /texml/recording (URL logged)
```

Audio (SIP/RTP) flows **between the agents' softphones and Telnyx** — it never
touches this app or your network. The app only ever returns small XML documents.

## Layout

```
main.py                  # app + /health
app/config.py            # env-driven settings
app/security.py          # Telnyx Ed25519 signature verify + form parse
app/routers/texml.py     # routing logic (renders templates, failover staging)
app/services/contacts.py # caller match: local CSV first, optional HubSpot fallback
app/services/schedule.py # business hours from CSV
app/services/hubspot.py  # HubSpot API (used by sync + optional live fallback)
scripts/sync_hubspot.py  # download HubSpot contacts -> data/contacts.csv
texml/                   # EDITABLE call XML (Jinja2, auto-reloaded)
  menu / transfer / voicemail / after_hours / goodbye / invalid / unavailable
data/                    # EDITABLE operational data (mtime-cached)
  contacts.csv  hours.csv  holidays.csv
Dockerfile  docker-compose.yml  requirements.txt  .env.example
```

## Editable data files (`data/`)

No code or restart needed — edit and the next call picks it up.

- **`contacts.csv`** — `phone,name,company,tier`. Inbound numbers are matched
  here first (instant, no API). Refresh from HubSpot with
  `python scripts/sync_hubspot.py`, or drop in a CSV exported from anywhere.
  Matching ignores formatting (compares the last 10 digits).
- **`hours.csv`** — `day,open,close` per weekday (`HH:MM`; blank = closed that day).
- **`holidays.csv`** — `date,note` for **company-specific** closures only (offsite,
  etc.). Standard public holidays are computed automatically (`HOLIDAY_COUNTRY` /
  `HOLIDAY_SUBDIV`) with correct floating/observed dates every year — no annual
  editing. Set `AUTO_HOLIDAYS=false` to rely solely on the CSV.

## Multiple companies (one deployment, many numbers)

Every webhook includes the dialed number (`To`), so one app + one Telnyx TeXML
Application can serve many companies. Copy `data/companies.example.csv` →
`data/companies.csv` and list each number:

```csv
number,name,menu_audio_url,sales_agents,support_agents,billing_agents,operator_agents,sales_fallback,...
+18005550001,Acme Inc,,sip:acme1@sip.telnyx.com;sip:acme2@sip.telnyx.com,sip:acme1@sip.telnyx.com,+1...,+1...,+1...
+18005550002,Globex,https://cdn/globex.mp3,sip:globex1@sip.telnyx.com,...
```

Each company gets its own greeting (its `name` in the TTS prompt, or its own
`menu_audio_url` recording) and its own per-department agents/failover. Multiple
agents in one cell are separated by `;`. A call to a listed number uses that
company's row; an unlisted number falls back to the single-tenant `*_AGENTS` env
vars. Recordings are tagged with the company (filename + `index.csv` column).
Adding a company is a CSV edit — no redeploy. `data/companies.csv` is gitignored
(internal routing); `companies.example.csv` is the template.

## Caller routing & failover

Each option rings a stage of destinations, then fails over:

```
SALES_AGENTS (SIP softphones, ring together)
     │ no answer / SIP offline / busy
     ▼
SALES_FALLBACK (PSTN backup, e.g. a cell)     ← auto-forward if SIP fails
     │ no answer
     ▼
voicemail (<Record>)
```

Destinations are SIP URIs or PSTN numbers; multiple in a stage ring simultaneously.

## Run

### Docker (recommended)

```bash
cp .env.example .env          # fill BASE_URL + *_AGENTS
docker compose up -d                    # app on :8000
docker compose --profile tunnel up -d   # app + Cloudflare Tunnel (set TUNNEL_TOKEN)
```

The `texml/` folder is mounted in, so editing a template on the host updates the
next call with no rebuild.

### Local (no Docker)

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
ngrok http 8000               # put the https URL in BASE_URL
```

## Exposing your self-hosted instance to Telnyx

Telnyx must reach this app at a **public HTTPS URL** (its webhook). The app stays
on your own box; you just need a public front door. Three ways, fastest first:

| Option | Best for | Public URL | Open ports? |
| --- | --- | --- | --- |
| **ngrok** | Dev / quick test | random `*.ngrok.io` (static with paid plan) | no |
| **Cloudflare Tunnel** | Production self-hosting | your own `ivr.yourdomain.com` | no |
| **Tailscale Funnel / VPN + reverse proxy** | You already run a VPN / have a box with a public IP | your domain / `*.ts.net` | no (Funnel) / yes (port-forward) |

**ngrok (fastest, for testing).** `ngrok http 8000`, copy the `https://…` URL into
`BASE_URL`, and set it as the Telnyx TeXML webhook. The free URL changes on each
restart (a paid static domain avoids that). Good for a demo, not for production.

**Cloudflare Tunnel (recommended for real use).** A stable hostname, free, no
inbound ports opened — `cloudflared` dials *out* to Cloudflare, so Telnyx reaches
you through Cloudflare's edge. With the compose file:

```bash
# 1. Cloudflare Zero Trust dashboard → Networks → Tunnels → create a tunnel
# 2. Add a Public Hostname: ivr.yourdomain.com  →  service http://ivr:8000
# 3. Copy the tunnel token into .env as TUNNEL_TOKEN
docker compose --profile tunnel up -d
# BASE_URL=https://ivr.yourdomain.com  and point the Telnyx TeXML webhook there
```

Only the small HTTP webhook goes through Cloudflare — the call audio (SIP/RTP)
never does, so the tunnel's HTTP-only nature is fine here. Add a WAF **skip rule
for `/texml/*`** so bot protection doesn't block Telnyx (see Production notes).

**Tailscale Funnel / VPN (if you already run one).** Tailscale Funnel publicly
exposes a service on your tailnet over HTTPS (`tailscale funnel 8000`) — same idea
as Cloudflare Tunnel, no ports opened. Or, if your box has a **public static IP**,
put a reverse proxy (Caddy/nginx) with a Let's Encrypt cert in front of port 8000
and forward 443 → the app. Whatever the resulting public HTTPS URL is goes into
`BASE_URL` and the Telnyx webhook.

> Whichever you pick, the value in `BASE_URL` **must** match the host Telnyx calls —
> the app builds every callback URL (`/handle-input`, `/after-dial`, `/recording`)
> from it.

## Test it

```bash
curl http://localhost:8000/health

# Menu (caller +1555…):
curl -X POST http://localhost:8000/texml/menu -d "From=+15551234567"

# Press 1 -> rings the SALES_AGENTS ring group:
curl -X POST http://localhost:8000/texml/handle-input -d "Digits=1"

# Nobody answered -> voicemail:
curl -X POST "http://localhost:8000/texml/after-dial?dept=sales" -d "DialCallStatus=no-answer"
```

Each prints the exact TeXML Telnyx will act on. Watch the app logs to see the
caller number, CRM match, menu choice, dial result, and voicemail URL.

## Telnyx setup

1. **TeXML Application** (Voice → TeXML): webhook = `https://<BASE_URL>/texml/menu`.
   Assign your phone number to it.
2. **SIP credentials** for each agent (Voice → SIP Connections / Credentials).
   Enable **Receive SIP URI Calls: From anyone**. Each agent registers a softphone
   (Zoiper / Linphone / desk phone) to `sip.telnyx.com` with those credentials.
3. Put the agents' SIP URIs in `.env` (`SALES_AGENTS=sip:agent1@sip.telnyx.com,…`).

You'll have **1 TeXML Application** (HTTP webhooks — your app) and **2 SIP
credentials** (your agents). The app is not a SIP connection; it never speaks SIP.

## HubSpot caller matching (optional)

Set `HUBSPOT_TOKEN` (Private App token, CRM read scope). Each call searches
Contacts by `phone`/`mobilephone`; a match is logged and greeted by name. Store
numbers in E.164 so they match what Telnyx sends. If unset or HubSpot is down,
the call proceeds normally — the CRM is never on the critical path.

## Voicemail — what to put, and when callers hear it

Callers reach voicemail in three situations, each with its **own editable prompt
template** (change the `<Say>` wording, or swap it for a `<Play>` of a recorded
MP3 — the next call uses it, no restart):

| When | Template | Default prompt |
| --- | --- | --- |
| Agents rang but nobody answered (after failover) | `texml/voicemail.xml.j2` | "Sorry, no one is available in {dept}. Leave a message after the beep…" |
| A menu option has **no agents configured** | `texml/unavailable.xml.j2` | "Sorry, {dept} is not staffed right now. Leave a message…" |
| **After business hours** with no on-call agents | `texml/after_hours.xml.j2` | "Our office is closed. Our hours are … Leave a message…" |

A fourth, separate one is the **failover bin** (`texml/busy-voicemail-bin.example.xml`)
— a static voicemail you paste into a Telnyx TeXML Bin and set as the Application's
Failover URL, so callers can leave a message even when this app is down (see
*Staying up* below).

Controls:
- `ENABLE_VOICEMAIL=true|false` — turn voicemail on/off (off = polite hang-up).
- `VOICEMAIL_MAX_SECONDS=120` — max message length (billed as plain telephony time).
- To use a **recorded greeting** instead of TTS, replace the `<Say>` line in a
  template with `<Play>https://your-cdn/vm-greeting.mp3</Play>`.

Where messages go: Telnyx records and **stores every voicemail on Telnyx** (Portal
→ Reporting → Recordings, or via API). To also keep them locally with transcripts,
see the next section.

## Recording, transcription & storage (no database)

- **Disclosure** — `ANNOUNCE_RECORDING=true` plays "this call may be recorded for
  quality and monitoring purposes" at the start of every call (edit wording in
  `texml/menu.xml.j2`). Required for monitoring/QA recording in all-party-consent
  jurisdictions; make sure agents are aware too.
- **Recording** — voicemail is always recorded. `RECORD_CALLS=true` also records the
  live agent conversation. Telnyx stores every recording (Portal → Reporting →
  Recordings, or via API) regardless of what's below.
- **Local storage + transcription** — set `SAVE_RECORDINGS=true` to download each
  recording into `recordings/` as paired files; add `TRANSCRIBE_ENABLED=true`
  (and `pip install -r requirements-transcribe.txt`) to also write a transcript.
  Everything runs in a background thread — no caller impact.

  ```
  recordings/
    2026/05/30/
      18-22-05_sales_+14155551234_<callsid>.mp3   audio
      18-22-05_sales_+14155551234_<callsid>.txt   transcript
      18-22-05_sales_+14155551234_<callsid>.json  metadata
    index.csv                                     one row per call (paths relative)
  ```

  Files are organized by `year/month/day`; within a day the audio, transcript, and
  metadata share one base filename, so they're paired with no database. The
  top-level `index.csv` lists every call across all days with its relative path —
  search it with grep/Excel. The `recordings/` folder holds customer PII (gitignored).

### How transcription works + resource use

Transcription is **local faster-whisper** — no API, no per-minute cost, audio
never leaves your server (good for the PII you're recording). It runs **after the
call ends**, in a background thread, so it never delays the caller; it only
affects how soon the `.txt` appears.

Pick the model with `WHISPER_MODEL`. Costs are per voicemail on CPU (int8):

| Model | Disk (one-time download) | RAM while loaded | Speed (30s clip) | Accuracy |
| --- | --- | --- | --- | --- |
| `tiny`  | ~75 MB  | ~0.5–1 GB | ~2–4 s   | OK |
| `base`  | ~140 MB | ~1 GB     | ~4–8 s   | Good ← recommended |
| `small` | ~460 MB | ~2 GB     | ~8–15 s  | Better |
| `medium`| ~1.5 GB | ~5 GB     | slow on CPU | Great (use only with a GPU) |

Practical notes:
- The model loads into RAM on first use and **stays resident** — so the app's
  memory rises by roughly the "RAM while loaded" figure once transcription starts.
- A transcription pins **~1 CPU core** for its duration; jobs run one at a time, so
  simultaneous voicemails queue (fine at low volume). On a **2 vCPU / 2–4 GB** box,
  `base` leaves a core free for webhooks.
- First run downloads the model once (cached afterward). Bake it into the image
  with `docker build --build-arg INSTALL_TRANSCRIBE=true`.
- No spare CPU/RAM? Set `TRANSCRIBE_ENABLED=false` and use a cloud STT (e.g. Groq's
  free Whisper tier) on the stored audio instead — same files, offloaded compute.

## Staying up: monitoring + failover

Two independent layers — one tells you it's down, the other keeps calls working.

**Backup so calls survive an outage (most important).** Telnyx TeXML Applications
have a **Failover URL**: if your primary webhook doesn't return 200 (app crashed,
Cloudflare tunnel dropped), Telnyx fetches instructions from the failover instead.
Point it at a **TeXML Bin** (hosted on Telnyx, independent of your infra) that
forwards to a backup line — see `texml/failover.example.xml`. Callers get answered
instead of dead air, even with your server completely down.

**Get notified.** Pick either or both:

- *External uptime monitor* (recommended) — point UptimeRobot / Better Stack /
  Healthchecks at your **public** `https://<BASE_URL>/health`. Because it traverses
  the same Cloudflare path Telnyx uses, it catches a dropped tunnel too. `/health`
  returns 503 (not just 200) if the app can't actually serve calls, so it's a true
  readiness signal. Alerts go to SMS/email/Slack.
- *Heartbeat / dead-man's switch* — set `HEARTBEAT_URL` (a healthchecks.io ping
  URL). The app pings it every `HEARTBEAT_INTERVAL_SECONDS`; if the process dies,
  pings stop and you're alerted. Catches process death even without an external monitor.
- *Cloudflare tunnel health* — Cloudflare Zero Trust can notify you when a tunnel
  goes unhealthy.

Run with `restart: unless-stopped` (already in `docker-compose.yml`) so the app
and tunnel self-heal after a crash.

## Production notes

- Set `VERIFY_SIGNATURES=true` and a real `TELNYX_PUBLIC_KEY` to reject spoofed
  webhooks (Ed25519, with replay protection).
- Behind Cloudflare, add a WAF **skip rule for `/texml/*`** so bot protection
  doesn't block Telnyx, and don't enable any body-transforming feature (it would
  break signature verification).
- Voicemail hold/record time is billed at the plain telephony rate — keep
  `VOICEMAIL_MAX_SECONDS` and `DIAL_TIMEOUT` sane.
