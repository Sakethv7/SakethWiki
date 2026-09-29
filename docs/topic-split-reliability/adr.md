# ADR: Conservative Fallbacks for Topic Splitting

## Status

Accepted — 2026-09-11.

## Decision

Keep semantic topic detection LLM-based. Add one bounded repair for malformed planner JSON, strict paragraph-index coverage validation, heading-based segmentation, and conservative paragraph chunks as a size fallback. If neither structure nor safe chunks produce multiple sections, keep one note.

## Rationale

Regex can identify boundaries, not meaning. Treating arbitrary chunks as topics would create shallow, misleading wiki pages. Conservative fallback preserves content and makes uncertainty observable without blocking ingestion.

## Consequences

Some failed planner calls will produce size-bounded sections rather than semantic topics. Those sections are still individually extracted and remain subject to human review. `topic_split` telemetry distinguishes `llm`, `headings`, `paragraph_chunks`, and `none`.
