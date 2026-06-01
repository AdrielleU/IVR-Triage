# Go-Live Checklist

App is live at **https://ivr.aiivar.com** · numbers: TMR `+18775541997`,
Ravenswood `+16029221925`, direct line `+14804051088`.

---

## 1. Telnyx — SIP credentials (your softphones)
- [ ] **Voice → SIP Connections → Create**, type **Credentials**
  - [ ] Username = `AdrielleLinphone`, set a password
  - [ ] **Inbound → "Receive SIP URI Calls: From anyone" = ON**  ← required, or the app can't ring you
  - [ ] **Outbound tab →** attach an **Outbound Voice Profile** + set **Caller ID Override = +18775541997**
- [ ] Repeat for **Citadel** (username `CitadelLinphone`, same settings) — Ravenswood support/billing rings this
- [ ] **No phone numbers assigned to the SIP connections** (numbers go on the TeXML app, not here)

## 2. Telnyx — TeXML Application (the webhook)
- [ ] **Voice → Programmable Voice → TeXML Applications → Create**
  - [ ] Voice webhook URL = `https://ivr.aiivar.com/texml/menu`  (method POST)
  - [ ] (optional) Failover URL = a TeXML **Bin** from `texml/busy-voicemail-bin.example.xml`
- [ ] **Numbers → My Numbers →** assign **all three numbers** to this TeXML app

## 3. Linphone (register the softphones)
- [ ] Account: user `AdrielleLinphone`, domain `sip.telnyx.com:5061`, transport **TLS**, Register ON
- [ ] Citadel does the same with `CitadelLinphone`
- [ ] Status shows **Registered (green)**

## 4. AI Assistants (press 4 / busy press 1) — optional but recorded greetings promise it
- [ ] Mission Control → **AI, Storage & Compute → AI Assistants → Create** (one for TMR, one for RVB)
  - [ ] Set greeting + instructions (paste your common sales FAQs into Instructions)
  - [ ] **Do NOT assign it to a phone number** — the app connects it by id
  - [ ] Copy each `assistant-…` id
- [ ] Add a **Webhook tool** to each assistant for lead capture:
  - URL `https://ivr.aiivar.com/leads` · POST · header `X-Leads-Token: <LEADS_TOKEN from .env>`
  - body: company, caller_name, caller_number, intent, issue_summary, wants_callback, callback_number
  - instruction: "before ending, call log_lead with the caller's details"
- [ ] **Send the two `assistant-…` ids to Claude** → wired into `companies.csv`, press-4 + busy-press-1 go live

## 5. Dashboard auth — finish the lock (Cloudflare Access already created ✅)
- [ ] In the Access application's **Overview**, copy the **Application Audience (AUD) tag**
- [ ] Get your **team domain** (e.g. `yourteam.cloudflareaccess.com`)
- [ ] **Send both to Claude** → sets `CF_ACCESS_AUD` + `CF_ACCESS_TEAM_DOMAIN` so the app verifies the
      signed Access JWT (until then the dashboard trusts the plain email header — spoofable)
- [ ] Access policy emails match `data/dashboard_access.csv`: `adrielle@techmanager.ai`,
      `citadel@ravenswoodbilling.com`

---

## Smoke test (do these in order)
1. [ ] `curl https://ivr.aiivar.com/health` → **200**
2. [ ] Call **+14804051088** from a phone → Linphone rings immediately (no menu) → direct line ✅
3. [ ] Call **+18775541997** → hear the TMR menu → press 1 → Linphone rings ✅
4. [ ] Call **+16029221925** → hear the RVB menu → press 2 → Citadel's Linphone rings → don't answer →
       rolls to cell `+16022847550` → then voicemail ✅
5. [ ] Dial out from Linphone → shows as **+18775541997** ✅
6. [ ] Open `https://ivr.aiivar.com/dashboard` (incognito) → Cloudflare login → see voicemails/leads ✅
7. [ ] After a real call, check the log: `podman logs ivr | grep -i signature` → **no** "Invalid signature"

> ⚠️ Signature verify is ON. If a real call fails with `403 Invalid Telnyx signature` in the logs,
> the public key is wrong for this account → tell Claude to flip `VERIFY_SIGNATURES=false`, call,
> then fix the key and flip back.

---

## State (already done)
- ✅ Live recording → local download → auto-delete from Telnyx · free local transcription (bounded queue)
- ✅ Lead capture `/leads` (token-guarded) · dashboard (theme + company filter + search)
- ✅ Signature verify ON · secrets gitignored · `TELNYX_API_KEY` set · business hours Mon–Fri 6–6 PT
- ✅ Direct line rings 24/7 → straight to beep · `*` repeats menu/busy prompt

## Still needs Claude (after you gather the values above)
- The two **AI assistant ids** → press-4 / busy press-1
- The Cloudflare **AUD + team domain** → lock dashboard JWT verification
