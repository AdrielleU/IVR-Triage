from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Telnyx
    telnyx_api_key: str
    telnyx_public_key: str  # Mission Control → Keys & Credentials → Public Key

    # Verify the Ed25519 signature on inbound Telnyx webhooks. Leave False in
    # local dev (no real key); set True in production to reject spoofed requests.
    verify_signatures: bool = False

    # Caller matching. Inbound numbers are matched against a local CSV first
    # (instant, no API on the call path). data/contacts.csv is the default;
    # refresh it from HubSpot with scripts/sync_hubspot.py, or drop in a CSV
    # exported from any other source.
    data_dir: str = "data"
    hubspot_token: str | None = None          # used by the sync script (and optional live fallback)
    hubspot_live_fallback: bool = False        # if a number isn't in the CSV, try the HubSpot API too

    # IVR routing — comma-separated destinations each menu option rings. A
    # destination is either a SIP agent (sip:agent1@sip.telnyx.com) or a PSTN
    # number (+14155551234). Multiple = simultaneous ring, first to answer wins.
    # Example: SALES_AGENTS="sip:agent1@sip.telnyx.com,sip:agent2@sip.telnyx.com"
    sales_agents: str = ""
    support_agents: str = ""
    billing_agents: str = ""
    operator_agents: str = ""
    after_hours_agents: str = ""

    # Auto-forward failover: if the agents above don't answer (e.g. a SIP
    # softphone is offline), ring these PSTN backups next, then voicemail.
    sales_fallback: str = ""
    support_fallback: str = ""
    billing_fallback: str = ""
    operator_fallback: str = ""
    after_hours_fallback: str = ""

    dial_timeout: int = 20  # seconds to ring each stage before failing over

    # Voicemail: when nobody answers, record a message (TeXML <Record>).
    enable_voicemail: bool = True
    voicemail_max_seconds: int = 120

    # Recording + legal disclosure. announce_recording plays the disclosure (edited
    # in texml/menu.xml.j2) at the start of every call — required for monitoring/QA
    # recording in many places (esp. all-party-consent states). record_calls also
    # records the live agent conversation, not just voicemail.
    announce_recording: bool = False
    record_calls: bool = False

    # Recording storage + transcription (filesystem only — no database needed).
    # save_recordings downloads each Telnyx recording and stores audio + a JSON
    # sidecar + an index.csv row, all sharing one base filename. transcribe_enabled
    # additionally writes a paired .txt transcript via local faster-whisper
    # (pip install -r requirements-transcribe.txt). transcribe implies save.
    save_recordings: bool = False
    transcribe_enabled: bool = False
    whisper_model: str = "base"          # tiny | base | small | medium
    recordings_dir: str = "recordings"

    # AI Assistant (Telnyx Conversational AI), opt-in. When ai_assistant_id is set,
    # the menu offers "press 4" to hand the caller to a Telnyx AI Assistant. The
    # handoff uses TeXML <Connect><AIAssistant>, which runs the assistant ON THE
    # EXISTING call leg — it does NOT originate a new <Dial> leg, so there's no
    # second concurrent telephony leg and no $0.10/transfer fee; only the
    # ~$0.05/min AI minute stacks on the single inbound leg. The assistant's
    # voice, greeting, instructions, and its own transfer-to-human tool are
    # configured on the assistant resource (Mission Control portal or the AI
    # Assistant API) and referenced here ONLY by id. Per-company override:
    # data/companies.csv `ai_assistant_id` column.
    ai_assistant_id: str = ""
    ai_intro_audio_url: str | None = None  # optional pre-recorded "connecting you…" clip; else TTS

    # Single-tenant defaults (used when data/companies.csv is absent or the dialed
    # number isn't listed). company_name personalizes the greeting; menu_audio_url
    # plays a pre-recorded greeting instead of TTS. Per-company values in
    # companies.csv override these.
    company_name: str = ""
    menu_audio_url: str | None = None

    # Business hours. When enforce_business_hours is True, calls outside the
    # window are sent to after_hours_number (if set) or politely turned away.
    enforce_business_hours: bool = False
    business_timezone: str = "America/New_York"
    # Public holidays are computed automatically (correct floating/observed dates
    # every year) for this country/region. data/holidays.csv adds company-specific
    # closures on top. Set auto_holidays=False to use only the CSV.
    auto_holidays: bool = True
    holiday_country: str = "US"
    holiday_subdiv: str | None = None  # e.g. "CA" for California-specific holidays
    business_open_hour: int = 9       # 24h local time, inclusive
    business_close_hour: int = 17     # 24h local time, exclusive
    business_days: str = "0-4"        # Mon=0 .. Sun=6; ranges/commas e.g. "0-4" or "0,1,2,3,4"
    after_hours_number: str | None = None  # optional on-call line for closed hours

    # Outbound heartbeat (dead-man's switch). If set, the app pings this URL every
    # heartbeat_interval_seconds; when the app dies, pings stop and the monitor
    # (e.g. healthchecks.io) alerts you. Complements an external uptime monitor.
    heartbeat_url: str | None = None
    heartbeat_interval_seconds: int = 60

    # Application
    base_url: str
    debug: bool = False

    class Config:
        env_file = ".env"
        case_sensitive = False
        extra = "ignore"  # tolerate leftover/unused vars in .env instead of crashing


settings = Settings()
