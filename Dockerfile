# llm-mailroom producer — FastAPI on :7860 (Hugging Face Spaces convention).
# The-Mailroom REVIEW resolve points MAILROOM_PIPELINE_URL here and sends
# MAILROOM_PIPELINE_TOKEN = MAILROOM_API_TOKEN. Off-loopback bind refuses
# to start without that bearer token (audit L-2).
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir . \
    && mkdir -p /data

ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1
ENV MAILROOM_API_HOST=0.0.0.0
ENV MAILROOM_API_PORT=7860
ENV MAILROOM_BASE_DIR=/data
ENV MAILROOM_EMBED_WATCHER=1

EXPOSE 7860

CMD ["python", "-m", "api.main"]
