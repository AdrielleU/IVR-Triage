# Telnyx Phone IVR (cost-optimized, optional AI handoff)

A small FastAPI app that serves a **TeXML** IVR for a Telnyx number. Callers hear
a menu (1 sales / 2 support / 3 billing / 0 operator), get rung through to your
agents (SIP softphones or phone numbers), and land in **voicemail** if nobody
answers. Human routing rides **plain telephony minutes** (~$0.002/min/leg + SIP
trunk fee), not AI minutes. Inbound callers are matched against a local
**`data/contacts.csv`** by phone so they're logged and greeted by name — no
database, no API on the call path. (Auto-syncing that CSV from a CRM like
HubSpot is a planned premium add-on; see *Caller matching* below.)

Optionally, set `AI_ASSISTANT_ID` to add a **"press 4" handoff to a Telnyx AI
Assistant**. It uses `<Connect><AIAssistant>`, which runs the assistant on the
**same call leg** — so it adds only the ~$0.05/min AI minute, with **no second
telephony leg** (see *AI Assistant handoff* below).

All the call XML lives in **editable templates** under `texml/` — change a
greeting, menu, or voicemail prompt and the next call uses it, no restart.

## Quick deploy (Docker + Cloudflare Tunnel)

Get a working phone IVR with a public HTTPS endpoint and **no open ports** in a
few minutes. You need a Cloudflare account with a domain, and a Telnyx number.

**1. Create a Cloudflare Tunnel and copy its token.**
In the [Cloudflare Zero Trust dashboard](https://one.dash.cloudflare.com/) →
**Networks → Tunnels → Create a tunnel** → connector **Cloudflared**. Cloudflare
then shows an install command like `cloudflared service install eyJ...` — you
only need the long **`eyJ...` token** at the end (skip running the command; the
compose file runs cloudflared for you). Then under **Public Hostname → Add**:

| Field      | Value                                   |
|------------|-----------------------------------------|
| Subdomain  | e.g. `ivr` → `ivr.yourdomain.com`       |
| Type       | `HTTP`                                  |
| URL        | `ivr:8000`  ← the compose service name  |

**2. Configure `.env`.**
```bash
cp .env.example .env
```
```ini
BASE_URL=https://ivr.yourdomain.com     # the public hostname from step 1
TUNNEL_TOKEN=eyJ...                      # the token from step 1
VERIFY_SIGNATURES=true                   # reject spoofed webhooks in production
TELNYX_API_KEY=...                       # from the Telnyx portal
TELNYX_PUBLIC_KEY=...                    # for signature verification
SALES_AGENTS=sip:jane@sip.telnyx.com     # who each menu option rings
SUPPORT_AGENTS=sip:bob@sip.telnyx.com    # (comma-separate for multiple)
BILLING_AGENTS=+15551234567              # SIP URI or PSTN number
OPERATOR_AGENTS=sip:ops@sip.telnyx.com
```

**3. Start the app + tunnel.**
```bash
# pull the published image instead of building (optional):
#   docker pull adrielleu/ivr-triage:latest
docker compose --profile tunnel up -d --build
docker compose logs -f tunnel    # wait for "Registered tunnel connection" x4
curl https://ivr.yourdomain.com/health   # expect HTTP 200
```

**4. Point Telnyx at it.**
In the Telnyx portal create a **TeXML Application** with the voice webhook set to
`https://ivr.yourdomain.com/texml/menu`, then assign your number to it. Call the
number — you should hear the menu.

> The tunnel makes the app outbound-only (Cloudflare terminates TLS at the edge;
> the hop to your container stays internal), so you can remove the `ports:` block
> in `docker-compose.yml` for a tunnel-only deploy. No Caddy/nginx needed.

> **Podman on SELinux (RHEL/Fedora)?** Bind mounts need an SELinux relabel or the
> container hits `Permission denied` on `data/`. Add `:z` to the volume lines in
> `docker-compose.yml` (e.g. `./data:/app/data:ro,z`). For a one-off `podman run`
> (not compose), also add `--userns=keep-id` so the container's uid can read your
> files. Both are **podman-only concerns** — on plain Docker `:z` is a harmless
> no-op and `--userns=keep-id` is not a valid flag.

See [`.env.example`](.env.example) for the full list of settings (caller
matching, business hours, recording, the optional AI handoff).

## How a call flows

```
Caller dials your number
      │
      ▼
Telnyx ──webhook──► /texml/menu        (business-hours check, contacts.csv lookup, greeting + DTMF)
      │  caller presses a key
      ▼
Telnyx ──webhook──► /texml/handle-input   (rings that department's agents via <Dial>)
      │  no one answers (or none configured)
      ▼
Telnyx ──webhook──► /texml/after-dial  ──► voicemail (<Record>) ──► /texml/recording (URL logged)

      (optional) caller presses 4
      ▼
/texml/handle-input ──► <Connect><AIAssistant>   (assistant on the SAME leg; no <Dial>, no extra leg/fee)
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
app/services/routing.py  # data/routing.csv roster -> ring stages per department
app/services/hubspot.py  # HubSpot API (used by sync + optional live fallback)
scripts/sync_hubspot.py  # download HubSpot contacts -> data/contacts.csv
texml/                   # EDITABLE call XML (Jinja2, auto-reloaded)
  menu / transfer / voicemail / after_hours / goodbye / invalid / unavailable
  connect-ai             # optional <Connect><AIAssistant> handoff (same leg)
data/                    # EDITABLE operational data (mtime-cached)
  contacts.csv  hours.csv  holidays.csv
Dockerfile  docker-compose.yml  requirements.txt  .env.example
```

## Editable data files (`data/`)

No code or restart needed — edit and the next call picks it up.

- **`contacts.csv`** — `phone,name,company,tier`. This is the **source of truth**
  for caller matching: inbound numbers are looked up here (instant, in-memory, no
  API). Drop in a CSV exported from anywhere — your CRM, a spreadsheet, etc.
  Matching ignores formatting (compares the last 10 digits). *(Auto-refreshing
  this file from HubSpot is a planned premium feature.)*
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
number,name,menu_audio_url,ai_assistant_id,sales_agents,support_agents,billing_agents,operator_agents,sales_fallback,...
+18005550001,Acme Inc,,,sip:acme1@sip.telnyx.com;sip:acme2@sip.telnyx.com,sip:acme1@sip.telnyx.com,+1...,+1...,+1...
+18005550002,Globex,https://cdn/globex.mp3,assistant-776d…,sip:globex1@sip.telnyx.com,...
```

Each company gets its own greeting (its `name` in the TTS prompt, or its own
`menu_audio_url` recording), an optional per-company `ai_assistant_id` (enables
that company's "press 4" AI handoff), and its own per-department agents/failover.
Multiple
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

**The agent sees who's calling.** When the caller matches a contact, the agent's
softphone shows **`Sales - Jane Doe`** (department + name) as the caller-ID display
name, while the caller's own number stays put for callback. No match → just the
department (`Sales`). Works on the SIP/Linphone leg; a PSTN-fallback carrier may
override the name. (Telnyx disallows `:` in this field, so the separator is `-`.)

Where those destinations come from is resolved **first-hit-wins**:
`data/routing.csv` (the agent roster, below) → `data/companies.csv` columns →
the `*_AGENTS` / `*_FALLBACK` env vars. So you can adopt the roster gradually —
departments without a routing row keep using your existing config.

## Direct lines (no IVR menu)

Some numbers shouldn't play a menu at all — a person's direct line should just
**ring them**. Give a number a **`direct`** ring chain and it skips the menu
entirely: the call plays that number's greeting, then auto-rings the chain
(SIP → personal line) and lands in voicemail if nobody answers — exactly the
same failover as a department, just with no keypress.

A number **without** a `direct` chain still shows the normal menu, so one
deployment mixes both: menu lines and direct lines side by side.

```csv
# data/routing.csv — "direct" department = a direct line for that number
company,department,name,destination,extension,priority,active
4155559999,direct,Bob (SIP),sip:bob@sip.telnyx.com,,1,true
4155559999,direct,Bob (personal cell),+14155551212,,2,true
```

That rings Bob's softphone first, fails over to his cell, then voicemail. Add a
greeting with `audio/4155559999/menu.mp3` (or a `menu_audio_url`); without one it
says a short "Please hold while we connect you." A **single** direct-line
deployment can skip routing.csv and just set `DIRECT_AGENTS` / `DIRECT_FALLBACK`
in `.env`.

A `direct` line must list at least one destination — that non-empty chain is what
flags the number as "direct" (no chain → it just shows the normal menu). A
**ring-nobody, straight-to-voicemail** line isn't supported by an empty chain
today; it would need a small follow-up (a `voicemail` sentinel destination).

## Agent roster (`data/routing.csv`) — optional, recommended

Instead of packing agents into `companies.csv` cells, keep a clean **one-row-per-
person** roster. Copy `data/routing.example.csv` → `data/routing.csv`:

```csv
company,department,name,destination,extension,priority,active
,sales,Alice,sip:alice@sip.telnyx.com,101,1,true
,sales,Bob,sip:bob@sip.telnyx.com,102,1,true
,sales,Sales cell (backup),+14155550102,,2,true
,support,Carol,sip:carol@sip.telnyx.com,201,1,true
,support,AI Support Bot,assistant-776d0d6f,,2,true
18005550001,sales,Acme Alice,sip:acme-alice@sip.telnyx.com,201,1,true
```

- **`company`** — blank = default/single-tenant; or the dialed number for a
  specific tenant (matched on the last 10 digits).
- **`department`** — `sales` / `support` / `billing` / `operator` / `after_hours`.
- **`destination`** — a SIP URI, a PSTN number, **or a Telnyx AI Assistant id**
  (`assistant-…`). Type is inferred from the value.
- **`priority`** — ring order: same number = ring together; higher = later
  fail-over stage. (Above: Sales rings Alice + Bob, then the cell.)
- **`extension`** — a label for now (reserved for future dial-by-extension).
- **`active`** — `false` benches someone without deleting the row.

**AI assistants are just another destination.** A row whose `destination` is an
`assistant-…` id is handed the call via `<Connect><AIAssistant>` on the same leg
(no extra telephony leg). So Support above rings Carol first and, if she doesn't
answer, **fails over to an AI assistant** before voicemail — and each
company/department can point at a *different* assistant. (This is separate from
the global "press 4" handoff; both can coexist.) Give an assistant its own
`priority` — it's a terminal stage, not something to ring alongside a human.

`data/routing.csv` is gitignored (your internal numbers); `routing.example.csv`
is the template. Edits apply on the next call — no restart.

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

## AI Assistant handoff (optional)

Add a "press 4 to speak with our virtual assistant" option that hands the caller
to a **Telnyx AI Assistant** — without ever getting billed for two call legs.

**Why it's cheap (the billing point).** Telnyx bills *per leg, per minute*, and
**every leg alive at the same time meters separately**. A `<Dial>` to an AI
behind a phone number would open a *second* concurrent leg (two telephony meters)
— the "charged twice" trap, and it would need a separate DID for the AI. This app
instead uses `<Connect><AIAssistant>`, which attaches the assistant to the
**existing** leg:

```xml
<Connect>
  <AIAssistant id="assistant-…"/>
</Connect>
```

So during the AI conversation you pay just the one inbound leg (~$0.002/min + SIP
trunk fee) **+ ~$0.05/min** for the AI — no extra leg.

**Setup:**
1. In Mission Control → **AI Assistants**, create an assistant. Configure its
   **voice, greeting, system instructions, and its transfer-to-human tool** there
   — all of that lives on the assistant, not in this app's XML. Copy its id
   (`assistant-…`).
2. Set `AI_ASSISTANT_ID=assistant-…` in `.env` (or the per-company
   `ai_assistant_id` column in `companies.csv`). Optionally set
   `AI_INTRO_AUDIO_URL` to play a recorded "connecting you…" clip instead of TTS.
3. That's it — the menu now offers "press 4". With the id unset, the option
   doesn't appear and behavior is unchanged.

**Escalating to a human** is the assistant's own job (its transfer tool), kept
out of this app. If that escalation uses a SIP-REFER transfer, that's the one
place Telnyx's **$0.10 REFER surcharge** can apply — only when it actually hands
off. Don't wrap the assistant in a `<Dial>`; that reintroduces the double-leg.

> **Note on the $0.10 fee:** it applies to the Call Control **SIP-REFER**
> `transfer` command, *not* to the TeXML `<Dial>` verb this app uses for normal
> IVR→agent routing. A routine "press 1 → ring sales" handoff is **not**
> surcharged — you just pay the (cheap) per-minute legs. Confirm on your first
> invoice.

## Caller matching

Caller matching reads from **`data/contacts.csv`** (`phone,name,company,tier`) —
loaded into memory and re-read whenever the file changes, so there's no database
and nothing on the call's critical path. Export contacts from your CRM or a
spreadsheet and drop the file in; the next call uses it. Numbers are matched on
the last 10 digits, so formatting doesn't matter.

> **Premium (planned): CRM auto-sync.** Keeping `contacts.csv` in lockstep with a
> live CRM such as **HubSpot** — scheduled pulls, a live API fallback for misses —
> is a premium feature we may add. The plumbing exists behind `HUBSPOT_TOKEN`
> (`app/services/hubspot.py`, `scripts/sync_hubspot.py`) but is not a supported
> part of the core, CSV-driven product. Even when enabled, the CSV stays the
> source of truth and the CRM is never on the critical path.

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
- To use a **recorded voicemail prompt** instead of TTS, drop an
  `audio/voicemail.mp3` clip — see *Recorded prompts* below.

Where messages go: Telnyx records and **stores every voicemail on Telnyx** (Portal
→ Reporting → Recordings, or via API). To also keep them locally with transcripts,
see the next section.

## Recorded prompts (play audio instead of TTS)

Play your own **recorded clips** to callers instead of the synthesized voice — a
branded greeting, a human-recorded voicemail prompt, etc. Supported for the
**menu, voicemail, unavailable (no-agents), after-hours, invalid-key, and goodbye**
prompts; any prompt without a clip falls back to TTS automatically. (The
`unavailable` prompt falls back to your `voicemail` clip before TTS.)

Two ways to set a clip, first hit wins:

1. **Just drop a file** in `./audio` (zero config):
   - `audio/menu.mp3` — used for every number
   - `audio/<dialed-number-last10>/menu.mp3` — per-company override
   - extensions tried: `.mp3`, `.wav` (Telnyx `<Play>` is unreliable with OGG)
2. **Point at a URL or filename** via env (`MENU_AUDIO_URL`, `VOICEMAIL_AUDIO_URL`,
   `UNAVAILABLE_AUDIO_URL`, `AFTER_HOURS_AUDIO_URL`, `INVALID_AUDIO_URL`,
   `GOODBYE_AUDIO_URL`) or the matching `data/companies.csv` column. A value is
   either a full `https://…` URL (host it anywhere) or a bare filename served
   from `./audio`.

Local files are served by the app at `https://<BASE_URL>/audio/…`, so Telnyx
fetches them over the same public URL as the webhooks — no CDN required. The
`./audio` folder is bind-mounted, so adding or swapping a clip takes effect on the
**next call**, no restart. The folder's contents are gitignored by default (treated
as deployment assets, not source). Full details in [`audio/README.md`](audio/README.md).

> Telephony tip: 8 kHz mono is plenty for phone audio and keeps files small.
> `BASE_URL` must be your real public URL (not `localhost`) for `<Play>` to resolve.

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
