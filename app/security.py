"""Verify that inbound webhooks really came from Telnyx, and parse their form.

Telnyx signs every webhook with Ed25519. The signature is over the bytes
`{timestamp}|{raw_body}`, sent in these headers:
  - telnyx-signature-ed25519 : base64 signature
  - telnyx-timestamp         : unix seconds the signature was made

FastAPI reads a form body *before* dependencies run, so we can't grab the raw
bytes from a dependency. Instead, call `verified_form(request)` at the top of
each webhook handler: it reads the raw body first (caching it), verifies the
signature, then returns the parsed form. Verification is a no-op unless
settings.verify_signatures is True, so local dev without a real key still runs.
"""

import base64
import logging
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import HTTPException, Request

from app.config import settings

log = logging.getLogger("ivr")

# Reject signatures older than this (replay protection), in seconds.
TOLERANCE = 300


def _public_key() -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(base64.b64decode(settings.telnyx_public_key))


def _verify(headers, body: bytes) -> None:
    signature = headers.get("telnyx-signature-ed25519")
    timestamp = headers.get("telnyx-timestamp")
    if not signature or not timestamp:
        raise HTTPException(status_code=403, detail="Missing Telnyx signature headers")

    if abs(time.time() - int(timestamp)) > TOLERANCE:
        raise HTTPException(status_code=403, detail="Stale webhook timestamp")

    signed = timestamp.encode() + b"|" + body
    try:
        _public_key().verify(base64.b64decode(signature), signed)
    except (InvalidSignature, ValueError) as exc:
        log.warning("Rejected webhook with bad signature: %s", exc)
        raise HTTPException(status_code=403, detail="Invalid Telnyx signature")


async def verified_form(request: Request) -> dict:
    """Read + verify the request, returning its form fields as a dict.

    Reads the raw body first so the signature check sees the exact bytes;
    `request.form()` then reuses that cached body. Raises 403 on a bad/missing
    signature when verification is enabled.
    """
    body = await request.body()
    if settings.verify_signatures and request.method != "GET":
        _verify(request.headers, body)
    form = await request.form()
    return {key: str(value) for key, value in form.items()}
