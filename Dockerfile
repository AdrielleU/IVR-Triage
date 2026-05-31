FROM python:3.11-slim

# Unbuffered stdout so call logs show up live; no .pyc clutter.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install deps first for better layer caching. Set INSTALL_TRANSCRIBE=true at build
# time to also bake in faster-whisper for local transcription:
#   docker build --build-arg INSTALL_TRANSCRIBE=true -t telnyx-ivr .
ARG INSTALL_TRANSCRIBE=false
COPY requirements.txt requirements-transcribe.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
 && if [ "$INSTALL_TRANSCRIBE" = "true" ]; then pip install --no-cache-dir -r requirements-transcribe.txt; fi

# App code + editable templates/data + sync script.
COPY main.py .
COPY app/ ./app/
COPY texml/ ./texml/
COPY data/ ./data/
COPY scripts/ ./scripts/

# Non-root user. Create the recordings dir up front and hand it to that user so the
# app can write (and a named volume inherits the right ownership).
RUN useradd --create-home --uid 1000 appuser \
 && mkdir -p /app/recordings \
 && chown -R appuser /app/recordings
USER appuser

EXPOSE 8000

# Container is unhealthy if /health doesn't return 200 (it returns 503 when the app
# can't serve calls), so the orchestrator can restart it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Honor $PORT if the host sets one (Render/Railway), else default to 8000.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
