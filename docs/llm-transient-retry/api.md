# LLM Transient Retry — Contracts

## Python contract

`llm_client.complete(...) -> str` keeps its existing public signature and return behavior.

New internal retry behavior:

- A successful call returns one response string.
- A non-retryable exception is raised immediately.
- A retryable exception is raised only after the configured attempt budget is exhausted.
- JSON contract validation is performed after a transport success and is not treated as a transient transport failure.

## Configuration contract

| Variable | Default | Valid effective range | Meaning |
|---|---:|---:|---|
| `LLM_MAX_ATTEMPTS` | `3` | `1..5` | Total provider calls, including the first attempt |
| `LLM_RETRY_BASE_SECONDS` | `1` | `0..10` | Initial exponential-backoff delay |
| `LLM_RETRY_MAX_SECONDS` | `8` | `0..30` | Maximum delay before any retry |

Missing, malformed, or out-of-range values fall back to or are clamped to safe bounds; they never create unbounded attempts or sleeps.

## Retry classification contract

Retryable:

- HTTP status: 408, 409, 429, 500, 502, 503, 504, 529
- Network connect/read/write/pool timeouts
- Network connection failures

Not retryable:

- Authentication and permission failures
- Invalid request/model/input failures
- Valid responses that fail the required JSON contract
- Local parsing or application-state errors

## Telemetry contract

The existing `llm_call` event adds:

- `primary_attempts`: provider calls made on the primary route
- `fallback_attempts`: provider calls made on the fallback route, otherwise `0`

No API keys, headers, prompts, or response bodies are added to retry logs.

## HTTP/UI contract

No endpoint response schema changes. If retries recover, Capture behaves as an ordinary success. If retries exhaust, the existing error response and existing manual retry UI remain the recovery path.

