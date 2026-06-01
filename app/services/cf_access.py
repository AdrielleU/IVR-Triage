"""Verify the Cloudflare Access identity on a request — the dashboard's login.

Cloudflare Access (free, up to 50 users) authenticates the user at the edge and
forwards a signed JWT in the `Cf-Access-Jwt-Assertion` header (and the email in
`Cf-Access-Authenticated-User-Email`). We MUST verify the JWT's signature — the
plain email header alone is spoofable by anyone hitting the origin directly
(through the tunnel). The JWT is RS256, signed by your team's Access keys published
at https://<team>.cloudflareaccess.com/cdn-cgi/access/certs (a JWKS).

We verify with `cryptography` (no extra dep): check signature, expiry, issuer, and
the AUD (the Access application's Audience tag). Returns the verified email or None.

Config:
  - cf_access_team_domain : e.g. "aiivar.cloudflareaccess.com"
  - cf_access_aud         : the Application Audience (AUD) tag from the Access app
If either is unset, verification is treated as DISABLED and get_verified_email()
falls back to the plain email header — convenient for local dev, NOT for prod.
"""

import base64
import json
import logging
import time

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
from cryptography.hazmat.primitives.hashes import SHA256

from app.config import settings

log = logging.getLogger("ivr")

_JWKS_CACHE: dict = {"keys": {}, "fetched_at": 0.0}
_JWKS_TTL = 3600  # refresh Cloudflare signing keys hourly


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _b64url_uint(val: str) -> int:
    return int.from_bytes(_b64url_decode(val), "big")


def _certs_url() -> str:
    domain = settings.cf_access_team_domain.strip().rstrip("/")
    if not domain.startswith("http"):
        domain = f"https://{domain}"
    return f"{domain}/cdn-cgi/access/certs"


def _load_keys(now: float) -> dict:
    """Return {kid: RSAPublicKey}, cached for _JWKS_TTL. Empty dict on failure."""
    if _JWKS_CACHE["keys"] and now - _JWKS_CACHE["fetched_at"] < _JWKS_TTL:
        return _JWKS_CACHE["keys"]
    try:
        with httpx.Client(timeout=10.0) as client:
            jwks = client.get(_certs_url()).json()
        keys = {}
        for jwk in jwks.get("keys", []):
            pub = RSAPublicNumbers(
                e=_b64url_uint(jwk["e"]), n=_b64url_uint(jwk["n"])
            ).public_key()
            keys[jwk["kid"]] = pub
        _JWKS_CACHE["keys"] = keys
        _JWKS_CACHE["fetched_at"] = now
        return keys
    except Exception as exc:  # noqa: BLE001
        log.warning("Cloudflare Access JWKS fetch failed: %s", exc)
        return _JWKS_CACHE["keys"]  # stale keys are better than none


def _verify_jwt(token: str) -> dict | None:
    """Validate an Access JWT (sig, exp, iss, aud). Return its claims or None."""
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
        header = json.loads(_b64url_decode(header_b64))
        payload = json.loads(_b64url_decode(payload_b64))
    except Exception:  # noqa: BLE001 — malformed token
        return None

    now = time.time()
    key = _load_keys(now).get(header.get("kid"))
    if key is None:
        log.warning("Access JWT: unknown signing kid")
        return None

    signed = f"{header_b64}.{payload_b64}".encode()
    try:
        key.verify(_b64url_decode(sig_b64), signed, padding.PKCS1v15(), SHA256())
    except InvalidSignature:
        log.warning("Access JWT: bad signature")
        return None

    if payload.get("exp", 0) < now:
        return None
    aud = payload.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    if settings.cf_access_aud and settings.cf_access_aud not in auds:
        log.warning("Access JWT: AUD mismatch")
        return None
    expected_iss = _certs_url().rsplit("/cdn-cgi", 1)[0]
    if not str(payload.get("iss", "")).startswith(expected_iss):
        log.warning("Access JWT: issuer mismatch")
        return None
    return payload


def get_verified_email(request) -> str | None:
    """The Cloudflare-verified login email for this request, or None.

    With cf_access_team_domain + cf_access_aud set, requires a valid Access JWT.
    If unset (local dev), falls back to the plain Cf-Access-Authenticated-User-Email
    header — DO NOT run prod without the config set, or the header is spoofable.
    """
    configured = settings.cf_access_team_domain and settings.cf_access_aud
    token = request.headers.get("cf-access-jwt-assertion", "")
    if configured:
        claims = _verify_jwt(token) if token else None
        if not claims:
            return None
        return (claims.get("email") or "").strip().lower() or None
    # Dev fallback: trust the email header (only safe behind a trusted proxy / locally).
    return (request.headers.get("cf-access-authenticated-user-email", "") or "").strip().lower() or None
