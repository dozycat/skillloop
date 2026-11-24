# Treasury bulletin question answering

## Goal
An agent answers factual questions about U.S. federal finances (expenditures, debt, interest outlays,
international claims, 1939-2003) by locating the relevant table in historical Treasury Bulletin text
files and reporting the requested figure.

## Scope
- Each question is answerable from one bulletin in the provided corpus.
- Questions range from a single table lookup to sums over monthly rows and derived quantities.
- No web access and no outside data sources.

## Acceptance criteria
- The reported value matches the source table exactly (after any arithmetic the question asks for).
- The answer uses the unit and rounding the question specifies; fiscal-year versus calendar-year and
  the exact line item asked for are respected.
- The reply ends with the required `FINAL ANSWER:` line.

## Constraints
- Answers must include units when the question asks for them.
- The agent must not use external APIs.
