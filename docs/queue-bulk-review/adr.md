# ADR: Explicit Selection with a Per-Item Batch Result

## Status

Proposed — awaiting user approval.

## Decision

Use persistent checkboxes and a selection toolbar for bulk review, keep approve/reject visible on every collapsed extracted card, and add one backend batch-decision route that returns a result for every requested item.

## Why

The main cost is opening cards, not reading them. The title and target slug already provide enough information for many obvious decisions, so the common action belongs on the collapsed card.

Bulk decisions need deliberate scope. Checkboxes make the chosen set visible and auditable; a confirmation step prevents a stray click from writing or rejecting dozens of candidates.

The backend must report partial completion. Approvals can create or evolve multiple Markdown pages, append traces, and update derived indexes. Those side effects cannot be rolled back safely as one transaction with the current file-based architecture.

## Alternatives considered

### Call the existing endpoint repeatedly from the browser

Rejected. It duplicates orchestration in the client, makes partial failure accounting fragile, and offers no single server-side request contract or request limit.

### Add `Approve all` and `Reject all`

Rejected. A 77-item queue can contain uncertain or failed extractions. A global action makes scope too easy to misunderstand.

### Put actions behind a card hover state

Rejected. Hover is unavailable on touch devices and makes important controls undiscoverable.

### Make bulk approval atomic

Rejected for this iteration. True atomicity would require transactional staging and rollback across queue JSON, Markdown writes, traces, preference memory, and indexing. Pretending the existing pipeline is atomic would hide real failure states.

## Consequences

- Review is much faster for obvious candidates.
- Detailed `Review` and card expansion remain available for uncertain candidates.
- The UI must clearly communicate partial success.
- A shared backend decision function is required to prevent single and batch behavior from drifting.
- This design intentionally does not add undo. Rejected items remain represented in traces, but restoration would be a separate feature.

