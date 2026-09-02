<div align="center">

# 🌐 HuggingFace Space Deployment

**HuggingFace Space deployment configuration for the llm-mailroom package.**

</div>

---

## Purpose

Deploy the Mailroom pipeline as a HuggingFace Space.

## Configuration

| File | Purpose |
|:---|:---|
| `PAIRING.md` | Observatory + producer Space pairing guide |
| `SPACE_README.md` | Space README template |

## Usage

```bash
cd packages/llm-mailroom
python scripts/publish_space.py --check
python scripts/publish_space.py --repo <user>/llm-mailroom
```

## Related Files

- `../` — Deployment root
- `src/` — Source code
