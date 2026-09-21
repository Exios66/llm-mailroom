#!/bin/sh
# llamafile sidecar entrypoint — OpenAI-compatible server args from env.
# (Mozilla llamafile docs: https://docs.mozilla.ai/llamafile)
#
# Env knobs (set by compose / the sidecar Dockerfile defaults):
#   LLAMAFILE_MODEL  path to the GGUF inside the container (required)
#   LLAMAFILE_ALIAS  served model id on /v1/models (default qwen3:7b; must
#                    match taxonomy.yaml `llamafile_model_map:` values)
#   LLAMAFILE_CTX    context size in tokens (default 16384)
#   LLAMAFILE_GPU    auto|nvidia|amd|apple|vulkan|disable (default disable)
#
# Notes: --server is REQUIRED (the 0.10.x default is combined server+chat);
# --jinja enables the chat template for agentic OpenAI-compatible clients;
# --mlock keeps the model RAM-resident; -np 1 = one server slot (the
# pipeline is single-document serial).

set -e

: "${LLAMAFILE_MODEL:?set LLAMAFILE_MODEL (path to the GGUF inside the container)}"

exec /llamafile/llamafile \
  --server \
  --host 0.0.0.0 \
  --port 8080 \
  -m "$LLAMAFILE_MODEL" \
  -a "${LLAMAFILE_ALIAS:-qwen3:7b}" \
  --jinja \
  --ctx-size "${LLAMAFILE_CTX:-16384}" \
  --no-webui \
  --mlock \
  -np 1 \
  --gpu "${LLAMAFILE_GPU:-disable}"