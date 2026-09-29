# Capture Queue — Architecture

## Status

Proposed. This document authorizes no implementation or vault writes by itself.

## Problem

The main capture composer waits for synchronous extraction before it can accept another text, URL, or image capture. The iOS Share Sheet has a separate URL-only fast path (`POST /queue-url`), which persists a queue item then starts a fire-and-forget extraction task. The two flows have different capabilities and the task is lost on a process restart while it is running.

## Goal

Allow the user to submit text, URLs, Markdown clips, and image captures immediately, continue entering the next capture, and review each generated draft before any Markdown vault mutation.

## Scope

- One unified capture endpoint and queue item schema for URL, text, Markdown, and images.
- Persistent queue states: `queued`, `processing`, `ready`, and `failed`.
- A single in-process worker that processes items FIFO, resumes recoverable work after server start, and exposes retry only after failure.
- Composer feedback that frees the input immediately and shows queued/processing counts.

Out of scope: automatic approval, automatic vault writes, a remote durable job runner, cross-device cancellation, or parallel LLM extraction.

## Components and data flow

```text
Composer / Share Sheet
  -> POST /queue-capture (capture_key + immutable payload)
  -> queue_manager.enqueue_or_get() under flock
  -> hitl_queue.json: queued
  -> one CaptureWorker claims oldest queued item: processing
  -> fetch/normalize/extract
       -> ready: draft metadata for human review
       -> failed: sanitized retryable error
  -> QueueSection polling displays state
  -> explicit POST /approve/{id}
  -> wiki_writer -> Markdown vault + trace + index refresh
```

The queue JSON remains derived, recoverable state; the Markdown vault remains the only durable knowledge source of truth. A queued item must never call `wiki_writer`.

## Queue item contract

Each item contains `id`, `capture_key`, `source_kind`, `payload`, `status`, `attempt_count`, `queued_at`, and state timestamps. `payload` is immutable after creation. Extracted draft fields are written only when status becomes `ready`; a failure adds a sanitized `error` object.

`capture_key` is browser-generated once per click and supports idempotent submission. A second request with the same key returns the original item without a second queue record or a second extraction.

Images remain in the queue only until the user approves or rejects the item. The API enforces an explicit, documented payload limit before writing queue JSON; rejected/approved items are removed exactly as current queue decisions remove items.

## Worker lifecycle and recovery

- At startup, a recovery pass changes stale `processing` entries to `queued`. It does not increment `attempt_count`: the interrupted attempt did not produce a terminal outcome.
- The worker claims an item by updating its status while holding the queue lock. Only the worker may make `queued -> processing`; only the worker processing that ID may make `processing -> ready|failed`.
- Concurrency is exactly one. This preserves input order, bounds provider load/cost, and avoids concurrent mutable queue races.
- An unexpected worker-loop exception is logged and the loop continues. An item-level extraction failure becomes `failed`, preserves the original capture, and requires an explicit retry request.
- No automatic retry is introduced in this change. Provider retry policy stays at its current boundary and is not broadened here.

## Invariants and failure modes

| Invariant | Failure prevented |
|---|---|
| A capture key maps to exactly one queue item. | Double click/network replay creates duplicate LLM work or notes. |
| A claim and every state mutation occur under the file lock. | Two workers process the same item. |
| Only `ready` items may be approved. | An incomplete draft reaches the vault. |
| The capture payload is retained through failure and retry. | User input disappears after a provider/fetch failure. |
| Restart requeues interrupted work without treating it as successful. | A forever-spinning or silently lost item. |
| Approval remains a distinct, explicit operation. | Background work silently changes authoritative Markdown. |

## Callers and downstream consumers

The React composer, iOS Share Sheet, inbox import, and existing QueueSection are capture callers/consumers. `queue_manager.py` persists the queue. The worker uses current fetch/extraction helpers and writes draft metadata. `POST /approve/{id}`, batch decisions, trace logging, `wiki_writer.py`, and memory indexing consume only ready queue items; their write behavior remains unchanged.

## Verification after approval

- Unit tests for idempotent submit, FIFO claim, start-up recovery, failed capture retention, retry, and an approval attempt against each non-ready state.
- A restart simulation with an item claimed but not completed.
- Existing queue-decision tests plus new direct text and image queue tests.
- Frontend build and a bounded browser check: queue capture A, immediately enter capture B, observe `queued -> processing -> ready`, then explicitly approve only one item.
- Review final diff for no direct `wiki_writer` call from worker code and no raw input/provider response in error records.

## Rollback and recovery

Rollback is code rollback plus restart; existing ready items remain reviewable using the existing queue API. For an item stuck in `processing`, restart invokes recovery and returns it to `queued`. No vault content is created until a user approval, so queue removal/rejection has no authoritative-content rollback requirement.
