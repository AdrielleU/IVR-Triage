"""Match inbound callers to HubSpot contacts by phone number.

The IVR must never break because HubSpot is slow or unreachable, so every path
here degrades to None and the call continues with the normal menu.
"""

import logging

import httpx

from app.config import settings

log = logging.getLogger("ivr")

SEARCH_URL = "https://api.hubapi.com/crm/v3/objects/contacts/search"


async def lookup_caller(phone: str) -> dict | None:
    """Return the matched HubSpot contact for `phone`, or None.

    `phone` is the caller's number from Telnyx (`From`), e.g. "+14155551234".
    For matches to work, store numbers in HubSpot in the SAME format Telnyx
    sends (E.164 is best). Returns None if HubSpot isn't configured, no contact
    matches, or anything goes wrong — the caller is never blocked on the CRM.
    """
    if not settings.hubspot_token or not phone:
        return None

    # filterGroups are OR'd: match either the phone or mobilephone property.
    payload = {
        "filterGroups": [
            {"filters": [{"propertyName": "phone", "operator": "EQ", "value": phone}]},
            {"filters": [{"propertyName": "mobilephone", "operator": "EQ", "value": phone}]},
        ],
        "properties": ["firstname", "lastname", "company", "hs_lead_status", "hubspot_owner_id"],
        "limit": 1,
    }

    try:
        # Short timeout: a caller is waiting on the line. Fail fast, route anyway.
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.post(
                SEARCH_URL,
                headers={"Authorization": f"Bearer {settings.hubspot_token}"},
                json=payload,
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
    except Exception as exc:  # noqa: BLE001 — never let a CRM error drop the call
        log.warning("HubSpot lookup failed for %s: %s", phone, exc)
        return None

    if not results:
        return None

    props = results[0].get("properties", {})
    name = " ".join(filter(None, [props.get("firstname"), props.get("lastname")])) or None
    return {
        "id": results[0].get("id"),
        "name": name,
        "company": props.get("company"),
        "lead_status": props.get("hs_lead_status"),
        "owner_id": props.get("hubspot_owner_id"),
    }
