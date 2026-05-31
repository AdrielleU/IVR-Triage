"""Resolve a playable audio URL for a named prompt (menu, voicemail, …).

Prompts can play a pre-recorded clip via TeXML <Play> instead of TTS <Say>. A
clip is resolved per-prompt and per-company, first hit wins:

  1. Explicit config — companies.csv `<name>_audio_url` column, else the env
     `<NAME>_AUDIO_URL` setting. The value is either a full http(s):// URL (hosted
     anywhere) or a bare filename/relative path served from the local audio_dir.
  2. Convention — a local file at audio/<company>/<name>.<ext> then
     audio/<name>.<ext>, so you can just drop files in with no config.

Returns None when nothing is found, so the template falls back to TTS. Local
files are served by this app's /audio static mount (see main.py), so Telnyx
fetches them over the same public BASE_URL it already uses for webhooks.
"""

from pathlib import Path

from app.config import settings

# Common telephony-friendly audio containers, in preference order.
AUDIO_EXTS = (".mp3", ".wav", ".ogg")


def _is_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def _served(rel: str) -> str:
    """Public URL for a file under the local /audio mount."""
    return f"{settings.base_url.rstrip('/')}/audio/{rel.lstrip('/')}"


def prompt_audio(name: str, company: dict | None = None, co: str = "") -> str | None:
    """Playable URL for prompt `name`, or None to fall back to TTS."""
    # 1. Explicit config: per-company CSV column, else env setting.
    configured = ""
    if company is not None:
        configured = (company.get(f"{name}_audio_url") or "").strip()
    if not configured:
        configured = (getattr(settings, f"{name}_audio_url", None) or "").strip()
    if configured:
        return configured if _is_url(configured) else _served(configured)

    # 2. Convention: audio/<co>/<name>.<ext> then audio/<name>.<ext>.
    base = Path(settings.audio_dir)
    candidates = []
    if co:
        candidates += [f"{co}/{name}{ext}" for ext in AUDIO_EXTS]
    candidates += [f"{name}{ext}" for ext in AUDIO_EXTS]
    for rel in candidates:
        if (base / rel).is_file():
            return _served(rel)
    return None
