# llm-mailroom producer — FastAPI on :7860 (Hugging Face Spaces convention).
# The-Mailroom REVIEW resolve points MAILROOM_PIPELINE_URL here and sends
# MAILROOM_PIPELINE_TOKEN = MAILROOM_API_TOKEN. Off-loopback bind refuses
# to start without that bearer token (audit L-2).
#
# Best-practice baseline: multi-stage build, non-root runtime user,
# HEALTHCHECK, pinned slim base, no secrets baked into the image.
#
# Deployment matrix (DMR-076):
#   Mode B lean (OpenRouter only): docker build --build-arg ML_BUILD_NONE=1 --build-arg PIP_EXTRAS= .
#   Mode B / Mixed default:        docker build -t mailroom:latest .            (PIP_EXTRAS=bert + model bundle)
#   Mode A (Ollama / llamafile):   docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.<ollama|llamafile>.yml up
#   Mode M (Modal-hosted vLLM):    env-only (VLLM_BASE_URL); modal deploy deploy/modal_vllm.py
#   Modal Image.from_dockerfile:   build_args={"FINAL_USER": "root", ...}  (see deploy/README.md)
#
# FINAL_USER=mailroom (uid 10001) is the Docker/Compose contract. Modal
# SDK 1.5.5 has no `container_user` — Modal builds MUST pass FINAL_USER=root
# and drop privileges in-function if a non-root app process is required.

ARG PIP_EXTRAS=bert
ARG ML_EXPORT_ONNX=1
ARG ML_MODEL_REPO=Lucius-Morningstar/mailroom-modernbert-classifier
ARG ML_MODEL_REVISION=main
ARG ML_BUILD_NONE=0
ARG FINAL_USER=mailroom

FROM python:3.11-slim-bookworm AS builder

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src

# PIP_EXTRAS selects optional extras — default "bert" (the mixed dev default);
# "" yields the leanest OpenRouter-only image.
ARG PIP_EXTRAS
RUN pip install --no-cache-dir --prefix=/install .${PIP_EXTRAS:+[${PIP_EXTRAS}]}


# --- Model stage: pinned snapshot of the ModernBERT classifier bundle -------
# Stages Lucius-Morningstar/mailroom-modernbert-classifier @ML_MODEL_REVISION
# into /ml/bundle via huggingface_hub `hf download --local-dir` (no torch, no
# transformers: raw safetensors + heads.pt + tokenizer + labels bundle).
# ML_EXPORT_ONNX is the hook for the future int8 ONNX export pipeline (see
# deploy/README.md § Offline model packaging); until that tooling ships, the
# raw bundle is staged and the sibling mailroom-ml runner picks its own
# backend (BERT_INTAKE_MODE). ML_BUILD_NONE=1 stages an EMPTY bundle dir for
# strict Mode B images — the BERT lane fails open to the deterministic clerk
# at runtime. A download failure ABORTS the build (loud), so images are never
# silently weightless.
FROM python:3.11-slim-bookworm AS model

WORKDIR /ml
ARG ML_EXPORT_ONNX
ARG ML_MODEL_REPO
ARG ML_MODEL_REVISION
ARG ML_BUILD_NONE

RUN pip install --no-cache-dir "huggingface_hub>=0.30" \
    && if [ "$ML_BUILD_NONE" != "1" ]; then \
         hf download "$ML_MODEL_REPO" --revision "$ML_MODEL_REVISION" --local-dir /ml/bundle; \
       else \
         mkdir -p /ml/bundle; \
       fi


FROM python:3.11-slim-bookworm AS runtime

WORKDIR /app

# Runtime needs ca-certificates only (git was build-time for dojo pin).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 mailroom \
    && useradd --system --uid 10001 --gid mailroom --home-dir /app --shell /usr/sbin/nologin mailroom \
    && mkdir -p /data /models \
    && chown -R mailroom:mailroom /app /data \
    && chmod -R a+rX /models

COPY --from=builder /install /usr/local
COPY --chown=mailroom:mailroom pyproject.toml README.md ./
COPY --chown=mailroom:mailroom src ./src

# Baked model bundle (world-readable; empty dir when ML_BUILD_NONE=1 — the
# runner's `no_model` reason handles it, intake stays deterministic).
COPY --from=model /ml/bundle /models/mailroom-modernbert-classifier/

ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1
ENV MAILROOM_API_HOST=0.0.0.0
ENV MAILROOM_API_PORT=7860
ENV MAILROOM_BASE_DIR=/data
ENV MAILROOM_EMBED_WATCHER=1
ENV ML_MODEL_DIR=/models/mailroom-modernbert-classifier
ENV MAILROOM_BERT_INTAKE=0

# The literal USER mailroom line is the TESTED contract (publish_space.py
# checks for "USER mailroom"/"USER 10001"). FINAL_USER stays an override:
# Modal SDK 1.5.5 has no container_user, so Image.from_dockerfile builds
# must pass build_args={"FINAL_USER": "root"} and drop privileges in-function.
ARG FINAL_USER
USER mailroom
USER ${FINAL_USER}

EXPOSE 7860

# Prefer platform PORT (Railway/Fly/Render) over the Spaces image default.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import os,urllib.request; p=os.environ.get('PORT') or os.environ.get('MAILROOM_API_PORT','7860'); urllib.request.urlopen(f'http://127.0.0.1:{p}/health', timeout=3)"

CMD ["python", "-m", "api.main"]