import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.routers import dashboard, leads, texml

log = logging.getLogger("ivr")
TEMPLATE_DIR = Path(__file__).resolve().parent / "texml"


async def _heartbeat_loop() -> None:
    """Ping settings.heartbeat_url on an interval (dead-man's switch).

    If the process dies, pings stop and your monitor (healthchecks.io, Better
    Stack, etc.) raises an alert. Failures to ping are logged, never fatal.
    """
    while True:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.get(settings.heartbeat_url)
        except Exception as exc:  # noqa: BLE001 — a missed ping must not crash the app
            log.warning("Heartbeat ping failed: %s", exc)
        await asyncio.sleep(settings.heartbeat_interval_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_heartbeat_loop()) if settings.heartbeat_url else None
    if task:
        log.info("Heartbeat enabled -> %s every %ss", settings.heartbeat_url, settings.heartbeat_interval_seconds)
    yield
    if task:
        task.cancel()


app = FastAPI(
    title="Telnyx Phone IVR",
    version="1.1.0",
    description=("Telnyx IVR: DTMF menu, SIP ring-groups, voicemail, caller matching, "
                 "optional AI handoff, call recording + local transcription, lead capture, "
                 "and a Cloudflare-Access-gated dashboard."),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(texml.router, prefix="/texml", tags=["TeXML"])
app.include_router(leads.router, tags=["Leads"])  # POST/GET /leads — AI lead capture
app.include_router(dashboard.router, tags=["Dashboard"])  # /dashboard — Cloudflare-Access-gated viewer

# Serve pre-recorded prompt clips so Telnyx can <Play> them over BASE_URL. Bind
# audio_dir from the host (compose) to swap clips without a rebuild. Created if
# absent so the mount never fails on a fresh deploy. NOTE: this serves only
# audio_dir — never point it at recordings_dir (caller PII).
_audio_dir = Path(settings.audio_dir)
_audio_dir.mkdir(parents=True, exist_ok=True)
app.mount("/audio", StaticFiles(directory=str(_audio_dir)), name="audio")


@app.get("/")
async def root():
    return {"service": "Telnyx Phone IVR", "status": "online", "health": "/health", "menu": "/texml/menu"}


@app.get("/health")
async def health():
    """Readiness check. Returns 503 if the app can't actually serve calls, so an
    external monitor pinging this URL (through Cloudflare) catches real outages —
    the same path Telnyx uses to reach the webhook."""
    templates_ok = TEMPLATE_DIR.is_dir() and (TEMPLATE_DIR / "menu.xml.j2").is_file()
    if not templates_ok:
        return JSONResponse(status_code=503, content={"status": "unhealthy", "templates": False})
    return {"status": "ok", "templates": True, "debug_mode": settings.debug}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=settings.debug)
