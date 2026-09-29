# ADR: Regenerate Diagrams From Evidence With Versioned Candidates

## Status

Proposed — awaiting approval.

## Decision

Replace the current summary-only `mode="diagram"` overwrite with evidence-preserving, versioned diagram candidates. A candidate must first receive a structure-aware plan and pass deterministic render, shape, and grounding checks. The planner can return no diagram. A queue compare-and-swap prevents stale review tabs from overwriting one another.

## Context

Ingestion already has access to raw capture material, images, `knowledge_shape`, and `diagram_plan`. The regeneration path discards those inputs, treats every result as a generic regenerated type, and falls back to an input-processing-output template. That violates the wiki's curation boundary: visual structure is evidence, not decoration.

The queue is persistent review state. Regeneration therefore has a higher failure cost than a display-only render: an unversioned write can silently replace another reviewer's selected draft.

## Alternatives considered

### Improve the summary-only prompt

Rejected. Better wording cannot recover source labels, spatial directions, or omitted relationships. It would still create diagrams from a lossy representation.

### Always fall back to a generic diagram

Rejected. It makes the UI look complete while introducing unsupported structure. A validated `none` result is more honest and more useful.

### Regenerate the entire extraction on every diagram retry

Rejected. It needlessly changes title, bullets, routing, and curation metadata when the reviewer only wants to improve a diagram.

### Overwrite the selected diagram but keep an audit log elsewhere

Rejected. It leaves conflict handling and parent-candidate recovery ambiguous. Versioned candidates make selection explicit and reversible before approval.

## Consequences

Diagram regeneration takes more input and may cost more than the existing summary-only call, especially for images. The result is auditable, recoverable, and structurally constrained. Some requests will intentionally produce no diagram. The user gains a clear explanation of why a candidate was rejected rather than a silently degraded fallback.
