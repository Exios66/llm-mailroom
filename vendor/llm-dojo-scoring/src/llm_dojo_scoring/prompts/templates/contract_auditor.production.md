<!-- provenance: llm-entity-extraction contracts_audit_v0 -->

AUDIT PASS — the extraction above may have MISSED obligation clauses. Your
job: find obligation clauses the extraction did not quote, and quote them
verbatim.

RESTRICTED CATEGORIES (only these five may produce output):
- Covenant Not To Sue
- Competitive Restriction Exception
- Volume Restriction
- Minimum Commitment
- Post‑Termination

For EACH of the five categories below, check the window text: if a clause sentence
for that category is PRESENT in the window but is NOT already quoted above (or
is only PARTIALLY quoted), quote the COMPLETE clause sentence VERBATIM, from its
first word through its final period.  Use the keyword hint for each category to
help identify the right clause.

STRICT RULES:
1. QUOTE VERBATIM — the clause sentence must appear in the window text
   word-for-word. Never paraphrase, never summarize, never expand.
2. NEVER FABRICATE — if a category has no clause sentence in the window, emit
   nothing for it. A quote must be a real, verbatim sentence from the window.
3. KEYWORD FILTER — only include a clause if it contains at least one of the
   category‑specific keyword(s) listed below; otherwise omit it.
3. NEVER RE‑QUOTE — if the extraction already quoted a clause fully, do not
   quote it again. But if its quote is only a FRAGMENT of a longer clause
   sentence, quote the COMPLETE sentence.
4. ONE ENTRY PER DISTINCT CLAUSE SENTENCE — a category with several distinct
   clause sentences in the window gets one entry per sentence.
5. EXACT CATEGORY NAMES — tag each entry with the exact canonical name above;
   never a sibling or a generic label.
6. EMPTY IS OK — respond ONLY with the JSON object:
   {"missing_obligations": []} when no clause satisfies the rules above. An
   empty list is a valid, honest answer.

Respond ONLY with the JSON object: {"missing_obligations": [{"category":
"<exact canonical name>", "clause": "<complete verbatim clause sentence>"}]}
An empty list is a valid, honest answer when nothing is missing.
