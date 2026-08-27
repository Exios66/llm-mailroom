# Confidence Calibration — Sorter Skill Reference

The sorter's `confidence` score drives routing. Derive it from evidence in THIS document, never from a default high value.

## When to be decisive (0.95+)

- The document's operative form is unambiguous (a labeled agreement, bylaws, 10-K wrapper, FNOL/coverage letter).
- Competing classes are implausible from the visible text.
- Cite the concrete heading, party block, or filing caption that settles the class.

## When to flag for review (below high)

- Multi-topic memos that mix contract terms, filings, and correspondence.
- An exhibit wrapper whose body is a different class (SEC exhibit of a contract is still a contract; a demand letter about a contract is correspondence).
- Truncated or illegible text that hides the operative form.

## Retired classes

Court opinions and due-diligence checklists/memos are not live mailroom classes. Emit `unknown` rather than remapping them onto correspondence or contract.
