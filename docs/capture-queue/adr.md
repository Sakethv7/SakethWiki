# ADR: Unified Persistent Capture Queue With One Worker

## Status

Proposed — awaiting approval.

## Decision

Replace the main composer’s synchronous extract-before-next-capture flow and the URL-only fast path with `POST /queue-capture`. Persist every accepted capture before returning, process queue entries in FIFO order with one in-process worker, and require the existing explicit review/approval step to write to the vault.

## Context

The user needs to continue capturing while an LLM/fetch request is running. Existing queue persistence is file-backed and lock-protected, but the current background extraction is a separate task per URL and has no restart recovery. Extending that behavior directly to every input would increase concurrent provider calls and can leave a task logically processing after restart.

## Alternatives considered

### Add a second composer textarea only

Rejected. It masks the blocked request but does not persist the second capture, solve restart behavior, or expose a truthful state.

### Browser fires `/ingest` for every capture concurrently

Rejected. It makes provider concurrency, errors, duplicate submissions, and ordering client-dependent; the user can still lose a capture on refresh.

### Keep a background task per item

Rejected. It retains the current restart-loss issue and gives no bounded concurrency or atomic work claim.

### Add Redis/Celery or a hosted queue now

Rejected for the local-first product. It adds operational dependencies before evidence that a single local worker is insufficient. The state machine and API are deliberately shaped so a later worker backend can replace the in-process implementation without changing review semantics.

## Consequences

Capture becomes fast and resilient to transient client failures. Extraction becomes intentionally serialized, so a long task delays later items; the UI must show that delay rather than pretending simultaneous processing. A process crash can replay an interrupted item, therefore the worker must never have vault-writing side effects. Per-provider request retry remains governed by the existing LLM retry policy rather than this queue.
