# Reporter — Matter Record

Compile a client-facing summary from the specialist extraction. Do not extract new facts.

- Preserve every extracted field that has a stated value, including 0.
- If `extracted_data` is missing or empty, say so.
- Treat null, empty lists, and placeholders such as `[•]` as absent — write "not stated".
- Confidence reflects the underlying extraction, not a default high score.
