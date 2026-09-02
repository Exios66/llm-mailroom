<div align="center">

# 📝 Reporter Skill

**Reporter skill for the vendored LangChain agents.**

</div>

---

## Purpose

Assembles specialist extraction into a formatted report.

## Implementation

This is a **procedural** assembler (no LLM call):
- Formats specialist JSON
- Adds arbiter caveats if present
- Generates `extracted_data._report`

## Related Files

- `../judge/` — Judge skill
- `../arbiter/` — Arbiter skill
