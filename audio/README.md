# Pre-recorded prompt clips

Drop audio files here to play **recorded prompts** to callers instead of the
robotic TTS voice. Files in this folder are served by the app at
`/<BASE_URL>/audio/...`, so Telnyx fetches them over the same public URL it uses
for webhooks. This folder is bind-mounted (`docker-compose.yml`), so adding or
swapping a clip takes effect on the **next call** — no rebuild, no restart.

## Supported prompts

Each of these prompts plays a clip if one is found, else falls back to TTS:

| Prompt        | When the caller hears it                                   |
|---------------|------------------------------------------------------------|
| `menu`        | Main greeting + menu options (the entry prompt)            |
| `voicemail`   | Agents rang but nobody answered — "leave a message"        |
| `unavailable` | Option chosen but no agents configured — "all agents busy" (falls back to the `voicemail` clip if unset) |
| `after_hours` | Office-closed greeting (record a generic one)             |
| `invalid`     | Caller pressed a key with no option                       |
| `goodbye`     | Call wrap-up                                               |

## How a clip is chosen (first hit wins)

1. **Explicit config** — a `<prompt>_audio_url` value, set per-company in
   `data/companies.csv` or globally via the `<PROMPT>_AUDIO_URL` env var. The
   value may be a full `https://…` URL (host anywhere) **or** a bare filename
   served from this folder (e.g. `MENU_AUDIO_URL=menu.mp3`).
2. **Convention (zero-config)** — just drop a file named after the prompt:
   - `audio/<prompt>.mp3` — used for every tenant
   - `audio/<company-key>/<prompt>.mp3` — per-tenant override, where
     `<company-key>` is the dialed number's last 10 digits (matches
     `data/companies.csv`)

Supported extensions, in preference order: `.mp3`, `.wav`. (Telnyx `<Play>` is
unreliable with OGG — stick to MP3 or WAV.)

## Examples

```
audio/menu.mp3                 # default greeting for all numbers
audio/voicemail.mp3            # default voicemail prompt
audio/4155550123/menu.mp3      # custom greeting just for +1 415 555 0123
```

Audio files here are gitignored by default (see `.gitignore`) — they're treated
as deployment assets, not source. Telephony tip: 8 kHz / mono is plenty for a
phone call and keeps files small.
