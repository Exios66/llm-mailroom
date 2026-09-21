FROM debian:bookworm-slim

# Llamafile sidecar for llm-mailroom Mode A (offline LLM). The binary is a
# single Cosmopolitan executable (Mozilla llamafile; runs on linux amd64 AND
# aarch64 — see docs.mozilla.ai/llamafile/reference/support). The GGUF weights
# are NEVER baked into the image — they are bind-mounted at runtime
# (deploy/models/llamafile/, see deploy/models/README.md).
#
# Build (from the repo root):
#   docker build -f deploy/llamafile/Dockerfile -t mailroom-llamafile .
# or via compose: docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.llamafile.yml build

ARG LLAMAFILE_VERSION=0.10.4    # pin a tested release (bump deliberately)

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /llamafile

RUN curl -fsSL -o llamafile \
      "https://github.com/mozilla-ai/llamafile/releases/download/${LLAMAFILE_VERSION}/llamafile-${LLAMAFILE_VERSION}" \
    && chmod +x llamafile

COPY entrypoint.sh /usr/local/bin/llamafile-entrypoint
RUN chmod +x /usr/local/bin/llamafile-entrypoint

# Runtime knobs (override via compose environment)
ENV LLAMAFILE_MODEL=/models/llamafile/qwen3-7b-instruct-q4_k_m.gguf
ENV LLAMAFILE_ALIAS=qwen3:7b
ENV LLAMAFILE_CTX=16384
ENV LLAMAFILE_GPU=disable

EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/llamafile-entrypoint"]