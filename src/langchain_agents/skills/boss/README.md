<div align="center">

# 👔 Boss Skill

**Boss skill for the vendored LangChain agents.**

</div>

---

## Purpose

Handles data conflicts and repeated low confidence situations.

## Usage

The boss is invoked when:
- Classification confidence is persistently low
- Data conflicts arise between agents
- Escalation is needed

## Related Files

- `../arbiter/` — Arbiter skill
- `../judge/` — Judge skill
