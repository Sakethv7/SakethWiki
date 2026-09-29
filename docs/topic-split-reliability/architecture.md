# Topic Split Reliability — Architecture

## Status

Implemented with bounded repair and deterministic fallback.

## Decision

Use LLM semantic grouping as the primary topic detector, but validate its paragraph coverage and fall back to explicit headings or conservative paragraph-size chunks. A fallback is labeled in telemetry; it never claims semantic topic discovery.

## Safety invariants

- Every source paragraph is assigned exactly once in an accepted LLM plan.
- Invalid, duplicated, missing, or out-of-range paragraph indices are never used.
- A failed planner does not discard the source or invent topic titles.
- Child extraction remains review-gated; splitting never writes to the vault.
- LLM planner repair is bounded to one additional structured-output request.

## Failure path

```text
LLM planner -> valid coverage -> semantic child drafts
     | invalid JSON -> one compact repair -> validate coverage
     | invalid coverage / repair failure -> headings -> size chunks -> one note
```

Explicit Markdown headings take precedence over a planner call. If no reliable structure exists, the system retains one note rather than presenting arbitrary chunks as topics.
