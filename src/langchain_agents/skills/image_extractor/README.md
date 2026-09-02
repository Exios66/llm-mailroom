<div align="center">

# 🖼️ Image Extractor Skill

**Image extractor skill for the vendored LangChain agents.**

</div>

---

## Purpose

Extracts visible text from image files using vision LLMs.

## Schema

| Field | Type |
|:---|:---|
| `text` | `str` |
| `confidence` | `float` |
| `method` | `str` (always `vision`) |

## Supported Formats

- JPG, PNG, GIF, WebP, TIFF, BMP

## Related Files

- `../pdf_transcriber/` — PDF transcription
- `../sorter/` — Document classification
