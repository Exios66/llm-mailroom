# deploy/models/ — local model staging (never commit weights)

This directory stages **model weights on the host** for container runs. It is
bind-mounted read-only (`:ro`) into the containers — weights are multi-GB and
must never be baked into images or committed to git (`.dockerignore` excludes
`*.gguf` and `deploy/models/` from every build context).

## Layout

```
deploy/models/
├── README.md
└── llamafile/                 # GGUF weights for the Mode-A llamafile sidecar
    └── qwen3-7b-instruct-q4_k_m.gguf   # ~4.7 GB (Q4_K_M); drop yours here
```

## llamafile (Mode A, offline LLM)

1. Download a GGUF of your choice into `deploy/models/llamafile/`
   (e.g. `qwen3-7b-instruct-q4_k_m.gguf` ≈ 4.7 GB — see resource notes in
   `deploy/README.md`).
2. The sidecar compose mount makes it available at
   `/models/llamafile/<file>` inside the container (`:ro`).
3. Point the sidecar at it:

   ```bash
   LLAMAFILE_MODEL=/models/llamafile/qwen3-7b-instruct-q4_k_m.gguf \
   docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.llamafile.yml \
     --env-file .env up -d --build
   ```

4. The served model id is the `--alias` value (`LLAMAFILE_ALIAS`, default
   `qwen3:7b`) — it must match a value in `src/config/taxonomy.yaml:
   llamafile_model_map:`.

## ModernBERT bundle (BERT fast-path, `ML_MODEL_DIR`)

The app image **bakes** the classifier bundle by default (build-time
`hf download` of `Lucius-Morningstar/mailroom-modernbert-classifier` into
`/models/mailroom-modernbert-classifier` — see the `model` stage in the root
Dockerfile). For development without a rebuild:

1. Prepare a bundle directory locally under `deploy/models/` (same layout:
   `model.safetensors`, `heads.pt`, `tokenizer.json`, `labels.json`,
   `config.json` — or the ONNX int8 output once the export tooling ships).
2. Uncomment the `./models:/models:ro` bind in
   `deploy/docker-compose.yml` and set:

   ```bash
   ML_MODEL_DIR=/models/<your-bundle-dir> MAILROOM_BERT_INTAKE=1
   ```

The BERT lane fails **open**: a missing bundle / missing package degrades to
the deterministic intake clerk (`reason: no_model|no_package` on the
manifest's `intake.bert` block), never crashing intake.

## Rules

- **Never commit** `.gguf`, `.safetensors`, `.pt`, `.onnx` files.
- `.gitkeep` files only — this directory is staging space, not an artifact
  store. On CI/air-gapped hosts, prepare it once and copy it over.