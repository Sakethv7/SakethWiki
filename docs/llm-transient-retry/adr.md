# ADR: Bounded Retries at the Shared LLM Boundary

## Status

Accepted — approved by the user on 2026-09-02.

## Decision

Retry only transient provider and network failures inside `llm_client.complete`, with three total attempts, exponential bounded jitter, optional `Retry-After`, and environment-configurable limits.

## Why

The screenshot is a transient Anthropic 529 overload, but the current single-attempt path treats it like a permanent extraction failure. The shared LLM boundary is the narrowest place that covers foreground capture, background URL extraction, and other completion users while staying upstream of persistent queue and vault writes.

## Alternatives considered

### Retry only in the Capture UI

Rejected. Closing or navigating away would cancel browser-owned recovery, background extraction would remain broken, and retry rules would drift across callers.

### Retry `_background_extract` end to end

Rejected. This would repeat URL fetching and risks expanding the retry boundary around queue-state transitions. The failure shown is specifically the provider request.

### Use the Anthropic SDK's implicit retry behavior only

Rejected. The observed 529 reached the application, so the currently installed/configured SDK behavior is not sufficient or visible. An application-level policy gives deterministic tests, cross-provider behavior, explicit bounds, and telemetry.

### Retry every exception

Rejected. Authentication, malformed requests, and contract failures do not become healthy with delay and would waste latency and cost.

## Consequences

- Short provider incidents recover automatically.
- A single logical completion may incur more than one provider charge if a response is lost.
- Terminal failures arrive later because the app waits through bounded backoff.
- Retry behavior becomes consistent and observable across supported providers.

## Rollback

Set `LLM_MAX_ATTEMPTS=1` for immediate operational rollback. Code rollback removes the retry helper and attempt telemetry fields; no data migration or queue repair is required.
