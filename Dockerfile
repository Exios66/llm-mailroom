# llm-mailroom producer — FastAPI on :7860 (Hugging Face Spaces convention).
# The-Mailroom REVIEW resolve points MAILROOM_PIPELINE_URL here and sends
# MAILROOM_PIPELINE_TOKEN = MAILROOM_API_TOKEN. Off-loopback bind refuses
# to start without that bearer token (audit L-2).
#
# Best-practice baseline: multi-stage build, non-root runtime user,
# HEALTHCHECK, pinned slim base, no secrets baked into the image.

FROM python:3.11-slim-bookworm AS builder

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir --prefix=/install .


FROM python:3.11-slim-bookworm AS runtime

WORKDIR /app

# Runtime needs ca-certificates only (git was build-time for dojo pin).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 mailroom \
    && useradd --system --uid 10001 --gid mailroom --home-dir /app --shell /usr/sbin/nologin mailroom \
    && mkdir -p /data \
    && chown -R mailroom:mailroom /app /data

COPY --from=builder /install /usr/local
COPY --chown=mailroom:mailroom pyproject.toml README.md ./
COPY --chown=mailroom:mailroom src ./src

ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1
ENV MAILROOM_API_HOST=0.0.0.0
ENV MAILROOM_API_PORT=7860
ENV MAILROOM_BASE_DIR=/data
ENV MAILROOM_EMBED_WATCHER=1

USER mailroom

EXPOSE 7860

# Prefer platform PORT (Railway/Fly/Render) over the Spaces image default.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import os,urllib.request; p=os.environ.get('PORT') or os.environ.get('MAILROOM_API_PORT','7860'); urllib.request.urlopen(f'http://127.0.0.1:{p}/health', timeout=3)"

CMD ["python", "-m", "api.main"]
