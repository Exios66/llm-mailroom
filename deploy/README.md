<div align="center">

# 🚀 Deployment Configuration

**Deployment configuration for the llm-mailroom package.**

</div>

---

## Structure

| Path | Contents |
|:---|:---|
| [`space/`](space/) | HuggingFace Space deployment |

## Configuration Files

| File | Purpose |
|:---|:---|
| `Dockerfile` | Docker image definition |
| `nixpacks.toml` | Nixpacks configuration |
| `railway.json` | Railway deployment config |

## Usage

Build from the package directory:
```bash
cd packages/llm-mailroom
docker build -t llm-mailroom .
```

## Related Files

- `src/` — Source code
- `docs/` — Documentation
