<div align="center">

# 📄 PDF Transcriber Skill

**PDF transcriber skill for the vendored LangChain agents.**

</div>

---

## Purpose

Extracts text from PDF files using a hybrid approach.

## Methods

| Method | When Used |
|:---|:---|
| `direct` | Text-based PDFs with sufficient characters/page |
| `llm` | Scanned or garbled PDFs requiring OCR |

## Threshold

The threshold is `pipeline.pdf_direct_chars_per_page` in `taxonomy.yaml`.

## Related Files

- `../image_extractor/` — Image text extraction
- `../sorter/` — Document classification
